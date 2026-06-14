"""
v4 모델 배포 전 검증 게이트 (세션27)
=====================================
v3 붕괴(예측 std 0.05 / 고유값 27 / max 0.41 → 변별력 0) 재발 방지.
새 .pkl을 서버에 올리기 *전에* 이 스크립트로 통과 여부를 확인한다.

사용법 (EC2 또는 pyupbit 가능한 환경):
    python3.11 validate_model_v4.py /경로/새모델.pkl

검사 항목 (분포 기반 — 라이브 minute5 표본으로):
  G1 feature_names == FEATURE_COLUMNS_V4 (순서까지)
  G2 예측 std ≥ 0.12        (v3=0.05, 변별력)
  G3 고유값 ≥ 표본의 40%    (v3=27/249≈11%, 과도 양자화 금지)
  G4 max 예측 ≥ 0.45        (게이트 0.42가 실제로 열려야 함)
  G5 ≥0.42 통과 코인 ≥ 1개  (매수 후보가 생겨야 함)

⚠️ 리프트(상위10% ≥2배)·레짐층화 calibration은 학습 데이터가 필요 →
   Colab 학습 셀에서 확인(가이드 V4_RETRAIN_GUIDE.md 참고). 여기선 분포만.
"""
import sys
import time
import pickle
import numpy as np
import pandas as pd
import pyupbit

from feature_engineering_v4 import build_features_v4, FEATURE_COLUMNS_V4

SAMPLE_COINS = 80     # 표본 코인 수
FETCH_COUNT = 360     # 24h 롤링 워밍업용


def load_model(path):
    with open(path, "rb") as fp:
        obj = pickle.load(fp)
    if isinstance(obj, dict):
        return obj.get("model"), obj.get("calibrator"), obj.get("feature_names")
    return obj, None, None


def predict(model, calibrator, feat_row):
    raw = model.predict_proba(feat_row[FEATURE_COLUMNS_V4])[:, 1]
    if calibrator is not None:
        raw = calibrator.predict(raw)
    return raw


def main():
    if len(sys.argv) < 2:
        print("사용법: python3.11 validate_model_v4.py <model.pkl>")
        sys.exit(1)
    model, calibrator, fnames = load_model(sys.argv[1])

    print("=" * 60)
    # G1 feature_names
    g1 = (fnames is not None and list(fnames) == FEATURE_COLUMNS_V4)
    print(f"G1 feature_names 일치: {'PASS' if g1 else 'FAIL'}")
    if not g1:
        print(f"   모델: {list(fnames) if fnames else None}")
        print(f"   기대: {FEATURE_COLUMNS_V4}")

    # BTC + 표본 코인 minute5
    print(f">>> BTC + {SAMPLE_COINS}개 코인 minute5({FETCH_COUNT}봉) 수집...")
    btc = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=FETCH_COUNT)
    time.sleep(0.1)
    tickers = (pyupbit.get_tickers(fiat="KRW") or [])[:SAMPLE_COINS]

    preds = []
    for t in tickers:
        try:
            df = pyupbit.get_ohlcv(t, interval="minute5", count=FETCH_COUNT)
            time.sleep(0.08)
            feats = build_features_v4(df, btc)
            if feats.empty:
                continue
            last = feats.dropna().iloc[[-1]]
            if last.empty:
                continue
            p = float(predict(model, calibrator, last)[0])
            preds.append(p)
        except Exception:
            pass

    p = np.array(preds)
    n = len(p)
    print(f">>> 예측 표본: {n}개")
    if n < 20:
        print("표본 부족 — 재시도 필요")
        sys.exit(1)

    std = p.std()
    uniq = len(set(round(x, 3) for x in p))
    mx = p.max()
    above42 = int((p >= 0.42).sum())

    g2 = std >= 0.12
    g3 = uniq >= n * 0.40
    g4 = mx >= 0.45
    g5 = above42 >= 1

    print("=" * 60)
    print(f"평균 {p.mean():.3f} | std {std:.3f} | 고유값 {uniq}/{n} | max {mx:.3f} | ≥0.42 {above42}개")
    print("-" * 60)
    print(f"G2 std≥0.12          : {'PASS' if g2 else 'FAIL'} ({std:.3f})")
    print(f"G3 고유값≥40%        : {'PASS' if g3 else 'FAIL'} ({uniq}/{n})")
    print(f"G4 max≥0.45          : {'PASS' if g4 else 'FAIL'} ({mx:.3f})")
    print(f"G5 ≥0.42 통과 ≥1개   : {'PASS' if g5 else 'FAIL'} ({above42})")
    print("=" * 60)
    allp = all([g1, g2, g3, g4, g5])
    print(f"종합: {'✅ 배포 가능' if allp else '❌ 배포 보류 — 재학습/피처 보강 필요'}")
    sys.exit(0 if allp else 2)


if __name__ == "__main__":
    main()
