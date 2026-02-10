"""
AS 網格交易機器人 - Gate.io 命令行版
無 GUI 版本，適合在服務器上運行
"""
import asyncio
import websockets
import json
import logging
import hmac
import hashlib
import time
import ccxt
import math
import os

# ==================== 配置 ====================
API_KEY = ""  # 替換為你的 API Key
API_SECRET = ""  # 替換為你的 API Secret
COIN_NAME = "XRP"  # 交易幣種
GRID_SPACING = 0.006  # 補倉間距 (0.6%)
TAKE_PROFIT_SPACING = 0.004  # 止盈間距 (0.4%)
INITIAL_QUANTITY = 1  # 初始交易數量 (張數)
LEVERAGE = 20  # 槓桿倍數
WEBSOCKET_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"  # WebSocket URL
POSITION_THRESHOLD = 500  # 鎖倉閾值
POSITION_LIMIT = 100  # 持倉數量閾值
ORDER_COOLDOWN_TIME = 60  # 鎖倉後的反向掛單冷卻時間（秒）
SYNC_TIME = 3  # 同步時間（秒）
ORDER_FIRST_TIME = 1  # 首單間隔時間
STRATEGY_THROTTLE_INTERVAL = 10

# ==================== 日志配置 ====================
script_name = os.path.splitext(os.path.basename(__file__))[0]
os.makedirs("log", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(f"log/{script_name}.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger()


class CustomGate(ccxt.gate):
    """自定義 Gate.io 交易所類，注入 Broker ID"""
    def __init__(self, config={}):
        # 1. 確保 options 中有 brokerId
        if 'options' not in config:
            config['options'] = {}
        config['options']['brokerId'] = 'voger'
        
        super().__init__(config)
        
        # 2. 強制設定全域 Headers
        if not self.headers:
            self.headers = {}
        self.headers['X-Gate-Channel-Id'] = 'voger'

    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        # 3. 雙重保險：在每次請求時再次檢查並注入
        headers['X-Gate-Channel-Id'] = 'voger'
        return super().fetch(url, method, headers, body)


class GridTradingBot:
    def __init__(self, api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, take_profit_spacing=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.coin_name = coin_name
        self.grid_spacing = grid_spacing
        self.take_profit_spacing = take_profit_spacing or grid_spacing
        self.initial_quantity = initial_quantity
        self.leverage = leverage
        self.exchange = self._initialize_exchange()
        self.ccxt_symbol = f"{coin_name}/USDT:USDT"
        self.ws_symbol = f"{coin_name}_USDT"
        self.price_precision = self._get_price_precision()

        self.long_initial_quantity = initial_quantity
        self.short_initial_quantity = initial_quantity
        self.long_position = 0
        self.short_position = 0
        self.last_long_order_time = 0
        self.last_short_order_time = 0
        self.buy_long_orders = 0
        self.sell_long_orders = 0
        self.sell_short_orders = 0
        self.buy_short_orders = 0
        self.last_position_update_time = 0
        self.last_orders_update_time = 0
        self.latest_price = 0
        self.best_bid_price = None
        self.best_ask_price = None
        self.balance = {}
        self.mid_price_long = 0
        self.lower_price_long = 0
        self.upper_price_long = 0
        self.mid_price_short = 0
        self.lower_price_short = 0
        self.upper_price_short = 0
        self.last_strategy_run_time = 0.0
    
    async def _ws_send_with_broker(self, websocket, channel, event, payload_list):
        """封裝 WebSocket 發送邏輯，加入 Broker req_header"""
        current_time = int(time.time())
        auth_msg = f"channel={channel}&event={event}&time={current_time}"
        sign = self._generate_sign(auth_msg)
        
        payload = {
            "time": current_time,
            "channel": channel,
            "event": event,
            "payload": payload_list,
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
            "req_header": {
                "X-Gate-Channel-Id": "voger" # 渠道碼
            }
        }
        await websocket.send(json.dumps(payload))

    def _initialize_exchange(self):
        """初始化交易所 API"""
        exchange = CustomGate({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "options": {
                "defaultType": "future",
                "brokerId": "voger"  # [修正點] 明確加入 brokerId
            },
        })
        # [修正點] 再次確保 header 存在 (雖 CustomGate 已做，但這裡加更保險)
        exchange.headers = {
            'X-Gate-Channel-Id': 'voger'
        }
        return exchange

    def _get_price_precision(self):
        """獲取交易對的價格精度"""
        markets = self.exchange.fetch_markets()
        symbol_info = next(market for market in markets if market["symbol"] == self.ccxt_symbol)
        return int(-math.log10(float(symbol_info["precision"]["price"])))

    def get_position(self):
        """獲取當前持倉"""
        params = {'settle': 'usdt', 'type': 'swap'}
        positions = self.exchange.fetch_positions(params=params)
        long_position = 0
        short_position = 0

        for position in positions:
            if position['symbol'] == self.ccxt_symbol:
                contracts = position.get('contracts', 0)
                side = position.get('side', None)
                if side == 'long':
                    long_position = contracts
                elif side == 'short':
                    short_position = abs(contracts)

        return long_position, short_position

    def check_orders_status(self):
        """檢查當前所有掛單的狀態"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)
        buy_long_orders_count = 0
        sell_long_orders_count = 0
        sell_short_orders_count = 0
        buy_short_orders_count = 0

        for order in orders:
            if not order.get('info') or 'left' not in order['info']:
                continue

            left_amount = abs(float(order['info'].get('left', '0')))

            if order.get('reduceOnly') and order.get('side') == 'sell' and order.get('status') == 'open':
                sell_long_orders_count = left_amount
            elif order.get('reduceOnly') and order.get('side') == 'buy' and order.get('status') == 'open':
                buy_short_orders_count = left_amount
            elif not order.get('reduceOnly') and order.get('side') == 'buy' and order.get('status') == 'open':
                buy_long_orders_count = left_amount
            elif not order.get('reduceOnly') and order.get('side') == 'sell' and order.get('status') == 'open':
                sell_short_orders_count = left_amount

        return buy_long_orders_count, sell_long_orders_count, sell_short_orders_count, buy_short_orders_count

    async def run(self):
        """啟動 WebSocket 監聽"""
        self.long_position, self.short_position = self.get_position()
        logger.info(f"初始化持倉: 多頭 {self.long_position} 張, 空頭 {self.short_position} 張")

        self.buy_long_orders, self.sell_long_orders, self.sell_short_orders, self.buy_short_orders = self.check_orders_status()
        logger.info(f"初始化掛單: 多頭開倉={self.buy_long_orders}, 多頭止盈={self.sell_long_orders}, "
                   f"空頭開倉={self.sell_short_orders}, 空頭止盈={self.buy_short_orders}")

        while True:
            try:
                await self.connect_websocket()
            except Exception as e:
                logger.error(f"WebSocket 連接失敗: {e}")
                await asyncio.sleep(5)

    async def connect_websocket(self):
        """連接 WebSocket 並訂閱數據"""
        async with websockets.connect(WEBSOCKET_URL) as websocket:
            # 呼叫封裝好的方法來訂閱，這會自動帶入 voger 渠道碼
            await self._ws_send_with_broker(websocket, "futures.tickers", "subscribe", [self.ws_symbol])
            await self._ws_send_with_broker(websocket, "futures.positions", "subscribe", [self.ws_symbol])
            await self._ws_send_with_broker(websocket, "futures.orders", "subscribe", [self.ws_symbol])
            await self._ws_send_with_broker(websocket, "futures.book_ticker", "subscribe", [self.ws_symbol])
            await self._ws_send_with_broker(websocket, "futures.balances", "subscribe", ["USDT"])

            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    channel = data.get("channel")

                    if channel == "futures.tickers":
                        await self.handle_ticker_update(message)
                    elif channel == "futures.positions":
                        await self.handle_position_update(message)
                    elif channel == "futures.orders":
                        await self.handle_order_update(message)
                    elif channel == "futures.book_ticker":
                        await self.handle_book_ticker_update(message)
                    elif channel == "futures.balances":
                        await self.handle_balance_update(message)
                except Exception as e:
                    logger.error(f"WebSocket 消息處理失敗: {e}")
                    break

    def _generate_sign(self, message):
        """生成 HMAC-SHA512 簽名"""
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha512).hexdigest()

    async def subscribe_balances(self, websocket):
        """訂閱餘額"""
        current_time = int(time.time())
        message = f"channel=futures.balances&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.balances",
            "event": "subscribe",
            "payload": ["USDT"],
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
        }
        await websocket.send(json.dumps(payload))

    async def subscribe_ticker(self, websocket):
        """訂閱 ticker"""
        current_time = int(time.time())
        message = f"channel=futures.tickers&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
        }
        await websocket.send(json.dumps(payload))

    async def subscribe_book_ticker(self, websocket):
        """訂閱 book_ticker"""
        current_time = int(time.time())
        message = f"channel=futures.book_ticker&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.book_ticker",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
        }
        await websocket.send(json.dumps(payload))

    async def subscribe_orders(self, websocket):
        """訂閱掛單"""
        current_time = int(time.time())
        message = f"channel=futures.orders&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.orders",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
        }
        await websocket.send(json.dumps(payload))

    async def subscribe_positions(self, websocket):
        """訂閱持倉"""
        current_time = int(time.time())
        message = f"channel=futures.positions&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.positions",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {"method": "api_key", "KEY": self.api_key, "SIGN": sign},
        }
        await websocket.send(json.dumps(payload))

    async def handle_balance_update(self, message):
        """處理餘額更新"""
        data = json.loads(message)
        if data.get("channel") == "futures.balances" and data.get("event") == "update":
            balances = data.get("result", [])
            for balance in balances:
                currency = balance.get("currency", "UNKNOWN")
                balance_amount = float(balance.get("balance", 0))
                change = float(balance.get("change", 0))
                self.balance[currency] = {"balance": balance_amount, "change": change}
                print(f"餘額更新: 幣種={currency}, 餘額={balance_amount}, 變化={change}")

    async def handle_ticker_update(self, message):
        """處理 ticker 更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            self.latest_price = float(data["result"][0]["last"])
            # print(f"最新價格: {self.latest_price:.8f}") # 可以註釋掉這行以減少終端輸出

            # --- 頻率控制：策略節流 (Throttling) 邏輯 START ---
            current_time = time.time()
            # 檢查是否已超過最小間隔
            if current_time - self.last_strategy_run_time < STRATEGY_THROTTLE_INTERVAL:
                return  # 間隔未到，跳過本次策略調整

            # 更新上次執行時間
            self.last_strategy_run_time = current_time
            # --- 頻率控制：策略節流 (Throttling) 邏輯 END ---

            # ... (以下為原本的同步邏輯) ...
            if time.time() - self.last_position_update_time > SYNC_TIME:
                self.long_position, self.short_position = self.get_position()
                self.last_position_update_time = time.time()
                print(f"同步 position: 多頭 {self.long_position}, 空頭 {self.short_position}")

            if time.time() - self.last_orders_update_time > SYNC_TIME:
                self.buy_long_orders, self.sell_long_orders, self.sell_short_orders, self.buy_short_orders = self.check_orders_status()
                self.last_orders_update_time = time.time()
                print(f"同步 orders: 多買 {self.buy_long_orders}, 多賣 {self.sell_long_orders}, 空賣 {self.sell_short_orders}, 空買 {self.buy_short_orders}")

            await self.adjust_grid_strategy()

    async def handle_book_ticker_update(self, message):
        """處理 book_ticker 更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            ticker = data["result"]
            if ticker:
                self.best_bid_price = float(ticker.get("b", 0))
                self.best_ask_price = float(ticker.get("a", 0))

    async def handle_position_update(self, message):
        """處理持倉更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            position_data = data["result"]
            if isinstance(position_data, list) and len(position_data) > 0:
                position = position_data[0]
                if position.get("mode") == "dual_long":
                    self.long_position = abs(float(position.get("size", 0)))
                    logger.info(f"更新多頭持倉: {self.long_position}")
                else:
                    self.short_position = abs(float(position.get("size", 0)))
                    logger.info(f"更新空頭持倉: {self.short_position}")

    async def handle_order_update(self, message):
        """處理掛單更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            order_data = data["result"]
            if isinstance(order_data, list) and len(order_data) > 0:
                for order in order_data:
                    if 'is_reduce_only' not in order or 'size' not in order:
                        continue

                    size = order.get('size', 0)
                    is_reduce_only = order.get('is_reduce_only', False)

                    if size > 0:
                        if is_reduce_only:
                            self.buy_short_orders = abs(order.get('left', 0))
                        else:
                            self.buy_long_orders = abs(order.get('left', 0))
                    else:
                        if is_reduce_only:
                            self.sell_long_orders = abs(order.get('left', 0))
                        else:
                            self.sell_short_orders = abs(order.get('left', 0))

    def get_take_profit_quantity(self, position, side):
        """調整止盈數量"""
        if side == 'long' and POSITION_LIMIT < position:
            self.long_initial_quantity = self.initial_quantity * 2
        elif side == 'short' and POSITION_LIMIT < position:
            self.short_initial_quantity = self.initial_quantity * 2
        else:
            self.long_initial_quantity = self.initial_quantity
            self.short_initial_quantity = self.initial_quantity

    async def initialize_long_orders(self):
        """初始化多頭掛單"""
        current_time = time.time()
        if current_time - self.last_long_order_time < ORDER_FIRST_TIME:
            return

        self.cancel_orders_for_side('long')
        mid_price = (self.best_bid_price + self.best_ask_price) / 2
        self.place_order('buy', mid_price, self.initial_quantity, False, 'long')
        logger.info(f"掛出多頭開倉單: 買入 @ {mid_price}")
        self.last_long_order_time = time.time()

    async def initialize_short_orders(self):
        """初始化空頭掛單"""
        current_time = time.time()
        if current_time - self.last_short_order_time < ORDER_FIRST_TIME:
            return

        self.cancel_orders_for_side('short')
        mid_price = (self.best_bid_price + self.best_ask_price) / 2
        self.place_order('sell', mid_price, self.initial_quantity, False, 'short')
        logger.info(f"掛出空頭開倉單: 賣出 @ {mid_price}")
        self.last_short_order_time = time.time()

    def cancel_orders_for_side(self, position_side):
        """撤銷某方向掛單"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)
        for order in orders:
            if position_side == 'long':
                if not order['reduceOnly'] and order['side'] == 'buy' and order['status'] == 'open':
                    self.cancel_order(order['id'])
                elif order['reduceOnly'] and order['side'] == 'sell' and order['status'] == 'open':
                    self.cancel_order(order['id'])
            elif position_side == 'short':
                if not order['reduceOnly'] and order['side'] == 'sell' and order['status'] == 'open':
                    self.cancel_order(order['id'])
                elif order['reduceOnly'] and order['side'] == 'buy' and order['status'] == 'open':
                    self.cancel_order(order['id'])

    def cancel_order(self, order_id):
        """撤單"""
        try:
            self.exchange.cancel_order(order_id, self.ccxt_symbol)
        except ccxt.BaseError as e:
            logger.error(f"撤單失敗: {e}")

    def place_order(self, side, price, quantity, is_reduce_only=False, position_side=None):
        """掛單"""
        try:
            params = {'reduce_only': is_reduce_only}
            self.exchange.create_order(self.ccxt_symbol, 'limit', side, quantity, price, params)
        except ccxt.BaseError as e:
            logger.error(f"下單報錯: {e}")

    def place_take_profit_order(self, ccxt_symbol, side, price, quantity):
        """掛止盈單"""
        try:
            if side == 'long':
                self.exchange.create_order(ccxt_symbol, 'limit', 'sell', quantity, price, {'reduce_only': True})
                logger.info(f"成功掛 long 止盈單: 賣出 {quantity} @ {price}")
            elif side == 'short':
                self.exchange.create_order(ccxt_symbol, 'limit', 'buy', quantity, price, {'reduce_only': True})
                logger.info(f"成功掛 short 止盈單: 買入 {quantity} @ {price}")
        except ccxt.BaseError as e:
            logger.error(f"掛止盈單失敗: {e}")

    async def place_long_orders(self, latest_price):
        """掛多頭訂單"""
        try:
            self.get_take_profit_quantity(self.long_position, 'long')
            if self.long_position > 0:
                if self.long_position > POSITION_THRESHOLD:
                    print(f"持倉{self.long_position}超過閾值 {POSITION_THRESHOLD}，long裝死")
                    if self.sell_long_orders <= 0:
                        r = float((int(self.long_position / max(self.short_position, 1)) / 100) + 1)
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.latest_price * r, self.long_initial_quantity)
                else:
                    self.update_mid_price('long', latest_price)
                    self.cancel_orders_for_side('long')
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.upper_price_long, self.long_initial_quantity)
                    self.place_order('buy', self.lower_price_long, self.long_initial_quantity, False, 'long')
                    logger.info(f"[多頭] 止盈@{self.upper_price_long:.4f} | 補倉@{self.lower_price_long:.4f}")
        except Exception as e:
            logger.error(f"掛多頭訂單失敗: {e}")

    async def place_short_orders(self, latest_price):
        """掛空頭訂單"""
        try:
            self.get_take_profit_quantity(self.short_position, 'short')
            if self.short_position > 0:
                if self.short_position > POSITION_THRESHOLD:
                    print(f"持倉{self.short_position}超過閾值 {POSITION_THRESHOLD}，short裝死")
                    if self.buy_short_orders <= 0:
                        r = float((int(self.short_position / max(self.long_position, 1)) / 100) + 1)
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.latest_price / r, self.short_initial_quantity)
                else:
                    self.update_mid_price('short', latest_price)
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.lower_price_short, self.short_initial_quantity)
                    self.place_order('sell', self.upper_price_short, self.short_initial_quantity, False, 'short')
                    logger.info(f"[空頭] 止盈@{self.lower_price_short:.4f} | 補倉@{self.upper_price_short:.4f}")
        except Exception as e:
            logger.error(f"掛空頭訂單失敗: {e}")

    def check_and_reduce_positions(self):
        """檢查並減倉"""
        local_threshold = int(POSITION_THRESHOLD * 0.8)
        reduce_qty = int(POSITION_THRESHOLD * 0.1)

        if self.long_position >= local_threshold and self.short_position >= local_threshold:
            logger.info(f"雙向持倉超過閾值，開始減倉")
            if self.long_position > 0:
                self.place_order('sell', self.latest_price, reduce_qty, True, 'long')
            if self.short_position > 0:
                self.place_order('buy', self.latest_price, reduce_qty, True, 'short')

    def update_mid_price(self, side, price):
        """更新中間價"""
        if side == 'long':
            self.mid_price_long = price
            self.upper_price_long = self.mid_price_long * (1 + self.take_profit_spacing)
            self.lower_price_long = self.mid_price_long * (1 - self.grid_spacing)
        elif side == 'short':
            self.mid_price_short = price
            self.upper_price_short = self.mid_price_short * (1 + self.grid_spacing)
            self.lower_price_short = self.mid_price_short * (1 - self.take_profit_spacing)

    async def adjust_grid_strategy(self):
        """調整網格策略"""
        self.check_and_reduce_positions()
        current_time = time.time()

        if self.long_position == 0:
            await self.initialize_long_orders()
        else:
            if not (0 < self.buy_long_orders <= self.long_initial_quantity) or not (0 < self.sell_long_orders <= self.long_initial_quantity):
                if self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
                    pass
                else:
                    await self.place_long_orders(self.latest_price)

        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (0 < self.sell_short_orders <= self.short_initial_quantity) or not (0 < self.buy_short_orders <= self.short_initial_quantity):
                if self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
                    pass
                else:
                    await self.place_short_orders(self.latest_price)


async def main():
    bot = GridTradingBot(
        API_KEY, API_SECRET, COIN_NAME,
        GRID_SPACING, INITIAL_QUANTITY, LEVERAGE,
        TAKE_PROFIT_SPACING
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
