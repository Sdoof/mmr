import sys
import os
import nest_asyncio

from trader.objects import WhatToShow
nest_asyncio.apply()

# in order to get __main__ to work, we follow: https://stackoverflow.com/questions/16981921/relative-imports-in-python-3
PACKAGE_PARENT = '../..'
SCRIPT_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
sys.path.append(os.path.normpath(os.path.join(SCRIPT_DIR, PACKAGE_PARENT)))

import pandas as pd
import datetime as dt
import backoff
import aioreactive as rx

from asyncio.events import AbstractEventLoop
from aioreactive.types import AsyncObservable, Projection
from expression.core import pipe
from aioreactive.observers import AsyncAnonymousObserver
from enum import Enum

from trader.common.logging_helper import setup_logging
logging = setup_logging(module_name='trading_runtime')

from arctic import Arctic, TICK_STORE
from arctic.date import DateRange
from arctic.tickstore.tickstore import TickStore
from ib_insync.ib import IB
from ib_insync.contract import Contract, Forex, Future, Stock
from ib_insync.objects import PortfolioItem, Position, BarData
from ib_insync.order import LimitOrder, Order, Trade
from ib_insync.util import df
from ib_insync.ticker import Ticker
from eventkit import Event

from trader.listeners.ibaiorx import IBAIORx
from trader.common.contract_sink import ContractSink
from trader.common.listener_helpers import Helpers
from trader.common.observers import ConsoleObserver, ArcticObserver, ComplexConsoleObserver, ContractSinkObserver, NullObserver
from trader.data.data_access import SecurityDefinition, TickData
from trader.data.universe import UniverseAccessor, Universe
from trader.container import Container
from trader.trading.book import Book
from trader.trading.algo import Algo
from trader.trading.portfolio import Portfolio
from trader.trading.executioner import Executioner
from trader.trading.strategy import Strategy
from trader.common.reactive import AsyncCachedObserver, AsyncEventSubject, AsyncCachedSubject
from trader.common.singleton import Singleton
from trader.common.helpers import get_network_ip, Pipe, dateify, timezoneify, ListHelper
from trader.messaging.bus_server import start_lightbus
from trader.data.market_data import MarketData, SecurityDataStream

from typing import List, Dict, Tuple, Callable, Optional, Set, Generic, TypeVar, cast, Union

# notes
# https://groups.io/g/insync/topic/using_reqallopenorders/27261173?p=,,,20,0,0,0::recentpostdate%2Fsticky,,,20,2,0,27261173
# talks about trades/orders being tied to clientId, which means we'll need to always have a consistent clientid

class Action(Enum):
    BUY = 1
    SELL = 2

    def __str__(self):
        if self.value == 1: return 'BUY'
        if self.value == 2: return 'SELL'


