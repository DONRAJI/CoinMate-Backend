from datetime import datetime
import pandas as pd
import numpy as np

class Strategy:
    def __init__(self):
        # 📊 [최종 업그레이드] 전략별 가중치 리밸런싱
        # 총점: 13.0점 만점
        self.WEIGHTS = {
            # --- [A] 배경 파악 (Trend & Power) ---
            "trend": 1.0,       # 20일 이평선 위 (기본)
            "adx": 1.0,         # [수정] 추세 강도 + 상승 방향 확인
            "volume": 1.0,      # [수정] 거래량 폭발 + 양봉 확인
            "vwap": 1.5,        # 세력 평단가 지지

            # --- [B] 진입 타이밍 (Timing & Reversal) ---
            "bollinger": 2.0,   # [수정] 밴드 하단 반등 + 양봉 확인
            "stoch": 1.5,       # [수정] 골든크로스 + 적정 구간(20~60)
            "cci": 1.5,         # 침체 구간(-100) 돌파

            # --- [C] 보조 필터 (Validation) ---
            "macd": 1.0,        # 추세 방향 확인
            "rsi": 1.0,         # 과매도 확인
            "mfi": 1.5          # 자금 흐름
        }
        
        # 매수 기준 점수: 13점 만점 중 6.0점 이상 (약 45% 이상의 지표가 동의할 때)
        self.THRESHOLD = 7.0 

    def get_ensemble_signal(self, df_day: pd.DataFrame, df_min: pd.DataFrame = None, debug=False):
        """
        일봉(Day)과 분봉(Min)을 종합 분석하여 매수 점수 산출
        """
        # --- 1. 데이터 유효성 검사 ---
        if df_day is None or len(df_day) < 30:
            if debug: print("⚠️ [Error] 일봉 데이터 부족")
            return None
            
        if df_min is None or len(df_min) < 30:
            if debug: print("⚠️ [Warning] 분봉 데이터 부족 -> 일봉으로 대체")
            df_min = df_day

        # --- 2. 일봉(Day) 분석 ---
        day_close = df_day['close']
        
        # (1) 추세: 20일 이동평균선
        ma20_day = day_close.rolling(window=20).mean().iloc[-1]
        current_price = day_close.iloc[-1]
        is_bull_market = current_price >= ma20_day
        
        # (2) ADX (추세 강도 + 방향)
        adx_signal = self._calc_adx(df_day)
        
        # (3) 거래량 (폭발 + 양봉)
        vol_signal = self._get_volume_signal(df_day)
        
        # --- 3. 분봉(Min) 분석 ---
        # [Tip] 분봉은 최소 15분봉 이상 권장
        closes = df_min['close']
        opens = df_min['open'] # [필수] 양봉 확인용
        lows = df_min['low']
        highs = df_min['high']
        volumes = df_min['volume']

        # 지표 산출
        rsi_series = self._calc_rsi_pandas(closes)
        mfi_series = self._calc_mfi_pandas(highs, lows, closes, volumes)
        atr_value = self._calc_atr_pandas(highs, lows, closes)
        
        # 신규 지표 계산
        cci_signal = self._calc_cci(highs, lows, closes)
        vwap_signal = self._calc_vwap_signal(df_min)
        
        # 기존 지표
        macd_score = self._calc_macd_score(closes)
        
        # [수정] 시가(opens) 전달 -> 양봉 체크
        bollinger_score = self._sig_bollinger(closes, opens) 
        
        stoch_signal = self._get_stochastic_signal(df_min)

        # 현재 값 추출
        rsi_value = rsi_series.iloc[-1]
        mfi_value = mfi_series.iloc[-1]
        
        # --- 4. 시그널 종합 ---
        signals = {
            "trend": 1 if is_bull_market else -1,
            "adx": adx_signal,
            "volume": vol_signal,
            "vwap": vwap_signal,
            "bollinger": bollinger_score,
            "stoch": 1 if stoch_signal else 0,
            "cci": cci_signal,
            "macd": macd_score,
            "rsi": self._eval_rsi(rsi_value),
            "mfi": self._eval_mfi(mfi_value)
        }

        # --- 5. 점수 계산 (Scoring) ---
        total_score = 0
        logs = []

        # (A) 하락장 패널티 (Risk Management)
        if not is_bull_market:
            score_change = -3.0 
            total_score += score_change
            if debug: logs.append(f"📉 [Trend] 하락 추세 (Price < 20MA) -> 패널티 {score_change}")

        # (B) 지표별 점수 합산
        for key, weight in self.WEIGHTS.items():
            signal = signals.get(key, 0)
            
            # RSI/CCI 과매도 부스트 (바닥 잡기)
            if key in ["rsi", "cci"] and signal == 1:
                score_change = weight + 0.5
                total_score += score_change
                if debug: logs.append(f"🔥 [{key.upper()}] 바닥 탈출 신호! (+{score_change})")
                
            # 일반 점수 합산
            elif signal == 1:
                total_score += weight
                if debug: logs.append(f"✅ [{key.upper()}] 긍정 신호 (+{weight})")
            
            # 매도 신호 차감
            elif signal == -1:
                deduction = weight * 0.5
                total_score -= deduction
                if debug: logs.append(f"🔻 [{key.upper()}] 부정 신호 (-{deduction})")

        # (C) 점수 보정
        final_score = round(max(0, total_score), 2)

        # --- 6. 목표가/손절가 (ATR 기반) ---
        target_price = current_price + (atr_value * 3.0) # 목표가 상향 (추세 추종)
        stop_loss_price = current_price - (atr_value * 1.5)
        
        # --- 디버그 출력 ---
        if debug:
            print("\n" + "="*60)
            print(f"📊 [{datetime.now().strftime('%H:%M:%S')}] 정밀 전략 분석 (현재가: {current_price:,.0f})")
            print("-" * 60)
            for log in logs:
                print(log)
            print("-" * 60)
            print(f" 🔍 RSI: {rsi_value:.1f} | MFI: {mfi_value:.1f} | ATR: {atr_value:.0f}")
            print(f" 🏆 최종 점수: {final_score} / 13.0 (기준: {self.THRESHOLD})")
            print(f" 🚦 판단: {'BUY 🚀' if final_score >= self.THRESHOLD else 'WAIT ✋'}")
            print("="*60 + "\n")

        return {
            "ticker": "UNKNOWN",
            "should_buy": final_score >= self.THRESHOLD,
            "score": final_score,
            "current_price": float(current_price),
            "target_price": round(target_price, 0),
            "stop_loss_price": round(stop_loss_price, 0),
            "atr": round(atr_value, 0),
            "strategies": signals,
            "rsi": float(rsi_value),
            "mfi": float(mfi_value)
        }

    # =========================================================
    #  Logic Methods (Indicators)
    # =========================================================

    def _calc_adx(self, df, n=14):
        """[수정됨] ADX: 추세 강도(20이상) AND 상승 추세(PDI > MDI) 확인"""
        if len(df) < n * 2: return 0
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        up_move = high.diff()
        down_move = -low.diff()
        
        pdm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        mdm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        pdm = pd.Series(pdm, index=df.index)
        mdm = pd.Series(mdm, index=df.index)
        
        tr = self._calc_atr_series(high, low, close)
        
        tr_smooth = tr.ewm(alpha=1/n, min_periods=n).mean().replace(0, 0.0001)
        pdm_smooth = pdm.ewm(alpha=1/n, min_periods=n).mean()
        mdm_smooth = mdm.ewm(alpha=1/n, min_periods=n).mean()
        
        pdi = 100 * (pdm_smooth / tr_smooth)
        mdi = 100 * (mdm_smooth / tr_smooth)
        
        div = (pdi + mdi).replace(0, 0.0001)
        dx = (abs(pdi - mdi) / div) * 100
        adx = dx.ewm(alpha=1/n, min_periods=n).mean()
        
        curr_adx = adx.iloc[-1]
        curr_pdi = pdi.iloc[-1]
        curr_mdi = mdi.iloc[-1]
        
        # [핵심 수정] 추세가 강하면서(20↑) + 매수세가 우위(PDI > MDI)일 때만
        if curr_adx >= 20 and curr_pdi > curr_mdi:
            return 1
        return 0

    def _get_volume_signal(self, df):
        """[수정됨] 거래량 폭발 AND 양봉(Close > Open) 확인"""
        volume = df['volume']
        close = df['close']
        open_p = df['open']
        
        if len(volume) < 20: return 0
        
        vol_ma20 = volume.rolling(20).mean().iloc[-1]
        curr_vol = volume.iloc[-1]
        
        # 거래량 급증 (1.5배)
        is_explosive = curr_vol > (vol_ma20 * 1.5)
        # 양봉 확인
        is_bullish = close.iloc[-1] > open_p.iloc[-1]
        
        if is_explosive and is_bullish:
            return 1
        return 0

    def _get_stochastic_signal(self, df, n=14, k=3):
        """[수정됨] 골든크로스 AND 적정 구간(20~60) 진입"""
        if len(df) < n: return False
        
        low_min = df['low'].rolling(n).min()
        high_max = df['high'].rolling(n).max()
        denominator = (high_max - low_min).replace(0, 0.0001)
        
        fast_k = ((df['close'] - low_min) / denominator) * 100
        slow_k = fast_k.rolling(k).mean()
        slow_d = slow_k.rolling(k).mean()
        
        if pd.isna(slow_k.iloc[-1]) or pd.isna(slow_d.iloc[-1]): return False
        
        curr_k = slow_k.iloc[-1]
        curr_d = slow_d.iloc[-1]
        
        # [핵심 수정] 80 근처 고점 추격 매수 방지 (20 <= k <= 60)
        return (curr_k > curr_d) and (20 <= curr_k <= 60)

    def _sig_bollinger(self, closes, opens, period=20, k=2, threshold=1.02):
        """[수정됨] 밴드 하단 터치 + 양봉 반등 확인"""
        if len(closes) < period: return 0
            
        ma = closes.rolling(period).mean()
        std = closes.rolling(period).std()
        upper = ma + (std * k)
        lower = ma - (std * k)
        
        curr_price = closes.iloc[-1]
        curr_open = opens.iloc[-1]
        prev_price = closes.iloc[-2]
        
        curr_lower = lower.iloc[-1]
        curr_upper = upper.iloc[-1]
        
        if np.isnan(curr_lower) or np.isnan(curr_upper): return 0

        # 조건 A: 하단 밴드 근처
        is_near_lower = curr_price <= (curr_lower * threshold)
        
        # 조건 B: 반등 (전봉 종가보다 상승 AND 양봉)
        is_rebounding = (curr_price > prev_price) and (curr_price >= curr_open)
        
        if is_near_lower and is_rebounding:
            return 1
        if curr_price >= curr_upper:
            return -1
        return 0

    def _calc_cci(self, highs, lows, closes, period=20):
        """CCI: -100 상향 돌파 시 매수"""
        tp = (highs + lows + closes) / 3
        ma = tp.rolling(period).mean()
        mad = (tp - ma).abs().rolling(period).mean().replace(0, 0.0001)
        
        cci = (tp - ma) / (0.015 * mad)
        
        if len(cci) < 2: return 0
        
        prev_cci = cci.iloc[-2]
        curr_cci = cci.iloc[-1]
        
        if prev_cci < -100 and curr_cci > -100:
            return 1
        return 0

    def _calc_vwap_signal(self, df):
        """VWAP: 현재가가 VWAP 위에 있을 때"""
        if 'volume' not in df.columns: return 0
        
        v = df['volume']
        tp = (df['high'] + df['low'] + df['close']) / 3
        
        cum_vol = v.cumsum().replace(0, 1)
        cum_vol_price = (tp * v).cumsum()
        
        vwap = cum_vol_price / cum_vol
        
        curr_price = df['close'].iloc[-1]
        curr_vwap = vwap.iloc[-1]
        
        if curr_price > curr_vwap:
            return 1
        return 0

    def _calc_atr_series(self, high, low, close):
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low, 
            (high - prev_close).abs(), 
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        return tr

    def _calc_atr_pandas(self, highs, lows, closes, period=14):
        tr = self._calc_atr_series(highs, lows, closes)
        return tr.rolling(period).mean().iloc[-1] if not pd.isna(tr.iloc[-1]) else 0

    def _calc_macd_score(self, closes):
        exp1 = closes.ewm(span=12, adjust=False).mean()
        exp2 = closes.ewm(span=26, adjust=False).mean()
        macd_line = exp1 - exp2
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        
        curr = macd_line.iloc[-1]
        sig = signal_line.iloc[-1]
        prev = macd_line.iloc[-2]
        prev_sig = signal_line.iloc[-2]

        if prev <= prev_sig and curr > sig: return 1 
        elif curr > sig: return 1 
        elif curr < sig: return -1 
        return 0

    def _calc_rsi_pandas(self, closes, period=14):
        delta = closes.diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/period, min_periods=period).mean()
        loss = -delta.where(delta < 0, 0).ewm(alpha=1/period, min_periods=period).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = 100 - (100 / (1 + rs))
        return rsi.ffill().fillna(50)

    def _calc_mfi_pandas(self, highs, lows, closes, volumes, period=14):
        tp = (highs + lows + closes) / 3
        mf = tp * volumes
        pos_flow = pd.Series(0.0, index=closes.index)
        neg_flow = pd.Series(0.0, index=closes.index)
        delta = tp.diff()
        
        pos_flow[delta > 0] = mf[delta > 0]
        neg_flow[delta < 0] = mf[delta < 0]
        
        pos_sum = pos_flow.rolling(period).sum()
        neg_sum = neg_flow.rolling(period).sum().replace(0, 0.0001)
        
        mfi = 100 - (100 / (1 + (pos_sum / neg_sum)))
        return mfi.fillna(50)

    def _eval_rsi(self, rsi_val):
        if rsi_val < 30: return 1
        if rsi_val > 70: return -1
        return 0

    def _eval_mfi(self, mfi_val):
        if mfi_val < 20: return 1
        if mfi_val > 80: return -1
        return 0