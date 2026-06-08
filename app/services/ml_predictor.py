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
        """[분봉 모델 v3] Colab 학습 노트북의 build_features와 1:1 동일 (11개 피처).

        ⚠️ 절대 임의 수정 금지 — Colab 학습 코드와 정확히 일치해야 모델이 정상 작동.
        ⚠️ minute5 데이터로 학습됨 → 추론도 반드시 minute5 OHLCV df를 입력해야 함.

        피처: volatility, ma_5, ma_20, ma_60, rsi, macd, macd_signal, macd_hist,
              volume_ma_5, volume_ma_20, daily_range_pct
        """
        # ma_60 + 워밍업 위해 최소 60봉 필요
        if df is None or len(df) < 60:
            return pd.DataFrame()

        features = pd.DataFrame(index=df.index)

        # 가격 변동성 (종가 20봉 표준편차)
        features['volatility'] = df['close'].rolling(window=20).std()

        # 이동 평균선 (원시 가격값)
        features['ma_5'] = df['close'].rolling(window=5).mean()
        features['ma_20'] = df['close'].rolling(window=20).mean()
        features['ma_60'] = df['close'].rolling(window=60).mean()

        # RSI (rolling mean 방식 14)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 1e-9)
        features['rsi'] = 100 - (100 / (1 + rs))

        # MACD (12/26/9, 원시 가격 스케일)
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        features['macd'] = exp1 - exp2
        features['macd_signal'] = features['macd'].ewm(span=9, adjust=False).mean()
        features['macd_hist'] = features['macd'] - features['macd_signal']

        # 거래량 이동평균 (원시값)
        features['volume_ma_5'] = df['volume'].rolling(window=5).mean()
        features['volume_ma_20'] = df['volume'].rolling(window=20).mean()

        # (고가 - 저가) / 종가 * 100
        features['daily_range_pct'] = (df['high'] - df['low']) / df['close'] * 100

        features = features.replace([np.inf, -np.inf], np.nan)
        return features

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
    # [분봉 모델 v3] 11개 피처 한글 라벨 (SHAP 근거 표시용)
    FEATURE_LABELS = {
        'volatility': '변동성(20봉)',
        'ma_5': 'MA5', 'ma_20': 'MA20', 'ma_60': 'MA60',
        'rsi': 'RSI',
        'macd': 'MACD', 'macd_signal': 'MACD 시그널', 'macd_hist': 'MACD 히스토그램',
        'volume_ma_5': '거래량 MA5', 'volume_ma_20': '거래량 MA20',
        'daily_range_pct': '고저 범위(%)',
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
            "version": "v3-minute5",
            "label_threshold": LABEL_THRESHOLD_PCT,
        }
