# CoinMate Backend

> 업비트(Upbit) 기반 암호화폐 자동매매 봇 — FastAPI + XGBoost ML

AI 앙상블 전략과 머신러닝 익절 예측을 결합해 매수/매도를 자동 판단하는 트레이딩 봇의 백엔드입니다. 다층 안전장치(시장 레짐 필터, Kelly 자금관리, 연속손절 쿨오프)와 뉴스 센티멘트 분석, Discord 실시간 알림을 갖추고 있습니다.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white)
![XGBoost](https://img.shields.io/badge/XGBoost-v4--minute5-EB5E28)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)

**🖥 웹 대시보드: [CoinMate-frontend](https://github.com/DONRAJI/CoinMate-frontend)** — 실시간 시세, ML 근거(SHAP), 거래 성과, 봇 제어

---

## 한눈에

| | |
|---|---|
| **역할** | 1인 개발 — 전략 설계 · ML 파이프라인 · 백엔드 · 프론트엔드 · 인프라 |
| **기간** | 2026.01 ~ 2026.06 (약 5개월 · 작업 세션 35회) |
| **규모** | 백엔드 Python 약 6,800줄 · 대시보드 TypeScript 약 2,200줄 · 커밋 121 + 52 |
| **인프라** | EC2 + Caddy(자동 HTTPS) + systemd · SQLite(WAL) |
| **현재 상태** | **운영 중단**(서버 운영비 부담). 중단 직전 국면에서는 라이브 매매를 멈추고 **섀도우(가상) 거래**로 전략을 검증하던 단계였다 |

> ⚠️ **성과 지표를 수익률로 제시하지 않습니다.** 이 문서에 나오는 숫자는 예측 분포·게이트 통과 여부·손익분기 대비 익절률 같은 **엔지니어링 지표**입니다. 투자 성과는 시장 상황에 좌우되고 재현 검증이 불가능하기 때문입니다.

---

## 기술적으로 어려웠던 것 3가지

### 1. 모델이 "예측하는 척"만 하고 있었다

**문제** — 분봉 모델 v3의 정확도 지표에는 문제가 없었다. 그런데 실제 예측값 분포를 뜯어보니 전 종목이 사실상 **같은 값을 뱉고 있었다.**

| 지표 | v3 실측 | 의미 |
|---|---|---|
| 예측 std | **0.05** | 코인이 달라도 확률이 안 움직인다 |
| 고유값 개수 | 249개 표본 중 **27개** | 과도한 양자화 |
| 최댓값 | **0.41** | 매수 게이트(0.42)가 **한 번도 안 열린다** |
| 가상거래 6건 | 전부 `ml_prob=0.369` | 기저율(~0.28) 근처로 붕괴 |

즉 **ML이 진입 선별에 기여한 정도가 0**이었다. 게이트를 통과한 종목이 있었다면 그건 모델이 골라준 게 아니라 앙상블 점수만으로 뽑힌 것이었다.

**원인** — 피처가 **원시 절대값**이었다. 이동평균·MACD·거래량을 그대로 넣었는데, BTC의 `ma`는 9,300만 단위이고 잡코인은 2 단위다. 스케일이 1,000만 배 차이나니 코인 간 전이가 불가능했고, 모델은 결국 어떤 입력에도 비슷한 값을 내는 쪽으로 수렴했다. 일일 자가평가 리포트에도 증상이 남아 있었다 — "예측은 일정(29.8%) / 실제는 출렁(4.5~52.9%)", 모델이 시장 맥락을 못 보고 있다는 뜻이었다.

**판단** — 모델을 바꾸는 대신 **피처를 전부 상대값으로 재설계**했다. 11개 → 27개.

- **상대값 정규화** — 수익률·이동평균 이격도·기울기·ATR%·거래량 서지 비율 등, 전부 비율/퍼센트로. 코인 간 동일 스케일
- **BTC 시장맥락 피처** — `btc_ret_1h/4h/24h`, `btc_ma_dist`, `coin_btc_corr`. "예측은 일정한데 실제는 시장 따라 출렁"을 해소
- **진입 타이밍·모멘텀** — 24h 고저 대비 위치, 거래량 서지, 기울기. 진입 직후 손절 패턴 대응
- **불균형 보정** — 양성 기저율이 낮아 `scale_pos_weight = neg/pos`

라벨과 TP/SL은 v3 그대로 뒀다(`FORWARD_BARS=288`, 익절 +3.5% / 손절 −2.0%). **한 번에 여러 변수를 바꾸면 무엇이 효과를 냈는지 알 수 없기 때문이다.**

**결과 — 그리고 재발 방지** — 고친 것보다 중요한 건 **같은 붕괴가 다시 배포되지 않게 만든 것**이다. 새 모델을 서버에 올리기 **전에** 통과해야 하는 게이트를 스크립트로 만들었다(`notebooks/validate_model_v4.py`). 라이브 5분봉 표본 80개 코인으로 검사한다.

| 게이트 | 기준 | v3였다면 |
|---|---|---|
| G1 | `feature_names`가 `FEATURE_COLUMNS_V4`와 **순서까지** 일치 | — |
| G2 | 예측 std ≥ 0.12 | ❌ 0.05 |
| G3 | 고유값 ≥ 표본의 40% | ❌ 약 11% |
| G4 | max 예측 ≥ 0.45 | ❌ 0.41 |
| G5 | 매수 게이트(0.42) 통과 코인 ≥ 1개 | ❌ 0개 |

학습 쪽에서는 **리프트**(상위 10% 예측의 실제 양성률 ÷ 전체 양성률) ≥ 2.0을 목표로 잡았다.

여기서 하나 더 막았다. Colab 학습과 서버 추론이 **각자 `build_features`를 갖고 있으면 조용히 어긋난다** — 코드가 아니라 결과만 틀리므로 발견이 늦다(training-serving skew). `feature_engineering_v4.py`를 **단일 진실 공급원**으로 두고 양쪽이 같은 함수를 import하게 해서, 불일치가 원리적으로 불가능하게 만들었다.

> `app/services/feature_engineering_v4.py` · `notebooks/validate_model_v4.py` · `notebooks/V4_RETRAIN_GUIDE.md`

### 2. 모델을 바꿨더니 매수가 한 건도 나가지 않았다

**문제** — 일봉 모델에서 분봉 모델로 교체한 뒤, 봇이 **아무것도 사지 않았다.** 에러도 없고 로그도 정상이었다.

**원인** — 임계값 `ML_MIN_PROB = 0.55`를 그대로 뒀기 때문이다. 두 모델은 똑같이 0~1 사이 값을 뱉지만 **예측하는 대상이 달랐다.**

| | 옛 일봉 모델 | 새 분봉 모델 |
|---|---|---|
| 라벨 | "다음날 1% 이상 오를 확률" | "익절 +3.5%가 손절 −2%보다 **먼저** 닿을 확률" |
| 분포 | 상대적으로 높게 나옴 | 구조적으로 낮게 나옴 |

같은 척도처럼 보였을 뿐, 0.55는 새 모델에서 사실상 도달 불가능한 값이었다.

**판단** — 임계값을 감으로 다시 잡는 대신 **손익분기에서 역산**했다. 익절 +3.5% / 손절 −2.0% 구조에서 수수료를 빼고 기댓값이 0이 되는 승률은

```
p = 손절폭 / (익절폭 + 손절폭) = 2.0 / (3.5 + 2.0) = 0.364
```

즉 **36.4%가 수수료 전 손익분기**다. 여기에 왕복 수수료(0.1%)와 안전 마진을 얹어 **0.42**로 정했다. 이 숫자는 "적당히 높게"가 아니라 **TP/SL을 바꾸면 같이 바뀌어야 하는 종속 변수**다.

나중에 **프론트엔드가 36%를, 백엔드가 42%를 기준선으로 그리고 있는 불일치**도 발견해 통일했다. 36%는 "이론상 본전"이고 42%는 "실제로 매수하는 선"이라, 대시보드에서 36%만 보면 사지도 않을 종목이 초록색으로 보였다.

**결과** — 매수가 정상 재개됐고, 이후 임계값 조정은 전부 손익분기 대비 마진으로 논의하게 됐다. 일일 자가평가도 같은 기준으로 처방을 낸다 — 실제 익절률이 손익분기 아래로 내려가면 `🔴 매수기준↑ 실제익절률 X% < 손익분기 36% → 임계값 상향(0.42→0.47) 또는 피처 보강 필요` 같은 **조치 문구를 만들어 Discord로 보낸다.**

> `app/services/trade_manager.py`(`ML_MIN_PROB`) · `app/services/backtester.py` · `app/services/notifier.py`

### 3. 실계좌로는 가설을 검증할 수 없었다

**문제** — 전략을 바꿀 때마다(모멘텀 게이트 추가, 추세역행 필터 완화, 하락장 매수 허용 등) "이게 나은가"를 확인할 방법이 실제로 사보는 것뿐이었다. **검증 비용이 곧 실손실**이고, 표본이 쌓일 때까지 기다리면 그동안 계속 잃는다.

**판단** — **섀도우(가상) 거래**를 라이브와 나란히 돌리는 구조를 만들었다. 핵심은 "가상"이 아니라 **동일 조건**이다.

- 라이브와 **같은 신호·같은 게이트·같은 손절 규칙**을 쓴다. 다르면 비교가 무의미하다
- **슬리피지를 가정에 넣는다** — 실거래는 시장가라 신호가와 체결가가 다르다. 편도 0.05%(왕복 0.1% 추가 비용)를 반영했다. 거래대금 10억 이상 종목만 매수하므로 5bp로 잡았다. 이걸 빼면 가상 성과가 낙관적으로 부풀어 **검증 도구 자체가 거짓말을 한다**
- 손절 기준을 **모델 라벨(−2%)에 고정**했다. 모델이 학습한 조건과 검증 조건이 달라지면 안 된다
- 가상 잔고(100만원)를 파일로 영속화해 재시작해도 이어진다. `SHADOW_MODE` 토글로 대시보드에서 전환한다

**결과** — 전략 변경이 "일단 켜보고 손실 나면 되돌리기"에서 **가설 → 섀도우 검증 → 데이터 축적 → 반영**으로 바뀌었다. 실제 진행이 이 순서였다.

```
세션30  모멘텀 확실성 게이트 추가 (섀도우 전용)
세션31  섀도우에서 하락장 매수 허용 — 게이트 검증용 데이터 수집
세션32  추세역행 필터(급등/고점/과열) 섀도우에서 완화
세션33  섀도우 손절을 모델 라벨(−2%)에 고정 — 조건 정합
세션34  교차검증 결과 반영 → 모멘텀 게이트를 mom6h 단일로 축소
```

라이브에 영향 0인 상태로 4세션 동안 데이터를 모은 뒤, 그 결과로 게이트를 **줄이는** 결정을 내렸다. 검증 장치가 없었으면 기능을 추가만 했을 것이다.

> `app/core/paper_repository.py` · `GET /trade/shadow/stats` · `POST /trade/shadow/toggle`

---

## ✨ 주요 기능

### 매매 엔진
- **앙상블 전략 점수** — 7개 기술적 지표(추세/MACD/RSI·MFI/ADX/거래량/볼린저/VWAP)를 가중 합산한 12.75점 만점 스코어링 (매수 임계 5.5점)
- **ATR 기반 동적 손절/익절** — 변동성에 비례한 손절·익절선 자동 조정 + 트레일링 스탑
- **시장 레짐 필터** — BTC 추세(상승/중립/하락) 판정으로 하락장 신규 매수 차단
- **Kelly Criterion 자금관리** — ML 승률 기반 동적 포지션 사이징(1/4 Kelly)
- **다층 안전장치** — 심야(22~07시) 매수 차단, 연속손절 쿨오프(3연패 + 6시간 윈도우, 점진 강화), 코인별 블랙리스트

### 머신러닝 (XGBoost)
- **분봉 모델 v4** — 5분봉 + **27개 상대값·시장맥락 피처**, forward-simulation 라벨("익절 +3.5%가 손절 −2%보다 먼저 닿을 확률", 24시간 288봉 시뮬레이션)
- **단일 진실 공급원** — `feature_engineering_v4.py`를 Colab 학습과 서버 추론이 함께 import해 training-serving skew를 구조적으로 차단
- **배포 전 검증 게이트** — 예측 분포 붕괴(v3 사고) 재발 방지용 5개 게이트를 통과해야 배포
- **Probability Calibration** — Isotonic Regression으로 예측 확률을 실제 승률에 정합
- **SHAP 기여도** — 코인별 예측 근거를 피처 단위로 설명
- **모델 버전 관리** — 날짜별 보관(최근 10개) + 롤백 + 이력 추적
- **자가 평가 리포트** — 매일 예측 vs 실제 결과를 손익분기와 대조해 조치 문구까지 생성, 주 1회 모델 수정 방향 진단

### 검증 · 데이터 · 알림
- **섀도우(가상) 거래** — 라이브와 동일 조건 + 슬리피지 반영 가상 체결로 전략 변경을 무손실 검증
- **뉴스 센티멘트** — 6개 RSS 소스(영문 4 + 한국어 2), 키워드 기반 감성 점수 + 치명적 이슈(hack/delist) 감지
- **Discord 알림** — 매수/매도, 일일 요약, ML 평가, 레짐 전환, critical 뉴스, 서버 상태
- **WebSocket** — 실시간 시세 브로드캐스트(폴링 폴백 지원)

---

## 🛠 기술 스택

| 분류 | 사용 기술 |
|------|-----------|
| 프레임워크 | FastAPI, Uvicorn |
| ML | XGBoost, scikit-learn(Isotonic Calibration), SHAP |
| 데이터 | pandas, numpy, pyupbit |
| DB | SQLite (WAL 모드) |
| 비동기 | asyncio, httpx |
| 인프라 | EC2, Caddy(리버스 프록시 · 자동 HTTPS), systemd |

---

## 🏗 아키텍처

```
업비트 API ──▶ collector(WebSocket) ──▶ shared_data(실시간 시세)
                                              │
                  ┌───────────────────────────┤
                  ▼                           ▼
            backtester                  trade_manager (매매 오케스트레이터)
      (일일 스캔 · 자가평가)           ├─ strategy (앙상블 점수)
                  │                    ├─ ml_predictor (XGBoost 익절확률)
                  ▼                    ├─ order_executor (실주문)
            ml_predictor  ◀────┐       ├─ paper_repository (섀도우 가상체결)
         (예측/평가/버전관리)  │       └─ notifier (Discord)
                  │           │
     news_collector           └── feature_engineering_v4
     (뉴스 + 센티멘트)             ⚠️ Colab 학습 ↔ 서버 추론 공용 모듈
                  │                  (피처 불일치 구조적 차단)
         ┌────────┴────────┐
         ▼                 ▼
    FastAPI REST       WebSocket
    /market /trade     /ws/prices
    /news              
```

### 매수 판단 흐름

```
1. 시장 레짐 체크 (하락장 → 전체 차단)
2. 심야/쿨오프/블랙리스트 체크
3. 종목별: 앙상블 점수 ≥ 5.5 (기술적 셋업)
4.        AND ML 익절확률 ≥ 0.42 (= 손익분기 36.4% + 수수료·마진)
5.        AND 진입 필터 (급등/고점/과열 · 매도벽 · 치명적 뉴스 차단)
6. 통과 후보 → 익절확률 순 정렬 → Kelly 사이징 → 매수
   (SHADOW_MODE면 실주문 대신 슬리피지 반영 가상체결)
```

---

## 📁 프로젝트 구조

```
app/
├── main.py                       # FastAPI 앱 + lifespan(백그라운드 태스크)
├── api/
│   ├── market_api.py             # 시세/분석/ML 상태·버전·롤백
│   ├── trade_api.py              # 봇 제어/수동매매/설정/통계/섀도우
│   ├── news_api.py               # 뉴스/센티멘트
│   └── ws_api.py                 # WebSocket 시세 브로드캐스트
├── core/
│   ├── config.py                 # 환경변수 로드
│   ├── auth.py                   # API 키 인증(쓰기 엔드포인트)
│   ├── database.py               # SQLite 초기화 + 마이그레이션
│   ├── trade_repository.py       # 실거래 DB CRUD + 통계
│   └── paper_repository.py       # 섀도우 가상거래 저장소 (동일 인터페이스)
└── services/
    ├── trade_manager.py          # 핵심 오케스트레이터 (매수/매도 루프)
    ├── strategy.py               # 앙상블 전략 (지표 → 점수)
    ├── ml_predictor.py           # XGBoost 예측/평가/버전관리
    ├── feature_engineering_v4.py # ★ 피처 단일 진실 공급원 (학습·추론 공용)
    ├── backtester.py             # 일일 스캔 + 백테스트 + ML 자가평가
    ├── order_executor.py         # 실주문 (슬리피지 반영)
    ├── news_collector.py         # 뉴스 수집 루프
    ├── news_sentiment.py         # 키워드 센티멘트 스코어러
    └── notifier.py               # Discord 알림 (Embed)

notebooks/
├── CoinMate_ML_Minute_Training.ipynb  # Colab 학습 (현행 분봉 v4)
├── feature_engineering_v4.py          # ★ app/services 사본 (내용 동일 유지)
├── validate_model_v4.py               # 배포 전 5게이트 검증
├── V4_RETRAIN_GUIDE.md                # v3 → v4 재설계 근거 + 재학습 절차
└── legacy_daily_model_training.ipynb  # 구 일봉 모델 학습 (폐기 라인 · Optuna 튜닝 / LightGBM 비교 기록)

scripts/
├── backup_db.sh                       # DB 일일 백업 (systemd timer, 7일 보관)
└── health_check.sh                    # 5분 헬스체크 + 디스크 경보 → Discord
```

---

## 🚀 시작하기

### 사전 요구사항
- Python 3.11+
- 업비트 Open API 키 (매매용)

### 설치

```bash
git clone https://github.com/DONRAJI/CoinMate-Backend.git
cd CoinMate-Backend
pip install -r requirements.txt
```

### 환경변수

루트에 `.env` 파일 생성:

```env
# 업비트 Open API
UPBIT_ACCESS_KEY=your_access_key
UPBIT_SECRET_KEY=your_secret_key

# 매수 한도 (원)
MAX_BUY_AMOUNT=100000

# Discord 웹훅 (선택)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# API 키 인증 (선택 — 미설정 시 인증 비활성)
API_KEY=your_random_secret

# CORS 허용 도메인 (쉼표 구분, 미설정 시 *)
ALLOWED_ORIGINS=https://your-frontend.example.com

# 뉴스 추가 소스 (선택)
CRYPTOPANIC_API_KEY=your_key
```

### 실행

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 처음 실행할 때는 `SHADOW_MODE`를 켜고 섀도우 거래로 동작을 확인한 뒤 라이브로 전환하는 것을 권장합니다.

---

## 📡 주요 API

### 시세 / 분석
| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/market/prices` | 대시보드용 전체 시세 + 요약 |
| GET | `/market/analysis/{ticker}` | 종목 상세 분석 + ML SHAP 근거 |
| GET | `/market/ml/status` | ML 모델 상태 |
| GET | `/market/ml/accuracy` | ML 예측 정확도 로그 |
| GET | `/market/ml/versions` | 모델 버전 목록 |
| POST | `/market/ml/rollback` 🔒 | 모델 롤백 |

### 매매 제어
| Method | Endpoint | 설명 |
|--------|----------|------|
| POST | `/trade/start` 🔒 | 자동매매 시작 |
| POST | `/trade/stop` 🔒 | 자동매매 중지 |
| POST | `/trade/manual/buy` 🔒 | 수동 매수 |
| POST | `/trade/manual/sell` 🔒 | 수동 매도 |
| GET | `/trade/history` | 거래 내역 |
| GET | `/trade/stats` | 거래 통계 |
| GET | `/trade/strategy-stats` | 전략별/레짐별 성과 |
| GET·POST | `/trade/config` 🔒 | 봇 설정 조회/변경 (영속화) |

### 섀도우(가상) 거래
| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/trade/shadow/stats` | 가상 거래 통계 |
| GET | `/trade/shadow/history` | 가상 거래 내역 |
| POST | `/trade/shadow/toggle` 🔒 | 섀도우 모드 ON/OFF |

### 뉴스
| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/news/recent` | 최신 뉴스 |
| GET | `/news/ticker/{ticker}` | 코인별 뉴스 |
| GET | `/news/sentiment/{ticker}` | 코인별 평균 센티멘트 |

### WebSocket
| Endpoint | 설명 |
|----------|------|
| `/ws/prices` | 실시간 시세 브로드캐스트 (2초) |

> 🔒 = `X-API-Key` 헤더 인증 필요 (`API_KEY` 설정 시)

---

## 🧠 ML 모델 학습 · 배포

분봉 모델은 Colab에서 학습한 뒤 산출물을 서버에 올리는 방식입니다.

```
notebooks/CoinMate_ML_Minute_Training.ipynb
```

1. **학습** — Colab에서 5분봉 수집 → `build_features_v4(df, btc_df)` → forward-sim 라벨 → XGBoost + Isotonic calibration
2. **검증** — `python validate_model_v4.py <새모델.pkl>` 로 5개 게이트 통과 확인 (**G2 std ≥ 0.12 · G3 고유값 ≥ 40% · G4 max ≥ 0.45** 등)
3. **배포** — 통과한 `xgb_model.pkl`을 `models/`에 배치 후 서버 재시작

> 피처 함수는 `app/services/feature_engineering_v4.py` **한 곳에서만** 정의합니다. `notebooks/` 사본은 Colab 업로드용이며 내용이 항상 같아야 합니다 — 갈라지면 학습과 추론이 다른 피처를 쓰게 되고, 에러 없이 예측만 조용히 틀려집니다. 재설계 배경과 절차는 [V4_RETRAIN_GUIDE.md](notebooks/V4_RETRAIN_GUIDE.md) 참고.

---

## ⚠️ 면책 조항

본 프로젝트는 **학습 및 연구 목적**으로 제작되었습니다. 암호화폐 자동매매는 원금 손실 위험이 있으며, 본 소프트웨어 사용으로 발생하는 어떠한 금전적 손실에 대해서도 개발자는 책임지지 않습니다. 실제 자금 투입은 전적으로 사용자 본인의 판단과 책임 하에 이루어집니다.