class Trader(metaclass=Singleton):
    def __init__(self,
                 ib_server_address: str,
                 ib_server_port: int,
                 arctic_server_address: str,
                 arctic_library: str,
                 arctic_universe_library: str,
                 redis_server_address: str,
                 redis_server_port: str,
                 paper_trading: bool = False,
                 simulation: bool = False):
        self.ib_server_address = ib_server_address
        self.ib_server_port = ib_server_port
        self.arctic_server_address = arctic_server_address
        self.arctic_library = arctic_library
        self.arctic_universe_library = arctic_universe_library
        self.simulation: bool = simulation
        self.paper_trading = paper_trading
        self.redis_server_address = redis_server_address
        self.redis_server_port = redis_server_port

        # todo I think you can have up to 24 connections to TWS (and have multiple TWS instances running)
        # so we need to take this from single client, to multiple client
        self.client: IBAIORx
        self.data: TickData
        self.universe_accessor: UniverseAccessor

        # the live ticker data streams we have
        self.contract_subscriptions: Dict[Contract, ContractSink] = {}
        # the minute-by-minute MarketData stream's we're subscribed to
        self.market_data_subscriptions: Dict[SecurityDefinition, SecurityDataStream] = {}
        # the strategies we're using
        self.strategies: List[Strategy] = []
        # current order book (outstanding orders, trades etc)
        self.book: Book = Book()
        # portfolio (current and past positions)
        self.portfolio: Portfolio = Portfolio()
        # takes care of execution of orders
        self.executioner: Executioner
        # a list of all the universes of stocks we have registered
        self.universes: List[Universe]
        self.market_data = 3

    @backoff.on_exception(backoff.expo, ConnectionRefusedError, max_tries=10, max_time=120)
    def connect(self):
        self.client = IBAIORx(self.ib_server_address, self.ib_server_port)
        self.data = TickData(self.arctic_server_address, self.arctic_library)
        self.universe_accessor = UniverseAccessor(self.arctic_server_address, self.arctic_universe_library)
        self.universes = self.universe_accessor.get_all()
        self.clear_portfolio_universe()
        self.contract_subscriptions = {}
        self.market_data_subscriptions = {}
        self.client.ib.connectedEvent += self.connected_event
        self.client.ib.disconnectedEvent += self.disconnected_event
        self.client.connect()

    def reconnect(self):
        # this will force a reconnect through the disconnected event
        self.client.ib.disconnect()

    async def __update_positions(self, positions: List[Position]):
        logging.debug('__update_positions')
        for position in positions:
            self.portfolio.add_position(position)

    async def __update_portfolio(self, portfolio_item: PortfolioItem):
        logging.debug('__update_portfolio')
        self.portfolio.add_portfolio_item(portfolio_item=portfolio_item)
        await self.update_portfolio_universe(portfolio_item)

    async def setup_subscriptions(self):
        if not self.is_ib_connected():
            raise ConnectionError('not connected to interactive brokers')

        error = False

        async def handle_subscription_exception(ex):
            logging.exception(ex)
            error = True

        # have the book subscribe to all relevant trade events
        await self.book.subscribe_to_eventkit_event(
            [
                self.client.ib.orderStatusEvent,
                self.client.ib.orderModifyEvent,
                self.client.ib.newOrderEvent,
                self.client.ib.cancelOrderEvent,
                self.client.ib.openOrderEvent,
            ]
        )

        positions = await self.client.subscribe_positions()
        await positions.subscribe_async(AsyncCachedObserver(self.__update_positions,
                                                            athrow=handle_subscription_exception,
                                                            capture_asend_exception=True))

        portfolio = await self.client.subscribe_portfolio()
        await portfolio.subscribe_async(AsyncCachedObserver(self.__update_portfolio,
                                                            athrow=handle_subscription_exception,
                                                            capture_asend_exception=True))

        # because the portfolio subscription is synchronous, an observer isn't attached
        # as the ib.portfolio() method is called, so call it again
        for p in self.client.ib.portfolio():
            await self.client.portfolio_subject.asend(p)

        # make sure we're getting either live, or delayed data
        self.client.ib.reqMarketDataType(self.market_data)

        orders = await self.client.ib.reqAllOpenOrdersAsync()
        for o in orders:
            await self.book.asend(o)

    async def connected_event(self):
        logging.debug('connected_event')
        await self.setup_subscriptions()

    async def disconnected_event(self):
        logging.debug('disconnected_event')
        self.connect()

    def clear_portfolio_universe(self):
        logging.debug('clearing portfolio universe')
        universe = self.universe_accessor.get('portfolio')
        universe.security_definitions.clear()
        self.universe_accessor.update(universe)

    async def update_portfolio_universe(self, portfolio_item: PortfolioItem):
        """
        Grabs the current portfolio from TWS and adds a new version to the 'portfolio' table.
        """
        universe = self.universe_accessor.get('portfolio')
        if not ListHelper.isin(
            universe.security_definitions,
            lambda definition: definition.conId == portfolio_item.contract.conId
        ):
            contract = portfolio_item.contract
            contract_details = await self.client.get_contract_details(contract)
            if contract_details and len(contract_details) >= 1:
                universe.security_definitions.append(
                    SecurityDefinition.from_contract_details(contract_details[0])
                )

            logging.debug('updating portfolio universe with {}'.format(portfolio_item))
            self.universe_accessor.update(universe)

            if not ListHelper.isin(
                list(self.market_data_subscriptions.keys()),
                lambda subscription: subscription.conId == portfolio_item.contract.conId
            ):
                logging.debug('subscribing to market data stream for portfolio item {}'.format(portfolio_item.contract))
                security = cast(SecurityDefinition, universe.find_contract(portfolio_item.contract))
                date_range = DateRange(
                    start=dateify(dt.datetime.now() - dt.timedelta(days=30)),
                    end=timezoneify(dt.datetime.now(), timezone='America/New_York')
                )
                security_stream = SecurityDataStream(
                    security=security,
                    bar_size='1 min',
                    date_range=date_range,
                    existing_data=None
                )
                await self.client.subscribe_contract_history(
                    contract=portfolio_item.contract,
                    start_date=dateify(dt.datetime.now() - dt.timedelta(days=30)),
                    what_to_show=WhatToShow.TRADES,
                    observer=security_stream
                )
                self.market_data_subscriptions[security] = security_stream

    async def temp_place_order(
        self,
        contract: Contract,
        order: Order
    ) -> AsyncCachedObserver[Trade]:
        async def handle_exception(ex):
            logging.exception(ex)
            # todo sort out the book here

        async def handle_trade(trade: Trade):
            logging.debug('handle_trade {}'.format(trade))
            # todo figure out what we want to do here

        observer = AsyncCachedObserver(asend=handle_trade,
                                       athrow=handle_exception,
                                       capture_asend_exception=True)

        disposable = await self.client.subscribe_place_order(contract, order, observer)
        return observer

    async def temp_handle_order(
        self,
        contract: Contract,
        action: Action,
        equity_amount: float,
        delayed: bool = False,
        debug: bool = False,
    ) -> AsyncCachedObserver[Trade]:
        # todo make sure amount is less than outstanding profit

        # grab the latest price of instrument
        subject = await self.client.subscribe_contract(contract=contract, one_time_snapshot=True)

        xs = pipe(
            subject,
            Pipe[Ticker].take(1)
        )

        observer = AsyncCachedObserver[Ticker]()
        await xs.subscribe_async(observer)
        latest_tick = await observer.wait_value()

        # todo perform tick sanity checks

        # assess if we should trade
        quantity = equity_amount / latest_tick.bid

        if quantity < 1 and quantity > 0:
            quantity = 1.0

        # round the quantity
        quantity_int = round(quantity)

        logging.debug('temp_handle_order assessed quantity: {} on bid: {}'.format(
            quantity_int, latest_tick.bid
        ))

        limit_price = latest_tick.bid
        # if debug, move the buy/sell by 10%
        if debug and action == Action.BUY:
            limit_price = limit_price * 0.9
            limit_price = round(limit_price * 0.9, ndigits=2)
        if debug and action == Action.SELL:
            limit_price = round(limit_price * 1.1, ndigits=2)

        # put an order in
        order = LimitOrder(action=str(action), totalQuantity=quantity_int, lmtPrice=limit_price)
        return await self.temp_place_order(contract=contract, order=order)

    def cancel_order(self, order_id: int) -> Optional[Trade]:
        # get the Order
        order = self.book.get_order(order_id)
        if order and order.clientId == self.client.client_id_counter:
            logging.info('cancelling order {}'.format(order))
            trade = self.client.ib.cancelOrder(order)
            return trade
        else:
            logging.error('either order does not exist, or originating client_id is different: {} {}'
                          .format(order, self.client.client_id_counter))
            return None

    def is_ib_connected(self) -> bool:
        return self.client.ib.isConnected()

    def red_button(self):
        self.client.ib.reqGlobalCancel()

    def status(self) -> Dict[str, bool]:
        # todo lots of work here
        status = {
            'ib_connected': self.client.ib.isConnected(),
            'arctic_connected': self.data is not None
        }
        return status

    def get_universes(self) -> List[Universe]:
        return self.universes

    def run(self):
        self.client.run()
