"""
ML 예측 모델 (XGBoost)
- 과거 OHLCV + 기술적 지표로 "다음날 상승 확률" 예측
- 기존 앙상블 점수 위에 추가 필터로 사용
- 하루 1회 전 종목 데이터로 학습, 매수 판단 시 예측
"""
import os
import numpy as np
import pandas as pd
import joblib
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

MODEL_PATH = os.path.join(MODEL_DIR, "xgb_model.pkl")


class MLPredictor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self.initialized:
            return
        self.model = None
        self.feature_names = []
        self.is_trained = False
        self.train_score = 0
        self.train_date = None
        self._load_model()
        self.initialized = True

    def _load_model(self):
        """저장된 모델 로드"""
        try:
            if os.path.exists(MODEL_PATH):
                data = joblib.load(MODEL_PATH)
                self.model = data['model']
                self.feature_names = data['feature_names']
                self.is_trained = True
                self.train_score = data.get('score', 0)
                self.train_date = data.get('train_date', '')
                print(f">>> 🤖 [ML] 모델 로드 완료 (정확도: {self.train_score:.1f}%, 학습일: {self.train_date})")
        except Exception as e:
            print(f">>> ⚠️ [ML] 모델 로드 실패: {e}")

    def _save_model(self):
        """모델 저장"""
        try:
            joblib.dump({
                'model': self.model,
                'feature_names': self.feature_names,
                'score': self.train_score,
                'train_date': datetime.now(KST).strftime('%Y-%m-%d %H:%M'),
            }, MODEL_PATH)
        except Exception as e:
            print(f">>> ⚠️ [ML] 모델 저장 실패: {e}")

    def build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OHLCV DataFrame에서 ML 피처 생성
        Strategy에서 이미 계산하는 지표들을 재활용
        """
        if df is None or len(df) < 50:
            return pd.DataFrame()

        feat = pd.DataFrame(index=df.index)

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        open_p = df['open']

        # --- 가격 기반 ---
        feat['return_1d'] = close.pct_change(1) * 100
        feat['return_3d'] = close.pct_change(3) * 100
        feat['return_5d'] = close.pct_change(5) * 100
        feat['return_10d'] = close.pct_change(10) * 100

        # 이동평균 대비 위치
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        feat['ma5_ratio'] = (close / ma5 - 1) * 100
        feat['ma10_ratio'] = (close / ma10 - 1) * 100
        feat['ma20_ratio'] = (close / ma20 - 1) * 100
        feat['ma5_ma20_cross'] = (ma5 / ma20 - 1) * 100

        # --- RSI ---
        delta = close.diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/14, min_periods=14).mean()
        loss = -delta.where(delta < 0, 0).ewm(alpha=1/14, min_periods=14).mean()
        rs = gain / loss.replace(0, 0.0001)
        feat['rsi'] = 100 - (100 / (1 + rs))

        # --- MFI ---
        tp = (high + low + close) / 3
        mf = tp * volume
        pos_flow = pd.Series(0.0, index=df.index)
        neg_flow = pd.Series(0.0, index=df.index)
        tp_delta = tp.diff()
        pos_flow[tp_delta > 0] = mf[tp_delta > 0]
        neg_flow[tp_delta < 0] = mf[tp_delta < 0]
        pos_sum = pos_flow.rolling(14).sum()
        neg_sum = neg_flow.rolling(14).sum().replace(0, 0.0001)
        feat['mfi'] = 100 - (100 / (1 + pos_sum / neg_sum))

        # --- MACD ---
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        feat['macd_diff'] = macd_line - signal_line
        feat['macd_ratio'] = feat['macd_diff'] / close * 100

        # --- ADX ---
        up_move = high.diff()
        down_move = -low.diff()
        pdm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        mdm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        pdm_s = pd.Series(pdm, index=df.index).ewm(alpha=1/14, min_periods=14).mean()
        mdm_s = pd.Series(mdm, index=df.index).ewm(alpha=1/14, min_periods=14).mean()
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        tr_smooth = tr.ewm(alpha=1/14, min_periods=14).mean().replace(0, 0.0001)
        pdi = 100 * pdm_s / tr_smooth
        mdi = 100 * mdm_s / tr_smooth
        dx = (abs(pdi - mdi) / (pdi + mdi).replace(0, 0.0001)) * 100
        feat['adx'] = dx.ewm(alpha=1/14, min_periods=14).mean()
        feat['di_diff'] = pdi - mdi

        # --- 볼린저 밴드 ---
        bb_ma = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        feat['bb_position'] = (close - (bb_ma - 2 * bb_std)) / (4 * bb_std.replace(0, 0.0001))

        # --- 거래량 ---
        vol_ma20 = volume.rolling(20).mean().replace(0, 1)
        feat['vol_ratio'] = volume / vol_ma20
        feat['vol_change'] = volume.pct_change(1) * 100

        # --- ATR ---
        atr = tr.rolling(14).mean()
        feat['atr_ratio'] = atr / close * 100

        # --- 캔들 패턴 ---
        body = abs(close - open_p)
        total_range = (high - low).replace(0, 0.0001)
        feat['body_ratio'] = body / total_range
        feat['upper_wick'] = (high - pd.concat([close, open_p], axis=1).max(axis=1)) / total_range
        feat['is_bullish'] = (close > open_p).astype(int)

        # --- 변동성 ---
        feat['volatility_5d'] = close.pct_change().rolling(5).std() * 100
        feat['volatility_10d'] = close.pct_change().rolling(10).std() * 100

        # --- [추가] 시간 패턴 ---
        if hasattr(df.index, 'hour'):
            feat['hour'] = df.index.hour
        else:
            feat['hour'] = 12  # fallback

        # --- [추가] 가격 모멘텀 가속도 ---
        feat['momentum_accel'] = feat.get('return_1d', close.pct_change(1)*100).diff()

        # --- [추가] 거래량 가격 상관 (5일) ---
        feat['vol_price_corr'] = close.rolling(5).corr(volume)

        # --- [추가] 고가/저가 범위 비율 ---
        feat['hl_range_pct'] = ((high - low) / close) * 100

        # --- [추가] RSI 변화율 (모멘텀의 모멘텀) ---
        feat['rsi_change'] = feat['rsi'].diff()

        # --- [추가] 연속 양봉/음봉 수 ---
        is_up = (close > open_p).astype(int)
        groups = (is_up != is_up.shift()).cumsum()
        feat['consecutive_candles'] = is_up.groupby(groups).cumsum() - (1 - is_up).groupby(groups).cumsum()

        # NaN 제거
        feat = feat.replace([np.inf, -np.inf], np.nan)
        return feat

    def train(self, all_ohlcv_dict: dict):
        """
        전 종목 OHLCV로 학습
        all_ohlcv_dict: {ticker: DataFrame} 형태
        """
        try:
            from xgboost import XGBClassifier
            from sklearn.model_selection import TimeSeriesSplit

            print(f">>> 🤖 [ML] 학습 시작 ({len(all_ohlcv_dict)}개 종목)...")

            all_X = []
            all_y = []

            for ticker, df in all_ohlcv_dict.items():
                if df is None or len(df) < 60:
                    continue

                features = self.build_features(df)
                if features.empty:
                    continue

                # 정답: 다음날 종가가 오늘보다 높으면 1, 아니면 0
                y = (df['close'].shift(-1) > df['close']).astype(int)

                # 마지막 행(다음날 없음)과 NaN 행 제거
                valid_mask = features.notna().all(axis=1) & y.notna()
                features = features[valid_mask]
                y = y[valid_mask]

                if len(features) < 30:
                    continue

                all_X.append(features)
                all_y.append(y)

            if not all_X:
                print(">>> ⚠️ [ML] 학습 데이터 부족")
                return

            X = pd.concat(all_X, ignore_index=True)
            y = pd.concat(all_y, ignore_index=True)

            self.feature_names = list(X.columns)

            # 시계열 분할 (마지막 20%를 테스트)
            split_idx = int(len(X) * 0.8)
            X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
            y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

            model = XGBClassifier(
                n_estimators=300,
                max_depth=4,
                learning_rate=0.03,
                subsample=0.75,
                colsample_bytree=0.7,
                min_child_weight=10,
                gamma=0.2,
                reg_alpha=0.3,
                reg_lambda=2.0,
                scale_pos_weight=1.0,
                random_state=42,
                eval_metric='logloss',
                verbosity=0,
            )

            # Early stopping으로 과적합 방지
            model.fit(
                X_train, y_train,
                eval_set=[(X_test, y_test)],
                verbose=False,
            )

            train_acc = model.score(X_train, y_train) * 100
            test_acc = model.score(X_test, y_test) * 100

            self.model = model
            self.is_trained = True
            self.train_score = test_acc
            self.train_date = datetime.now(KST).strftime('%Y-%m-%d %H:%M')
            self._save_model()

            # 피처 중요도 상위 10개 출력
            importances = model.feature_importances_
            top_idx = np.argsort(importances)[-10:][::-1]
            top_features = [(self.feature_names[i], importances[i]) for i in top_idx]

            print(f">>> 🤖 [ML] 학습 완료!")
            print(f"    학습 데이터: {len(X_train)}행 / 테스트: {len(X_test)}행")
            print(f"    학습 정확도: {train_acc:.1f}% / 테스트 정확도: {test_acc:.1f}%")
            print(f"    상위 피처: {', '.join(f'{n}({v:.3f})' for n, v in top_features[:5])}")

        except Exception as e:
            print(f">>> ❌ [ML] 학습 실패: {e}")
            import traceback
            traceback.print_exc()

    def predict(self, df: pd.DataFrame) -> float:
        """
        단일 종목 DataFrame으로 상승 확률 예측
        Returns: 0.0 ~ 1.0 (상승 확률), 모델 없으면 0.5
        """
        if not self.is_trained or self.model is None:
            return 0.5

        try:
            features = self.build_features(df)
            if features.empty:
                return 0.5

            # 마지막 행(최신 데이터)만 사용
            last_row = features.iloc[[-1]]

            # NaN 처리
            if last_row.isna().any(axis=1).iloc[0]:
                last_row = last_row.fillna(0)

            # 피처 순서 맞추기
            last_row = last_row.reindex(columns=self.feature_names, fill_value=0)

            prob = self.model.predict_proba(last_row)[0][1]  # 상승 확률
            return float(prob)

        except Exception as e:
            print(f"⚠️ [ML Predict Error] {e}")
            return 0.5

    def get_status(self) -> dict:
        """모델 상태 반환 (API/프론트 표시용)"""
        return {
            "is_trained": self.is_trained,
            "accuracy": self.train_score,
            "train_date": self.train_date or "-",
            "features": len(self.feature_names),
        }
