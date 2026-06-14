# 분봉 모델 v4 재학습 가이드 (세션27)

## 왜 v4인가 (정밀분석 요약)
v3(현행 11피처)는 **원시 절대값 피처**라 코인 간 전이가 안 됨 → calibrated 확률이
기저율(~0.28)로 붕괴(전 종목 std 0.05 / 고유값 27 / max 0.41, 가상거래 6건 전부 ml_prob=0.369).
즉 **ML이 진입 선별에 기여 0**. 일일평가도 "예측 일정(29.8%) / 실제 출렁(4.5~52.9%)" =
모델이 시장맥락을 못 봄.

**v4 3대 개선**: ① 전 피처 상대값 정규화(코인 전이) ② BTC 시장맥락 피처(레짐) ③ 진입타이밍·모멘텀.
피처 11 → **27개**. 라벨/TP-SL/forward는 v3 그대로 유지(FORWARD_BARS=288, TP3.5/SL-2.0).

---

## A. Colab 노트북 수정 (`CoinMate_ML_Minute_Training.ipynb`)

### 1) 피처 함수 교체
`build_features(df)` 정의 셀을 **삭제**하고, `feature_engineering_v4.py`를 업로드 후 import
(또는 셀에 함수 본문 붙여넣기). **단일 진실 공급원이므로 서버와 절대 분기시키지 말 것.**

### 2) BTC 데이터 로드 (시장맥락 피처용)
데이터셋 빌드 루프 **전에** BTC minute5를 한 번 로드:
```python
btc_df = pd.read_parquet(f'{CACHE_DIR}/{TIMEFRAME}_KRW-BTC.parquet')  # 없으면 download_one('KRW-BTC')
```

### 3) 데이터셋 빌드 루프에서 build_features 호출 변경
```python
feats = build_features_v4(df, btc_df)          # ← (df) 에서 (df, btc_df) 로
labels = make_labels(df, FORWARD_BARS, TAKE_PROFIT_PCT, STOP_LOSS_PCT)
valid_mask = feats.notna().all(axis=1).values & (labels >= 0)
feats_v = feats[valid_mask].iloc[::SAMPLE_STEP]
labels_v = labels[valid_mask][::SAMPLE_STEP]
```
※ BTC 자기 자신은 학습셋에서 제외 권장(coin_btc_corr=1 자명).

### 4) 불균형 보정 (양성 기저율 낮음 대응)
XGBoost 파라미터에 추가:
```python
pos = (y_train == 1).sum(); neg = (y_train == 0).sum()
scale_pos_weight = neg / max(pos, 1)
# XGBClassifier(..., scale_pos_weight=scale_pos_weight)
```

### 5) 모델 저장 포맷 (서버 호환 — 반드시 유지)
```python
import pickle
pickle.dump({
    'model': model,
    'calibrator': calibrator,          # IsotonicRegression (또는 Platt). 신호 보강 후에도 계단지면 Platt 검토
    'feature_names': FEATURE_COLUMNS_V4,  # ← v4 순서 (서버 검증 G1)
    'score': test_acc,
    'train_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
}, open('xgb_model.pkl','wb'))
```

### 6) Colab측 검증 셀 (분포 게이트는 서버 validate_model_v4.py가 담당, 여기선 학습데이터 필요한 것)
```python
# 리프트: 상위10% 예측의 실제 양성률 / 전체 양성률 ≥ 2.0 목표
proba_test = calibrator.predict(model.predict_proba(X_test)[:,1])
import numpy as np
thr = np.quantile(proba_test, 0.9)
lift = y_test[proba_test>=thr].mean() / max(y_test.mean(), 1e-9)
print(f'상위10% 리프트: {lift:.2f}x (목표 ≥2.0)')

# 레짐 층화 calibration: btc_ret_24h>0(상승) vs <0(하락) 각각 |예측평균-실제| ≤ 0.10
import pandas as pd
reg = X_test['btc_ret_24h'] > 0
for name, mask in [('상승일', reg), ('하락일', ~reg)]:
    if mask.sum()>50:
        err = abs(proba_test[mask].mean() - y_test[mask].mean())
        print(f'{name}: 예측 {proba_test[mask].mean():.3f} vs 실제 {y_test[mask].mean():.3f} (오차 {err:.3f})')
print(f'예측 std {proba_test.std():.3f} (목표 ≥0.12) / 고유값 {len(set(np.round(proba_test,3)))}')
```

---

## B. 서버 적용 (⚠️ 새 모델 업로드 *후에만*)

> 새 `xgb_model.pkl`을 올리기 전에 서버 build_features를 바꾸면 **현행 11피처 모델과 불일치로
> 추론이 깨짐.** 반드시 모델 업로드 → 동시에 아래 코드 교체 → 재시작 순서.

### 1) `ml_predictor.build_features` 교체
`feature_engineering_v4.py`를 `app/services/`에 두고:
```python
from app.services.feature_engineering_v4 import build_features_v4, FEATURE_COLUMNS_V4
# build_features(self, df, btc_df) 시그니처로 변경 → return build_features_v4(df, btc_df)
# FEATURE_LABELS / get_status version "v4-minute5"로 갱신
```

### 2) BTC minute5 공급 (`trade_manager`)
- `get_smart_candles`: minute5 `count=200` → **`count=360`** (24h 롤링 워밍업)
- BTC minute5를 30분 캐시로 보관(`self._btc_5m_cache`), 추론 호출부에 전달:
  - `process_buying`, `refresh_target_scores`, `refresh_ml_top_probs`, `backtester._analyze_one`,
    `market_api analysis` 의 `ml.predict_with_reasons(df_5m)` → `(df_5m, btc_5m)` 로

### 3) 배포 후 검증
```bash
scp .../xgb_model.pkl ec2:.../models/xgb_model.pkl
scp .../feature_engineering_v4.py ec2:.../app/services/
python3.11 notebooks/validate_model_v4.py models/xgb_model.pkl   # G1~G5 PASS 확인
sudo systemctl restart coinmate
```

---

## C. 배포 전 통과 기준 (요약)
| 게이트 | 기준 | v3(현행) |
|---|---|---|
| 예측 std | ≥ 0.12 | 0.05 ❌ |
| 고유값 비율 | ≥ 40% | 11% ❌ |
| max 예측 | ≥ 0.45 | 0.41 ❌ |
| 상위10% 리프트 | ≥ 2.0x | ~1.3x ❌ |
| 레짐층화 calib 오차 | ≤ 0.10 (상승/하락 각각) | 평균만 맞음 ❌ |

하나라도 FAIL이면 배포 보류 — 피처 추가/라벨 조정 후 재학습.
