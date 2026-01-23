from datetime import datetime
import pandas as pd
import numpy as np

class Strategy:
    def __init__(self):
        # 📊 [리밸런싱] 지표 간 상관관계를 고려한 가중치 재설정
        # 총점: 12.0점 만점
        self.WEIGHTS = {
            # --- [A] 추세 그룹 (Trend & Momentum) ---
            # 가격이 20MA 위에 있는가? (가장 중요)
            "trend": 3.0,       
            # 추세의 강도가 센가?
            "adx": 1.5,         
            
            # --- [B] 수급 그룹 (Volume & VWAP) ---
            # 거래량이 터졌는가?
            "volume": 1.0,      
            # 세력 평단가 위에 있는가?
            "vwap": 1.5,        

            # --- [C] 반전/타이밍 그룹 (Oscillators) ---
            # RSI, MFI만 사용해 중복을 줄이고 신뢰도 높은 신호에 집중합니다.
            # 이 그룹은 내부적으로 평균을 내어 최대 3.0점만 반영합니다.
            "oscillator_group": 3.0, 
            
            # --- [D] 변동성 그룹 (Volatility) ---
            # 볼린저 밴드 하단 반등 (역추세 매매 핵심)
            "bollinger": 2.0,   
        }
        
        # 매수 기준: 7.0 (확실할 때 진입)
        # 매도 기준: TradeManager에서 3.5 미만일 때 매도로 처리됨
        self.BUY_THRESHOLD = 7.0 

    def get_ensemble_signal(self, df_day: pd.DataFrame, df_min: pd.DataFrame = None, debug=False):
        """
        일봉(Day)과 분봉(Min)을 종합 분석하여 매수 점수 산출
        """
        # --- 1. 데이터 유효성 검사 ---
        if df_day is None or len(df_day) < 30:
            return None
        if df_min is None or len(df_min) < 30:
            df_min = df_day

        # --- 2. 지표 계산 ---
        # (1) 일봉 분석
        day_close = df_day['close']
        ma20_day = day_close.rolling(window=20).mean().iloc[-1]
        current_price = day_close.iloc[-1]
        
        # 추세 신호 (Trend): 패널티 방식 삭제 -> 점수 획득 방식으로 변경
        is_bull_market = current_price >= ma20_day
        adx_signal = self._calc_adx(df_day)
        vol_signal = self._get_volume_signal(df_day)

        # (2) 분봉 분석
        closes = df_min['close']
        opens = df_min['open']
        highs = df_min['high']
        lows = df_min['low']
        volumes = df_min['volume']

        # 개별 오실레이터 계산
        rsi_val = self._calc_rsi_pandas(closes).iloc[-1]
        mfi_val = self._calc_mfi_pandas(highs, lows, closes, volumes).iloc[-1]
        # 기타 지표
        vwap_signal = self._calc_vwap_signal(df_min)
        bollinger_score = self._sig_bollinger(closes, opens)
        atr_value = self._calc_atr_pandas(highs, lows, closes)
        
        # 기존 MACD 계산 (참고용)
        macd_score = self._calc_macd_score(closes)

        # --- 3. 오실레이터 그룹 점수 통합 (핵심 변경 사항) ---
        # RSI/MFI만 사용하여 중복 신호를 줄이고 평균으로 그룹 점수를 계산합니다.
        # 각각 1점(긍정), 0점(중립), -1점(부정) 부여 후 평균 계산
        osc_scores = []
        
        # RSI (35이하 매수, 65이상 매도 - 기준 약간 완화)
        if rsi_val < 35: osc_scores.append(1) 
        elif rsi_val > 65: osc_scores.append(-1)
        else: osc_scores.append(0)

        # MFI (25이하 매수, 80이상 매도)
        if mfi_val < 25: osc_scores.append(1)
        elif mfi_val > 80: osc_scores.append(-1)
        else: osc_scores.append(0)

        # 오실레이터 종합 점수 (-1.0 ~ 1.0 사이의 비율)
        # 예: 2개 중 2개가 좋으면 1.0, 1개만 좋으면 0.5
        osc_ratio = sum(osc_scores) / len(osc_scores) if osc_scores else 0
        
        # 최종 점수에 반영될 오실레이터 점수 (최대 3.0점)
        final_osc_score = osc_ratio * self.WEIGHTS["oscillator_group"]

        # --- 4. 최종 점수 계산 ---
        total_score = 0
        logs = []
        
        # 개별 전략 신호 맵 (디버깅/UI 표시용)
        strategies_map = {
            "trend": 1 if is_bull_market else -1,
            "adx": adx_signal,
            "volume": vol_signal,
            "vwap": vwap_signal,
            "bollinger": bollinger_score,
            "macd": macd_score,
            "rsi": self._eval_rsi(rsi_val),
            "mfi": self._eval_mfi(mfi_val),
        }

        # (A) 추세 (Trend): 3.0점
        if is_bull_market:
            total_score += self.WEIGHTS["trend"]
            if debug: logs.append(f"✅ [Trend] 상승 추세 (+{self.WEIGHTS['trend']})")
        else:
            # 패널티를 주는 대신 점수를 안 줌 (0점) -> 급격한 점수 하락 방지
            if debug: logs.append(f"📉 [Trend] 하락 추세 (0.0)")

        # (B) ADX
        if adx_signal:
            total_score += self.WEIGHTS["adx"]
            if debug: logs.append(f"✅ [ADX] 강한 추세 (+{self.WEIGHTS['adx']})")

        # (C) 거래량 & VWAP
        if vol_signal: total_score += self.WEIGHTS["volume"]
        if vwap_signal: total_score += self.WEIGHTS["vwap"]

        # (D) 오실레이터 그룹 (통합 점수)
        if final_osc_score > 0:
            total_score += final_osc_score
            if debug: logs.append(f"✅ [Oscillators] 바닥/반전 신호 종합 (+{final_osc_score:.2f})")
        elif final_osc_score < 0:
            # 매도 신호가 강할 경우 점수 차감 (절반 정도만 반영)
            deduction = abs(final_osc_score) * 0.5 
            total_score -= deduction
            if debug: logs.append(f"🔻 [Oscillators] 과열/매도 신호 종합 (-{deduction:.2f})")

        # (E) 볼린저 밴드 (역추세 매매의 핵심)
        if bollinger_score == 1:
            total_score += self.WEIGHTS["bollinger"]
            if debug: logs.append(f"🔥 [Bollinger] 반등 유력 (+{self.WEIGHTS['bollinger']})")
        elif bollinger_score == -1: # 상단 터치
            total_score -= 1.0 # 소폭 차감

        final_score = round(max(0, total_score), 2)

        # --- 5. 목표가/손절가 ---
        target_price = current_price + (atr_value * 3.0)
        stop_loss_price = current_price - (atr_value * 2.0) # 손절 여유 좀 더 줌

        # --- 디버그 출력 ---
        if debug:
            print("\n" + "="*60)
            print(f"📊 [{datetime.now().strftime('%H:%M:%S')}] 정밀 전략 분석 (현재가: {current_price:,.0f})")
            print("-" * 60)
            for log in logs:
                print(log)
            print("-" * 60)
            print(f" 🔍 RSI: {rsi_val:.1f} | MFI: {mfi_val:.1f} | Osc_Ratio: {osc_ratio:.2f}")
            print(f" 🏆 최종 점수: {final_score} / 12.0 (매수 기준: {self.BUY_THRESHOLD})")
            print(f" 🚦 판단: {'BUY 🚀' if final_score >= self.BUY_THRESHOLD else 'WAIT ✋'}")
            print("="*60 + "\n")

        return {
            "should_buy": final_score >= self.BUY_THRESHOLD,
            "score": final_score,
            "current_price": float(current_price),
            "target_price": round(target_price, 0),
            "stop_loss_price": round(stop_loss_price, 0),
            "atr": round(atr_value, 0),
            "rsi": float(rsi_val),
            "mfi": float(mfi_val),
            "strategies": strategies_map,
            "score_breakdown": logs
        }

    # =========================================================
    #  Logic Methods (Indicators) - 기존 코드 유지
    # =========================================================

    def _calc_adx(self, df, n=14):
        """ADX: 추세 강도(20이상) AND 상승 추세(PDI > MDI) 확인"""
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
        
        if curr_adx >= 20 and curr_pdi > curr_mdi:
            return 1
        return 0

    def _get_volume_signal(self, df):
        """거래량 폭발 AND 양봉(Close > Open) 확인"""
        volume = df['volume']
        close = df['close']
        open_p = df['open']
        
        if len(volume) < 20: return 0
        
        vol_ma20 = volume.rolling(20).mean().iloc[-1]
        curr_vol = volume.iloc[-1]
        
        is_explosive = curr_vol > (vol_ma20 * 1.5)
        is_bullish = close.iloc[-1] > open_p.iloc[-1]
        
        if is_explosive and is_bullish:
            return 1
        return 0

    def _sig_bollinger(self, closes, opens, period=20, k=2, threshold=1.02):
        """밴드 하단 터치 + 양봉 반등 확인"""
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
