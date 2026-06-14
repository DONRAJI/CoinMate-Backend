"""
CoinMate ML 분봉 모델 v4 — 공유 피처 엔지니어링 모듈
====================================================
⚠️ 이 파일이 Colab 학습 ↔ 서버 추론의 **단일 진실 공급원(single source of truth)**.
   양쪽이 이 함수를 그대로 import/사용하면 build_features 불일치가 구조적으로 불가능.

v3(현행 11피처) 대비 변경 — 세션27 정밀분석 근거:
  [문제] v3는 ma/macd/volume이 '원시 절대값'(BTC ma≈9300만 vs 잡코인≈2) → 코인 간 전이 불가
         → calibrated 확률이 기저율(~0.28)로 붕괴(std 0.05, 고유값 27, max 0.41). 변별력 ~0.
  [해결] ① 전 피처 상대값(비율/%) 정규화 → 코인 간 동일 스케일
         ② BTC 시장맥락 피처 추가 → "예측은 일정/실제는 시장따라 출렁" 해소
         ③ 진입타이밍(고점거리·거래량서지)·모멘텀 기울기 추가 → 진입직후 손절(4/5) 대응

입력:
  df      : 해당 코인 minute5 OHLCV (DatetimeIndex, columns: open/high/low/close/volume)
  btc_df  : KRW-BTC minute5 OHLCV (동일 인터벌). 시장맥락 피처용.
출력:
  FEATURE_COLUMNS_V4 순서의 pd.DataFrame (index=df.index). 데이터 부족 시 빈 DF.

워밍업: 24h(288봉) 롤링 사용 → 추론 시 minute5를 최소 ~350봉 확보할 것.
"""
import numpy as np
import pandas as pd

# ── 고정 피처 순서 (모델 feature_names와 1:1, 절대 순서 변경 금지) ──
FEATURE_COLUMNS_V4 = [
    # 가격 수익률 (상대)
    "ret_1", "ret_6", "ret_12",
    # 이동평균 이격/기울기 (상대)
    "ma5_dist", "ma20_dist", "ma60_dist", "ma5_slope", "ma20_slope",
    # 오실레이터/모멘텀
    "rsi", "rsi_slope", "macd_norm", "macd_hist_norm", "macd_hist_slope",
    # 변동성/범위 (상대)
    "volatility_pct", "atr_pct", "daily_range_pct",
    # 거래량 (상대)
    "vol_surge", "vol_ratio",
    # 위치 (24h 고저 대비)
    "dist_high_24h", "dist_low_24h",
    # 시장맥락 (BTC)
    "btc_ret_1h", "btc_ret_4h", "btc_ret_24h", "btc_ma_dist", "coin_btc_corr",
    # 시간
    "hour_sin", "hour_cos",
]

MIN_BARS = 300  # 288봉(24h) 롤링 워밍업 여유


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def build_features_v4(df: pd.DataFrame, btc_df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < MIN_BARS or btc_df is None or len(btc_df) < MIN_BARS:
        return pd.DataFrame()

    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    f = pd.DataFrame(index=df.index)

    # ── 수익률 (%) ──
    f["ret_1"] = close.pct_change(1) * 100
    f["ret_6"] = close.pct_change(6) * 100      # 30분
    f["ret_12"] = close.pct_change(12) * 100    # 1시간

    # ── 이동평균 이격/기울기 (%) ──
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    f["ma5_dist"] = (close / ma5 - 1) * 100
    f["ma20_dist"] = (close / ma20 - 1) * 100
    f["ma60_dist"] = (close / ma60 - 1) * 100
    f["ma5_slope"] = (ma5 / ma5.shift(6) - 1) * 100
    f["ma20_slope"] = (ma20 / ma20.shift(12) - 1) * 100

    # ── RSI / 기울기 ──
    rsi = _rsi(close, 14)
    f["rsi"] = rsi
    f["rsi_slope"] = rsi.diff(3)

    # ── MACD (종가로 정규화 → 코인 간 동일 스케일) ──
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    f["macd_norm"] = macd / close * 100
    f["macd_hist_norm"] = hist / close * 100
    f["macd_hist_slope"] = (hist / close * 100).diff(3)

    # ── 변동성/범위 (%) ──
    f["volatility_pct"] = close.rolling(20).std() / close * 100
    f["atr_pct"] = (high - low).rolling(14).mean() / close * 100
    f["daily_range_pct"] = (high - low) / close * 100

    # ── 거래량 (상대 비율) ──
    vol_ma20 = volume.rolling(20).mean()
    f["vol_surge"] = volume / vol_ma20.replace(0, 1e-9)
    f["vol_ratio"] = volume.rolling(5).mean() / vol_ma20.replace(0, 1e-9)

    # ── 24h 고저 대비 위치 (%) ──  0=고점, 음수=고점 아래 / 양수=저점 위
    f["dist_high_24h"] = (close / high.rolling(288).max() - 1) * 100
    f["dist_low_24h"] = (close / low.rolling(288).min() - 1) * 100

    # ── BTC 시장맥락 (index 정렬 후 계산) ──
    btc_close = btc_df["close"].reindex(df.index).ffill()
    f["btc_ret_1h"] = btc_close.pct_change(12) * 100
    f["btc_ret_4h"] = btc_close.pct_change(48) * 100
    f["btc_ret_24h"] = btc_close.pct_change(288) * 100
    f["btc_ma_dist"] = (btc_close / btc_close.rolling(288).mean() - 1) * 100  # BTC 레짐
    coin_ret = close.pct_change()
    btc_ret = btc_close.pct_change()
    f["coin_btc_corr"] = coin_ret.rolling(48).corr(btc_ret)  # 4h 상관

    # ── 시간 (순환 인코딩) ──
    hour = df.index.hour if hasattr(df.index, "hour") else pd.Series(0, index=df.index)
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24)

    f = f.replace([np.inf, -np.inf], np.nan)
    return f[FEATURE_COLUMNS_V4]
