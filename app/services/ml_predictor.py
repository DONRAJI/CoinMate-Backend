"""
ML 예측 모델 (XGBoost) v2
- 과거 OHLCV + 기술적 지표로 "의미 있는 상승(1%+) 확률" 예측
- 기존 앙상블 점수 위에 추가 필터로 사용
- 하루 1회 전 종목 데이터로 학습, 매수 판단 시 예측
- v2: 레이블 개선 (1%+ 상승), 피처 추가 (38개), 클래스 균형 처리
"""
import os
import json
import time
import shutil
import numpy as np
import pandas as pd
import joblib
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

MODEL_PATH = os.path.join(MODEL_DIR, "xgb_model.pkl")

# 🔥 [P1] 모델 버전 관리
VERSIONS_DIR = os.path.join(MODEL_DIR, "versions")
if not os.path.exists(VERSIONS_DIR):
    os.makedirs(VERSIONS_DIR)
HISTORY_PATH = os.path.join(MODEL_DIR, "model_history.json")
MAX_VERSIONS = 10  # 보관할 최대 버전 수

# 레이블 임계값: 다음날 고가 기준 이 이상 상승하면 1
LABEL_THRESHOLD_PCT = 1.0


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
        self.calibrator = None  # [개선] Isotonic regression calibrator
        self.feature_names = []
        self.is_trained = False
        self.train_score = 0
        self.train_date = None
        self._load_model()
        self.initialized = True

    def _load_model(self):
        """저장된 모델 로드 (calibrator 후방호환: 옛 모델은 None)"""
        try:
            if os.path.exists(MODEL_PATH):
                data = joblib.load(MODEL_PATH)
                self.model = data['model']
                self.feature_names = data['feature_names']
                self.calibrator = data.get('calibrator')  # 옛 모델엔 없음
                self.is_trained = True
                self.train_score = data.get('score', 0)
                self.train_date = data.get('train_date', '')
                cal_msg = " + calibrated" if self.calibrator is not None else " (uncalibrated)"
                print(f">>> 🤖 [ML] 모델 로드 완료 (정확도: {self.train_score:.1f}%, 학습일: {self.train_date}{cal_msg})")
        except Exception as e:
            print(f">>> ⚠️ [ML] 모델 로드 실패: {e}")

    def _save_model(self):
        """모델 저장 + 날짜별 버전 보관 (롤백 대비)"""
        try:
            payload = {
                'model': self.model,
                'calibrator': self.calibrator,  # [개선] Isotonic calibrator
                'feature_names': self.feature_names,
                'score': self.train_score,
                'train_date': datetime.now(KST).strftime('%Y-%m-%d %H:%M'),
            }
            joblib.dump(payload, MODEL_PATH)

            # 버전 보관 (재직렬화 대신 복사)
            ver_name = f"xgb_model_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.pkl"
            shutil.copy2(MODEL_PATH, os.path.join(VERSIONS_DIR, ver_name))
            self._append_history(ver_name, payload['train_date'], self.train_score, len(self.feature_names))
            self._prune_versions()
            print(f">>> 💾 [ML] 모델 저장 + 버전 보관: {ver_name}")
        except Exception as e:
            print(f">>> ⚠️ [ML] 모델 저장 실패: {e}")

    def _append_history(self, filename, train_date, score, n_features):
        hist = []
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, encoding='utf-8') as f:
                    hist = json.load(f)
            except Exception:
                hist = []
        hist.append({
            "file": filename,
            "train_date": train_date,
            "score": round(float(score), 2),
            "features": n_features,
            "ts": time.time(),
        })
        with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(hist[-50:], f, ensure_ascii=False, indent=2)

    def _prune_versions(self):
        """오래된 버전 정리 (최근 MAX_VERSIONS개만 유지)"""
        try:
            files = [os.path.join(VERSIONS_DIR, f) for f in os.listdir(VERSIONS_DIR) if f.endswith('.pkl')]
            files.sort(key=os.path.getmtime, reverse=True)
            for old in files[MAX_VERSIONS:]:
                os.remove(old)
        except Exception as e:
            print(f">>> ⚠️ [ML] 버전 정리 실패: {e}")

    def list_versions(self):
        """보관된 모델 버전 메타데이터 목록 (최신순)"""
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, encoding='utf-8') as f:
                    return list(reversed(json.load(f)))
            except Exception:
                return []
        return []

    def rollback(self, filename=None):
        """이전 모델 버전으로 롤백. filename 미지정 시 현재 직전 버전 사용."""
        try:
            files = sorted([f for f in os.listdir(VERSIONS_DIR) if f.endswith('.pkl')], reverse=True)
            if not files:
                return {"status": "error", "message": "보관된 버전이 없습니다"}
            if filename is None:
                # files[0]=현재(가장 최신 저장본), files[1]=직전
                target = files[1] if len(files) >= 2 else files[0]
            else:
                if filename not in files:
                    return {"status": "error", "message": f"버전을 찾을 수 없음: {filename}"}
                target = filename
            shutil.copy2(os.path.join(VERSIONS_DIR, target), MODEL_PATH)
            self._load_model()
            print(f">>> ⏪ [ML] 롤백 완료: {target} (정확도 {self.train_score:.1f}%)")
            return {"status": "success", "message": f"롤백 완료: {target}",
                    "score": self.train_score, "train_date": self.train_date}
        except Exception as e:
            return {"status": "error", "message": str(e)}

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

        # --- [v2] 거래대금 (volume * close) ---
        trade_value = volume * close
        tv_ma20 = trade_value.rolling(20).mean().replace(0, 1)
        feat['trade_value_ratio'] = trade_value / tv_ma20

        # --- [v2] OBV (On-Balance Volume) 변화율 ---
        obv = (np.sign(close.diff()) * volume).cumsum()
        obv_ma10 = obv.rolling(10).mean().replace(0, 0.0001)
        feat['obv_ratio'] = (obv / obv_ma10 - 1) * 100

        # --- [v2] 지지/저항 근접도 ---
        high_20 = high.rolling(20).max()
        low_20 = low.rolling(20).min()
        price_range = (high_20 - low_20).replace(0, 0.0001)
        feat['support_resistance_pos'] = (close - low_20) / price_range

        # --- [v2] 변동성 수축/확장 (BB Width) ---
        bb_width = (4 * bb_std) / bb_ma.replace(0, 0.0001) * 100
        bb_width_ma = bb_width.rolling(10).mean().replace(0, 0.0001)
        feat['bb_width_ratio'] = bb_width / bb_width_ma

        # --- [v2] 거래량 트렌드 (5일 vs 20일) ---
        vol_ma5 = volume.rolling(5).mean().replace(0, 1)
        feat['vol_trend'] = vol_ma5 / vol_ma20

        # --- [v2] 가격-거래량 다이버전스 ---
        price_up = close.diff() > 0
        vol_down = volume.diff() < 0
        feat['price_vol_divergence'] = (price_up & vol_down).astype(int)

        # --- [v2] 이동평균 정배열/역배열 점수 ---
        ma_align = (
            (ma5 > ma10).astype(int) +
            (ma10 > ma20).astype(int) +
            (close > ma5).astype(int)
        )
        feat['ma_alignment'] = ma_align

        # --- [v2] 하락 후 반등 패턴 ---
        feat['drawdown_5d'] = (close / high.rolling(5).max() - 1) * 100
        feat['bounce_from_low'] = (close / low.rolling(5).min() - 1) * 100

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

                # 정답: 다음날 고가가 오늘 종가 대비 LABEL_THRESHOLD_PCT% 이상 상승하면 1
                next_high = df['high'].shift(-1)
                gain_pct = ((next_high - df['close']) / df['close']) * 100
                y = (gain_pct >= LABEL_THRESHOLD_PCT).astype(int)

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

            # 🔥 [개선] 3-way 시계열 분할: 70% 학습 / 15% 보정 / 15% 테스트
            # - 학습: XGBoost 적합
            # - 보정: Isotonic regression으로 확률 calibration
            # - 테스트: 보정 vs 미보정 비교 평가
            total = len(X)
            train_end = int(total * 0.70)
            cal_end = int(total * 0.85)
            X_train, X_cal, X_test = X.iloc[:train_end], X.iloc[train_end:cal_end], X.iloc[cal_end:]
            y_train, y_cal, y_test = y.iloc[:train_end], y.iloc[train_end:cal_end], y.iloc[cal_end:]
            print(f"    분할: 학습={len(X_train)} / 보정={len(X_cal)} / 테스트={len(X_test)}")

            # 클래스 불균형 처리
            neg_count = (y_train == 0).sum()
            pos_count = (y_train == 1).sum()
            spw = neg_count / max(pos_count, 1)
            print(f"    클래스 비율: 상승={pos_count} / 하락={neg_count} (scale_pos_weight={spw:.2f})")

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
                scale_pos_weight=spw,
                random_state=42,
                eval_metric='logloss',
                verbosity=0,
            )

            # Early stopping용 eval은 calibration set 사용 (test는 최종 평가용)
            model.fit(
                X_train, y_train,
                eval_set=[(X_cal, y_cal)],
                verbose=False,
            )

            # 🔥 [개선] Isotonic Regression Calibrator
            # XGBoost raw output → 실제 확률에 맞춰 보정 (모델 평균 73% → 실제 33% 같은 어긋남 해결)
            from sklearn.isotonic import IsotonicRegression
            raw_cal_probs = model.predict_proba(X_cal)[:, 1]
            calibrator = IsotonicRegression(out_of_bounds='clip')
            calibrator.fit(raw_cal_probs, y_cal)

            # 평가: 보정 전후 비교
            train_acc = model.score(X_train, y_train) * 100
            raw_test_probs = model.predict_proba(X_test)[:, 1]
            cal_test_probs = calibrator.transform(raw_test_probs)
            uncal_acc = ((raw_test_probs >= 0.5) == y_test.values).mean() * 100
            cal_acc = ((cal_test_probs >= 0.5) == y_test.values).mean() * 100

            actual_pos_rate = y_test.mean() * 100
            print(f"    테스트 평균확률 — 미보정: {raw_test_probs.mean()*100:.1f}% / 보정: {cal_test_probs.mean()*100:.1f}% (실제 양성률 {actual_pos_rate:.1f}%)")
            print(f"    테스트 정확도   — 미보정: {uncal_acc:.1f}% / 보정: {cal_acc:.1f}%")

            test_acc = cal_acc  # 보정된 정확도를 공식 점수로

            # 🔥 [P1] 정확도 급락 감지 (이전 대비 10%p↓ 또는 50% 미만이면 경고)
            prev_score = self.train_score
            if prev_score and (test_acc < prev_score - 10 or test_acc < 50):
                print(f">>> ⚠️ [ML] 정확도 급락 경고! 이전 {prev_score:.1f}% → 신규 {test_acc:.1f}% "
                      f"(이상 시 rollback API로 직전 버전 복구 가능)")

            self.model = model
            self.calibrator = calibrator
            self.is_trained = True
            self.train_score = test_acc
            self.train_date = datetime.now(KST).strftime('%Y-%m-%d %H:%M')
            self._save_model()

            # 피처 중요도 상위 10개 출력
            importances = model.feature_importances_
            top_idx = np.argsort(importances)[-10:][::-1]
            top_features = [(self.feature_names[i], importances[i]) for i in top_idx]

            # 정밀도/재현율 계산
            from sklearn.metrics import precision_score, recall_score, f1_score
            y_pred_test = model.predict(X_test)
            precision = precision_score(y_test, y_pred_test, zero_division=0) * 100
            recall = recall_score(y_test, y_pred_test, zero_division=0) * 100
            f1 = f1_score(y_test, y_pred_test, zero_division=0) * 100

            print(f">>> 🤖 [ML] 학습 완료! (v2: {LABEL_THRESHOLD_PCT}%+ 상승 예측)")
            print(f"    학습 데이터: {len(X_train)}행 / 테스트: {len(X_test)}행")
            print(f"    학습 정확도: {train_acc:.1f}% / 테스트 정확도: {test_acc:.1f}%")
            print(f"    정밀도: {precision:.1f}% / 재현율: {recall:.1f}% / F1: {f1:.1f}%")
            print(f"    상위 피처: {', '.join(f'{n}({v:.3f})' for n, v in top_features[:5])}")

        except Exception as e:
            print(f">>> ❌ [ML] 학습 실패: {e}")
            import traceback
            traceback.print_exc()

    # 피처 이름 → 한글 설명 매핑
    FEATURE_LABELS = {
        'return_1d': '1일 수익률', 'return_3d': '3일 수익률', 'return_5d': '5일 수익률', 'return_10d': '10일 수익률',
        'ma5_ratio': 'MA5 이격도', 'ma10_ratio': 'MA10 이격도', 'ma20_ratio': 'MA20 이격도', 'ma5_ma20_cross': 'MA 골든크로스',
        'rsi': 'RSI', 'mfi': 'MFI 자금흐름', 'macd_diff': 'MACD 차이', 'macd_ratio': 'MACD 비율',
        'adx': 'ADX 추세강도', 'di_diff': 'DI 방향', 'bb_position': '볼린저 위치',
        'vol_ratio': '거래량 폭발', 'vol_change': '거래량 변화', 'atr_ratio': 'ATR 변동성',
        'body_ratio': '캔들 몸통', 'upper_wick': '윗꼬리', 'is_bullish': '양봉 여부',
        'volatility_5d': '5일 변동성', 'volatility_10d': '10일 변동성',
        'momentum_accel': '모멘텀 가속', 'vol_price_corr': '거래량-가격 상관',
        'hl_range_pct': '고저 범위', 'rsi_change': 'RSI 변화',
        'consecutive_candles': '연속 양봉/음봉', 'trade_value_ratio': '거래대금 비율',
        'obv_ratio': 'OBV 비율', 'support_resistance_pos': '지지/저항 위치',
        'bb_width_ratio': 'BB 수축/확장', 'vol_trend': '거래량 추세',
        'price_vol_divergence': '가격-거래량 괴리', 'ma_alignment': '이평선 정배열',
        'drawdown_5d': '5일 낙폭', 'bounce_from_low': '저점 반등', 'hour': '시간대',
    }

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

            raw_prob = self.model.predict_proba(last_row)[0][1]  # 상승 확률 (raw)
            # [개선] calibrator 있으면 보정된 확률 반환
            if self.calibrator is not None:
                return float(self.calibrator.transform([raw_prob])[0])
            return float(raw_prob)

        except Exception as e:
            print(f"⚠️ [ML Predict Error] {e}")
            return 0.5

    def predict_with_reasons(self, df: pd.DataFrame) -> dict:
        """
        상승 확률 + 코인별 SHAP 기여도 기반 주요 근거 반환
        XGBoost pred_contribs를 사용하여 개별 예측에 대한 피처 기여도를 계산
        Returns: { prob: float, reasons: [{ name, label, value, shap, direction }] }
        """
        if not self.is_trained or self.model is None:
            return {"prob": 0.5, "reasons": []}

        try:
            import xgboost as xgb

            features = self.build_features(df)
            if features.empty:
                return {"prob": 0.5, "reasons": []}

            last_row = features.iloc[[-1]]
            if last_row.isna().any(axis=1).iloc[0]:
                last_row = last_row.fillna(0)
            last_row = last_row.reindex(columns=self.feature_names, fill_value=0)

            raw_prob = float(self.model.predict_proba(last_row)[0][1])
            # [개선] calibrator 있으면 보정된 확률, SHAP은 raw 모델 기반 그대로
            if self.calibrator is not None:
                prob = float(self.calibrator.transform([raw_prob])[0])
            else:
                prob = raw_prob

            # --- 개별 예측 SHAP 기여도 (pred_contribs) ---
            dmatrix = xgb.DMatrix(last_row, feature_names=self.feature_names)
            # contribs shape: (1, n_features + 1) — 마지막 값은 bias
            contribs = self.model.get_booster().predict(dmatrix, pred_contribs=True)
            shap_values = contribs[0][:-1]  # bias 제외
            values = last_row.values[0]

            reasons = []
            for i, fname in enumerate(self.feature_names):
                shap_val = float(shap_values[i])
                val = float(values[i])
                label = self.FEATURE_LABELS.get(fname, fname)
                # SHAP 양수 = 상승 확률에 기여, 음수 = 하락 방향 기여
                direction = "up" if shap_val > 0.01 else "down" if shap_val < -0.01 else "neutral"
                reasons.append({
                    "name": fname,
                    "label": label,
                    "value": round(val, 4),
                    "shap": round(shap_val, 4),
                    "direction": direction,
                })

            # |SHAP| 절대값 순 정렬 → 이 코인에 가장 영향을 준 피처 상위 8개
            reasons.sort(key=lambda x: abs(x["shap"]), reverse=True)
            top_reasons = reasons[:8]

            return {"prob": prob, "reasons": top_reasons}

        except Exception as e:
            print(f"⚠️ [ML Predict Reasons Error] {e}")
            return {"prob": 0.5, "reasons": []}

    def get_status(self) -> dict:
        """모델 상태 반환 (API/프론트 표시용)"""
        return {
            "is_trained": self.is_trained,
            "accuracy": self.train_score,
            "train_date": self.train_date or "-",
            "features": len(self.feature_names),
            "version": "v2",
            "label_threshold": LABEL_THRESHOLD_PCT,
        }
