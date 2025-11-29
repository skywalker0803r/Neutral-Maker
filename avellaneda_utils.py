# avellaneda_utils.py
import pandas as pd
import numpy as np
import requests
import math
import logging

# 設置日誌
logger = logging.getLogger('AvellanedaBot')
# 確保日誌在 utils 檔案中也能正常顯示
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Gate.io K 線資料抓取 ---
def get_gateio_kline(currency_pair: str, interval: str = "1h", limit: int = 720) -> pd.DataFrame:
    """
    從 Gate.io API 取得歷史 K 線資料
    """
    try:
        base_url = "https://api.gateio.ws/api/v4/spot/candlesticks"
        params = {
            "currency_pair": currency_pair.upper(),
            "interval": interval,
            "limit": limit
        }

        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data, columns=[
            "timestamp", "volume_quote", "close", "high", "low", "open", "volume_base", "closed"
        ])
        if df.empty:
             return df
             
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="s", utc=True)
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
        
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df[["timestamp", "open", "high", "low", "close"]]
        
    except requests.RequestException as e:
        logger.error(f"獲取 Gate.io K 線資料失敗: {e}")
        return pd.DataFrame()


def calculate_historical_volatility(df: pd.DataFrame) -> float:
    """計算小時歷史波動率 (AVE_SIGMA)"""
    if df.empty or len(df) < 2:
        logger.warning("K線數據不足，無法計算波動率。")
        return 0.0

    # 計算對數收益率 Ri = ln(Pi / P(i-1))
    df['log_return'] = np.log(df['close'] / df['close'].shift(1))
    
    # 計算對數收益率的標準差 (即小時波動率)
    volatility = df['log_return'].std()
    
    return volatility if not math.isnan(volatility) else 0.0

def estimate_eta_from_fee(taker_fee_rate: float) -> float:
    """
    基於交易費率的簡化 Eta 估算。
    Eta = K_calib / Taker_Fee_Rate (啟發式)
    """
    # K_calib: 校準常數，可根據交易對和市場深度調整。
    K_calib = 0.05 
    
    if taker_fee_rate > 0:
        estimated_eta = K_calib / taker_fee_rate
    else:
        estimated_eta = 500.0 # 零費率或異常時使用高值

    return estimated_eta

def auto_calculate_params(coin: str, taker_fee: float) -> tuple[float, float]:
    """執行參數自動計算與推算，並返回 sigma, eta"""
    
    currency_pair = f"{coin}_USDT"
    
    # 1. 獲取 K 線數據 (預設 720小時 ≈ 30天)
    kline_df = get_gateio_kline(currency_pair, interval="1h", limit=720)
    
    # 2. 計算波動率 (AVE_SIGMA)
    AVE_SIGMA = calculate_historical_volatility(kline_df)
    
    # 設置安全默認值
    if AVE_SIGMA < 1e-5:
        AVE_SIGMA = 0.005
        logger.warning(f"波動率計算結果過小或失敗，使用預設值 {AVE_SIGMA}")

    # 3. 估算交易成本係數 (AVE_ETA)
    AVE_ETA = estimate_eta_from_fee(taker_fee)

    logger.info(f"--- Avellaneda 參數推算結果 ---")
    logger.info(f"使用 {len(kline_df)} 個小時數據")
    logger.info(f"AVE_SIGMA (小時波動率): {AVE_SIGMA:.8f}")
    logger.info(f"AVE_ETA (交易成本係數): {AVE_ETA:.2f} (基於 Taker Fee: {taker_fee:.4%})")
    logger.info(f"---------------------------------")

    return AVE_SIGMA, AVE_ETA