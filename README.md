# CoinMate Backend

> 업비트(Upbit) 기반 암호화폐 자동매매 봇 — FastAPI + XGBoost ML

AI 앙상블 전략과 머신러닝 익절 예측을 결합해 매수/매도를 자동 판단하는 트레이딩 봇의 백엔드입니다. 다층 안전장치(시장 레짐 필터, Kelly 자금관리, 연속손절 쿨오프)와 뉴스 센티멘트 분석, Discord 실시간 알림을 갖추고 있습니다.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white)
![XGBoost](https://img.shields.io/badge/XGBoost-ML-EB5E28)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)

---

## ✨ 주요 기능

### 매매 엔진
- **앙상블 전략 점수** — 7개 기술적 지표(추세/MACD/RSI·MFI/ADX/거래량/볼린저/VWAP)를 가중 합산한 12.75점 만점 스코어링
- **ATR 기반 동적 손절/익절** — 변동성에 비례한 손절·익절선 자동 조정 + 트레일링 스탑
- **시장 레짐 필터** — BTC 추세(상승/중립/하락) 판정으로 하락장 신규 매수 차단
- **Kelly Criterion 자금관리** — ML 승률 기반 동적 포지션 사이징(1/4 Kelly)
- **다층 안전장치** — 심야 매수 차단, 연속손절 쿨오프(점진 강화), 코인별 블랙리스트

### 머신러닝 (XGBoost)
- **분봉 모델 v3** — 5분봉 데이터 + forward-simulation 라벨("익절 +3.5%가 손절 -2%보다 먼저 닿을 확률")
- **Probability Calibration** — Isotonic Regression으로 예측 확률을 실제 승률에 정합
- **SHAP 기여도** — 코인별 예측 근거를 피처 단위로 설명
- **모델 버전 관리** — 날짜별 보관 + 롤백 + 정확도 자동 추적
- **자가 평가 리포트** — 매일 예측 vs 실제 결과 calibration 검증, 주 1회 모델 수정 방향 진단

### 데이터 & 알림
- **뉴스 센티멘트** — 6개 RSS 소스(영문 4 + 한국어 2) 수집, 키워드 기반 감성 점수 + 치명적 이슈(hack/delist) 감지
- **Discord 알림** — 매수/매도, 일일 요약, ML 평가, 레짐 전환, critical 뉴스, 서버 상태 등
- **WebSocket** — 실시간 시세 브로드캐스트(폴링 폴백 지원)

---

## 🛠 기술 스택

| 분류 | 사용 기술 |
|------|-----------|
| 프레임워크 | FastAPI, Uvicorn |
| ML | XGBoost, scikit-learn(Isotonic Calibration) |
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
         (일일 스캔/백테스트)          ├─ strategy (앙상블 점수)
                  │                    ├─ ml_predictor (XGBoost 익절확률)
                  ▼                    ├─ order_executor (주문 실행)
            ml_predictor               └─ notifier (Discord)
         (학습/예측/평가)
                  │
     news_collector (뉴스 + 센티멘트)
                  │
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
3. 종목별: 앙상블 점수 ≥ 임계값 (기술적 셋업)
4.        AND ML 익절확률 ≥ 손익분기 기반 임계값 (승률)
5.        AND 진입 필터 (급등/고점/과열 차단)
6. 통과 후보 → 익절확률 순 정렬 → Kelly 사이징 → 매수
```

---

## 📁 프로젝트 구조

```
app/
├── main.py                    # FastAPI 앱 + lifespan(백그라운드 태스크)
├── api/
│   ├── market_api.py          # 시세/분석/ML 상태·버전·롤백
│   ├── trade_api.py           # 봇 제어/수동매매/설정/통계
│   ├── news_api.py            # 뉴스/센티멘트
│   └── ws_api.py              # WebSocket 시세 브로드캐스트
├── core/
│   ├── config.py              # 환경변수 로드
│   ├── auth.py                # API 키 인증(쓰기 엔드포인트)
│   ├── database.py            # SQLite 초기화 + 마이그레이션
│   └── trade_repository.py    # 거래 DB CRUD + 통계
└── services/
    ├── trade_manager.py       # 핵심 오케스트레이터 (매수/매도 루프)
    ├── strategy.py            # 앙상블 전략 (지표 → 점수)
    ├── ml_predictor.py        # XGBoost 학습/예측/평가
    ├── backtester.py          # 일일 스캔 + 백테스트 + ML 평가
    ├── order_executor.py      # 실주문 (슬리피지 반영)
    ├── news_collector.py      # 뉴스 수집 루프
    ├── news_sentiment.py      # 키워드 센티멘트 스코어러
    └── notifier.py            # Discord 알림 (Embed)
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

## 🧠 ML 모델 학습

분봉 모델은 별도 Colab 노트북에서 학습 후 모델 파일을 서버에 업로드하는 방식입니다.

```
notebooks/CoinMate_ML_Minute_Training.ipynb
```

1. Colab에서 노트북 실행 (5분봉 수집 → 피처 → forward-sim 라벨 → 학습 + calibration)
2. 생성된 `xgb_model.pkl`을 `models/`에 배치
3. 서버 재시작

> ⚠️ 노트북의 `build_features`와 서버 `ml_predictor.build_features`는 **반드시 1:1 일치**해야 합니다.

---

## ⚠️ 면책 조항

본 프로젝트는 **학습 및 연구 목적**으로 제작되었습니다. 암호화폐 자동매매는 원금 손실 위험이 있으며, 본 소프트웨어 사용으로 발생하는 어떠한 금전적 손실에 대해서도 개발자는 책임지지 않습니다. 실제 자금 투입은 전적으로 사용자 본인의 판단과 책임 하에 이루어집니다.
