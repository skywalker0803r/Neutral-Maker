import asyncio
import time
import math
import logging
import os
# 假設 GridTradingBot 和所有必要的常量、logger 都從 bot.py 導入
from bot import GridTradingBot, logger 
from avellaneda_utils import auto_calculate_params

# ==================== Avellaneda 參數配置 (動態獲取) ====================
# 【重要】這些參數將在 main 函數中被 auto_calculate_params 的結果覆蓋
AVE_GAMMA = 1.0       # 風險厭惡係數 (固定值)
AVE_T_END = 1         # 交易時間週期 (T, 調整為 1 小時, 固定值)
Taker_Fee_Rate = 0.0005 # <-- 【請在此處設置您的 Taker 費率】
AVE_SIGMA = 0.0       # <--- 初始為 0，將被計算值覆蓋
AVE_ETA = 0.0         # <--- 初始為 0，將被計算值覆蓋

# 假設 bot.py 中的核心配置
API_KEY = "" 
API_SECRET = ""
COIN_NAME = "XRP" 
GRID_SPACING = 0.0006
TAKE_PROFIT_SPACING = 0.0004
INITIAL_QUANTITY = 1
LEVERAGE = 20
POSITION_THRESHOLD = 500
ORDER_COOLDOWN_TIME = 60 

# ==================== Avellaneda 繼承類 (保持不變) ====================
class AvellanedaGridBot(GridTradingBot):
    
    def __init__(self, api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, 
                 take_profit_spacing=None, gamma=AVE_GAMMA, eta=AVE_ETA, sigma=AVE_SIGMA, T_end=AVE_T_END):
        
        # 1. 呼叫父類別的初始化方法
        super().__init__(api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, take_profit_spacing)
        
        # 2. 初始化 Avellaneda 專有參數
        # 這裡使用的是 main 函數計算後的最新全局變量值
        self.gamma = gamma          # 風險厭惡係數
        self.eta = eta              # 交易成本係數
        self.sigma = sigma          # 波動率估計
        self.T_end = T_end          # 交易總時間 (單位：小時)
        self.reserve_price = 0      
        self.inventory = 0          
        self.best_bid = 0           
        self.best_ask = 0           
        logger.info(f"Avellaneda Bot 初始化: Gamma={gamma}, Eta={eta:.2f}, Sigma={sigma:.8f}")
    
    def _calculate_avellaneda_prices(self, price):
        """
        [輔助方法] 計算 Avellaneda 模型下的公允價格和最佳報價
        """
        
        # 1. 更新庫存 (淨持倉量)
        self.inventory = self.long_position - self.short_position
        
        # 2. 剩餘時間 T
        T = self.T_end
        
        # 3. 公允價格 (Reserve Price) 計算: R = S - q * gamma * sigma^2 * T
        # S = 當前市場價格 (price)
        self.reserve_price = price - self.inventory * self.gamma * (self.sigma**2) * T

        # 4. 最優報價寬度 (delta) 計算: Delta = 1/2 * gamma * sigma^2 * T + 1/gamma * ln(1 + gamma / eta)
        try:
            term1 = 0.5 * self.gamma * (self.sigma**2) * T
            term2 = (1 / self.gamma) * math.log(1 + self.gamma / self.eta)
            delta = term1 + term2
        except (ValueError, ZeroDivisionError) as e:
            logger.error(f"Delta 計算異常: {e}. 使用備用 Delta.")
            delta = self.grid_spacing * price * 0.5 # 使用基於價格的網格備用 Delta
            
        # 5. 計算最佳報價
        self.best_bid = self.reserve_price - delta
        self.best_ask = self.reserve_price + delta
        
        # 價格保護
        self.best_bid = max(0.0, self.best_bid) 
        self.best_ask = max(0.0, self.best_ask) 
        
        logger.info(f"Avellaneda: R={self.reserve_price:.8f}, Inv={self.inventory:.2f}, Delta={delta:.8f}")
        
    
    def update_mid_price(self, side, price):
        self._calculate_avellaneda_prices(price)
        self.upper_price_long = self.upper_price_short = self.best_ask
        self.lower_price_long = self.lower_price_short = self.best_bid
        self.mid_price_long = self.mid_price_short = self.reserve_price


    async def place_long_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出多頭開倉和止盈單。"""
        try:
            self.update_mid_price('long', latest_price) 
            self.get_take_profit_quantity(self.long_position, 'long')

            if self.long_position > 0:
                if self.long_position > POSITION_THRESHOLD:
                    if self.sell_long_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                else:
                    self.cancel_orders_for_side('long')
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                    self.place_order('buy', self.best_bid, self.long_initial_quantity, False, 'long')
                    
                    logger.info(f"[A-Long] 止盈@{self.best_ask:.8f} | 補倉@{self.best_bid:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 多頭訂單失敗: {e}")

    async def place_short_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出空頭開倉和止盈單。"""
        try:
            self.update_mid_price('short', latest_price) 
            self.get_take_profit_quantity(self.short_position, 'short')

            if self.short_position > 0:
                if self.short_position > POSITION_THRESHOLD:
                    if self.buy_short_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                else:
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                    self.place_order('sell', self.best_ask, self.short_initial_quantity, False, 'short')
                    
                    logger.info(f"[A-Short] 止盈@{self.best_bid:.8f} | 補倉@{self.best_ask:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 空頭訂單失敗: {e}")
            

    async def adjust_grid_strategy(self):
        self.check_and_reduce_positions()
        current_time = time.time()
        latest_price = self.latest_price
        
        if latest_price:
            self.update_mid_price(None, latest_price) 

        if self.long_position == 0:
            await self.initialize_long_orders()
        else:
            if not (self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME):
                await self.place_long_orders(latest_price)

        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME):
                await self.place_short_orders(latest_price)


# 7. 主程序入口
async def main():
    # 步驟 1: 自動計算 Avellaneda 參數，並覆蓋全局變量
    global AVE_SIGMA, AVE_ETA
    AVE_SIGMA, AVE_ETA = auto_calculate_params(COIN_NAME, Taker_Fee_Rate)

    # 步驟 2: 實例化機器人，使用計算後的參數
    bot = AvellanedaGridBot(
        API_KEY, API_SECRET, COIN_NAME,
        GRID_SPACING, INITIAL_QUANTITY, LEVERAGE,
        TAKE_PROFIT_SPACING,
        gamma=AVE_GAMMA, eta=AVE_ETA, sigma=AVE_SIGMA, T_end=AVE_T_END
    )
    
    # 步驟 3: 運行機器人
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("機器人已由用戶停止。")
    except Exception as e:
        logger.critical(f"主程序發生致命錯誤: {e}")