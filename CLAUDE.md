# CLAUDE.md — CoinMate 프로젝트

## 언어 설정
모든 응답은 **한국어**로 작성한다.

## 프로젝트 개요
업비트(Upbit) 거래소 기반 암호화폐 자동매매 봇. AI 앙상블 전략 + XGBoost ML로 매수 판단, FastAPI 백엔드 + React 프론트엔드 구성.

---

## 인프라
| 항목 | 값 |
|------|-----|
| 백엔드 | Python 3.11 + FastAPI, EC2 **t3.small (2GB RAM + 스왑 2GB)** |
| 리전 | **ap-northeast-2 (Seoul)** ← 세션12에서 Sydney→Seoul 이전 (업비트 latency 30배↓) |
| EC2 IP | **43.201.190.36** (Elastic IP eipalloc-09f7038d52084e41b) |
| Instance ID | i-0885a2e0c07dee1a7 |
| 보안그룹 | sg-087f50b713f678080 (SSH 22 from 211.238.109.169/32, HTTP 80, HTTPS 443) |
| EC2 사용자/경로 | `ec2-user`, `/home/ec2-user/CoinMate-Backend` |
| PEM 키 | `F:\Downloads\coinmate.pem` (Sydney/Seoul 공통, 공개키 import) |
| 프론트엔드 | React 19 + TypeScript + Vite, Vercel 자동배포 |
| 프론트 URL | https://coin-mate-frontend.vercel.app |
| 도메인(API) | coinmate1.duckdns.org (Caddy 리버스 프록시 → :8000) |
| 백엔드 repo | github.com/DONRAJI/CoinMate-Backend |
| 프론트 repo | github.com/DONRAJI/CoinMate-frontend |
| DB | SQLite (`coin_mate.db`, WAL 모드) |

### 환경변수
- **EC2 `.env`** (`~/CoinMate-Backend/.env`): `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`, `MAX_BUY_AMOUNT`, `DISCORD_WEBHOOK_URL`, `API_KEY`(쓰기 인증), `ALLOWED_ORIGINS`(CORS)
- **Vercel**: `VITE_API_URL`(백엔드 주소), `VITE_API_KEY`(= EC2 API_KEY와 동일, 쓰기 인증)
- ⚠️ API_KEY는 git 비커밋. 키 교체 시 EC2 `.env` + Vercel 둘 다 변경 후 각각 재시작/재배포

### systemd 서비스/타이머 (EC2)
- `coinmate.service` — 메인 봇 (start/stop/restart/status)
- `db-backup.timer` — 매일 자정 DB 백업
- `coinmate-health.timer` — 5분마다 헬스체크 → Discord (`scripts/health_check.sh` → `~/health_check.sh`)

## 배포 명령어
```bash
# 백엔드 EC2 배포 (Seoul)
ssh -i "F:\Downloads\coinmate.pem" ec2-user@43.201.190.36 "cd ~/CoinMate-Backend && git pull origin main && sudo systemctl restart coinmate"

# 프론트엔드: git push하면 Vercel 자동배포

# DuckDNS A 레코드 업데이트 (도메인 IP 변경 시)
curl "https://www.duckdns.org/update?domains=coinmate1&token={TOKEN}&ip={NEW_IP}&verbose=true"
# 캐시 stale 시: clear=true 한 번 호출 후 다시 ip 설정 (SOA serial 강제 갱신)
```

---

## 핵심 설정값 (trade_manager.py)
| 설정 | 값 | 설명 |
|------|-----|------|
| MAX_COIN_COUNT | 5 | 최대 동시 보유 코인 수 |
| MIN_ORDER_KRW | 6,000 | 최소 주문 금액 |
| BUY_THRESHOLD | 5.5 | 매수 최소 점수 (12.75점 만점) |
| PROFIT_TARGET | 3.5% | 익절 목표 |
| STOP_LOSS | -2.0% | 손절 라인 |
| TRAILING_ACTIVATION | 1.5% | 트레일링 스탑 활성화 |
| TRAILING_DISTANCE | 1.2% | 트레일링 스탑 폭 |
| REBUY_COOLDOWN | 1800초 (30분) | 매도 후 재매수 쿨타임 |
| ML_MIN_PROB | 0.55 | ML 최소 확률 (bull) |
| NEUTRAL_SCORE_FLOOR | 6.5 | neutral 시 score 최소 (세션10) |
| NEUTRAL_ML_FLOOR | 0.60 | neutral 시 ML 최소 (세션10) |
| ATR 손절 배수(추세/횡보) | 1.5 / 1.2 | 세션10에서 확대 |
| 자금관리 | per-pick Kelly (1/4 안전계수) | 세션16, ML prob 기반 동적 사이즈 |
| Kelly 범위 | 5%~40% | KELLY_MIN/KELLY_MAX |
| NIGHT_BUY_BLOCK | 22~07시 | 심야 매수 차단 |
| CONSECUTIVE_LOSS_LIMIT | 3연패 | 연속 손절 시 1시간 쿨오프 |
| MIN_HOLD_MINUTES | 30분 | 최소 보유 시간 (점수하락 매도 차단) |
| COIN_LOSS_STREAK_LIMIT | 2연패 | 같은 코인 연속 손절 시 2시간 블랙리스트 |
| volume 가중치 | 0.75 | 거래량 폭발 (하향: 1.5→0.75) |
| 업비트 수수료 | 0.05% (편도), 0.1% (왕복) | |

---

## 매매 전략
### 앙상블 점수 (12.75점 만점)
| 전략 | 가중치 |
|------|--------|
| trend (MA20 추세) | 2.5 |
| macd (모멘텀) | 2.5 |
| oscillator_group (RSI/MFI 통합) | 3.0 |
| adx (추세 강도) | 1.5 |
| volume (거래량 폭발) | 0.75 (하향됨) |
| bollinger (밴드 반등) | 1.5 |
| vwap (세력 평단가) | 1.0 |

### 매수 필터 (점수 통과 후 추가 검증)
점수 >= 6.0 통과해도 아래 조건이면 스킵:
- RSI >= 75 (과열)
- MFI >= 85 (과열)
- RSI >= 65 && MFI < 35 (괴리)
- 슈팅스타 캔들 (윗꼬리 > 몸통 * 2)
- 저거래량 펌프 (가격 +3% 이상인데 거래량 < MA20)
- 극단 변동성 (고저 범위 > 10%)
- 쿨타임 30분 내 재매수 차단

### 매도 조건
- 익절: 수익률 >= PROFIT_TARGET (4%)
- 손절: 수익률 <= STOP_LOSS (-2.5%)
- 트레일링 스탑: 2% 이상 수익 후 고점 대비 1.5% 하락 시

### ML 예측 (XGBoost v2)
- 38개 피처 (OHLCV + 기술적 지표 + 변동성 + 모멘텀)
- 하루 1회 학습 (daily scan 시), 전 종목 확률 산출
- 현재 정확도: 62.4% (테스트), 67.6% (학습) — 244개 코인 기준
- 차단 필터가 아닌 **정렬 우선순위**로만 사용
- SHAP(pred_contribs) 기반 코인별 개별 근거 제공
- ML 예측 정확도 자동 추적: `cache/ml_accuracy_log.json`
- API: `/market/ml/accuracy`, `/market/ml/status`

---

## 아키텍처 — 백엔드
```
app/
├── main.py                    # FastAPI 앱, 스케줄러 시작
├── api/
│   ├── market_api.py          # /market/prices, /analysis/{ticker}, /ml/status, /ml/accuracy, /ml/versions, /ml/rollback, /client-error
│   ├── news_api.py            # /news/recent, /news/ticker/{ticker}, /news/sentiment/{ticker}, /news/stats [Phase 1A]
│   ├── ws_api.py              # /ws/prices (WebSocket)
│   └── trade_api.py           # /trade/manual/buy, /trade/manual/sell, /trade/history, /trade/stats
├── core/
│   ├── config.py              # 환경변수 로드
│   ├── database.py            # SQLite init
│   ├── logger.py              # 로거
│   └── trade_repository.py    # DB CRUD (trades 테이블)
├── services/
│   ├── trade_manager.py       # 핵심 오케스트레이터 (매수/매도 루프, 시장 스캔)
│   ├── strategy.py            # 앙상블 전략 (10개 지표 → 점수)
│   ├── order_executor.py      # 실제 주문 실행 (실제 체결가 기록)
│   ├── upbit_client.py        # pyupbit 래퍼 (환경변수: UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)
│   ├── ml_predictor.py        # XGBoost 모델 학습/예측
│   ├── backtester.py          # 백테스트
│   ├── collector.py           # 데이터 수집
│   ├── notifier.py            # 텔레그램 알림
│   └── data_loader.py         # 데이터 로더
└── models/
    └── xgb_model.pkl          # 학습된 ML 모델
```

## 아키텍처 — 프론트엔드
```
src/
├── App.tsx, main.tsx
├── api/marketApi.ts           # API 호출 함수
├── types/common.ts            # MarketData, AnalysisDetail, TradeRecord, TradeStats 타입
├── components/
│   ├── Dashboard/Dashboard.tsx      # 메인 대시보드 (보유자산, 주도주, AI추천, ML Top, 거래성과)
│   ├── CoinModal/CoinModal.tsx      # 종목 상세 모달 (차트, 전략, ML예측, 수동매매)
│   ├── TradeHistory/TradeHistory.tsx # 거래 내역 테이블
│   └── ControlPanel/ControlPanel.tsx # 봇 ON/OFF 제어
```

## DB 스키마 (trades 테이블)
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| ticker | TEXT | 예: KRW-BTC |
| buy_price | REAL | 실제 체결 매수가 (슬리피지 반영) |
| buy_amount | REAL | 투자 금액 (KRW) |
| buy_time | TEXT | |
| sell_price | REAL | 실제 체결 매도가 (슬리피지 반영) |
| sell_time | TEXT | |
| status | TEXT | open / closed |
| profit_rate | REAL | 수익률 (%) |
| strategy_name | TEXT | 예: trend+macd+volume 또는 Manual(수동) |
| sell_reason | TEXT | 예: stop_loss, trailing, Manual(수동) |
| buy_score | REAL | [P1] 매수 시점 앙상블 점수 |
| buy_ml_prob | REAL | [P1] 매수 시점 ML 상승확률 (수동매수는 NULL) |
| buy_regime | TEXT | [P1] 매수 시점 BTC 레짐 (bull/neutral/bear) |
| buy_rsi | REAL | [P1] 매수 시점 RSI (수동매수는 NULL) |
| buy_news_sentiment | REAL | [Phase 1B] 매수 시점 24h 평균 뉴스 sentiment |
| buy_news_critical_count | INTEGER | [Phase 1B] 매수 시점 24h 내 치명적 뉴스 건수 |

---

## 작업 이력 (세션별 변경사항)

### 세션 1~2 (5/18~5/25): 초기 구축 & 기본 기능
- FastAPI 백엔드 + React 프론트엔드 구성
- 업비트 API 연동 (pyupbit), 자동매매 루프 구현
- 10개 기술적 지표 기반 앙상블 전략 구현 (strategy.py)
- EC2 배포, Caddy 리버스 프록시, systemd 서비스 설정
- 대시보드 카드 UI, 코인 상세 모달, TradingView 차트 연동

### 세션 3 (5/26): 대규모 개선 작업

#### 1. ML/AI 시스템 추가 (XGBoost)
- **신규**: `ml_predictor.py` — XGBoost 분류기 (23개 피처, 하루 1회 학습)
- **EC2 설치 이슈**: xgboost가 Python 3.9에 설치됨 → 서버는 3.11 사용. `python3.11 -m pip install`로 재설치
- **디스크 부족**: tmpfs 용량 초과 → `TMPDIR=/home/ec2-user/tmp_pip`로 우회
- **joblib 누락**: 같은 Python 버전 불일치 문제. 동일 방법으로 해결
- **첫 학습 수동 실행**: 244개 코인, 61.6% 정확도

#### 2. ML 차단 → 정렬 전환
- **문제**: ML 55% 기준으로 필터링하면 전 종목이 차단되어 매수 자체가 불가능
- **이전**: `if ml_prob < 0.55: continue` → 55% 미만이면 매수 차단
- **수정**: ML을 차단 필터에서 제거, 후보 정렬 우선순위로만 사용
- **변경 코드**: `candidates.sort(key=lambda x: (x.get('ml_prob', 0.5), x['score']), reverse=True)`
- **향후**: 정확도 향상 후 다시 필터로 전환 검토

#### 3. 프론트엔드 ML 예측 표시
- **신규**: CoinModal에 ML 예측 섹션 추가 (상승 확률, 모델 정확도, 학습일, 피처 수)
- **신규**: 대시보드 카드에 `AI {확률}%` 표시
- **신규**: `/market/ml/status` API 엔드포인트
- **수정**: `market_api.py` — analysis 응답에 `ml_prob`, `ml_status` 필드 추가
- **수정**: `common.ts` — MarketData, AnalysisDetail 타입에 ML 관련 필드 추가

#### 4. 총 손익(KRW) 지표 추가
- **문제**: 평균 수익률 0.04%만 보여서 실제로 돈을 잃고 있는지 알 수 없었음
- **이전**: 거래 성과에 승률, 평균수익률만 표시
- **수정**: `trade_repository.py`에 수수료 반영 총 손익 SQL 추가
- **수정**: 프론트 거래 성과에 "총 손익" 항목 추가, 5개 항목 한 줄 그리드로 변경
- **손익 공식**: `buy_amount * (1 - 0.0005) * (1 + profit_rate/100) * (1 - 0.0005) - buy_amount`

#### 5. 슬리피지 추적 시스템
- **문제**: 시그널 가격(분석 시점)과 실제 체결 가격이 달라 손익이 부정확
- **이전**: `order_executor.py`가 시그널 가격을 그대로 DB에 기록
- **수정 (매수)**: 체결 후 `avg_buy_price` API로 실제 평균 매수가 조회
- **수정 (매도)**: 주문 UUID로 체결 내역 조회 → `total_funds / total_volume`으로 실제 매도가 계산
- **기존 데이터 보정**: `fix_trades.py`로 40건의 기존 거래를 업비트 `/v1/orders/closed` API와 매칭하여 실제 매도가로 업데이트 (총 슬리피지: -450원)
- **제한**: 업비트 API가 매수 주문 내역을 보관하지 않아 기존 매수가는 보정 불가. 향후 매수는 실시간 조회로 정확히 기록됨

#### 6. 전량 매수/매도 기능
- **문제**: 수동 매도 시 보유 코인 정확한 수량을 모르면 매도 불가
- **이전**: 금액 입력 후 매수/매도 버튼만 존재
- **수정**: CoinModal에 "전량 매수 (보유 KRW 전액)" / "전량 매도 (보유 코인 전량)" 체크박스 추가
- **수정**: 백엔드 `place_manual_buy`에서 `amount=0`이면 `krw * 0.9995` (수수료 여유분) 전량 매수

#### 7. NoneType 포맷 에러 수정
- **문제**: `unsupported format string passed to NoneType.__format__` 로그 에러
- **원인**: `dict.get('ml_prob', 0.5)`는 키가 존재하지만 값이 None일 때 None을 반환 (default 값 0.5가 아님)
- **이전**: `ml_p = pick.get('ml_prob', 0.5)` → None이 나와서 `{ml_p:.0%}` 포맷 에러
- **수정**: `ml_p = pick.get('ml_prob') or 0.5` — None이면 0.5 fallback

#### 8. 매수 스킵 사유 표시
- **문제**: 점수가 6.0 이상인데도 매수가 안 되는 종목이 있어서 이유를 알 수 없었음
- **수정 (백엔드)**: `_set_skip_reason()` 헬퍼 추가. 각 필터(RSI과열, MFI과열, 괴리, 슈팅스타, 저거래량펌프, 극단변동성, 쿨타임)에 걸릴 때 `market_status[ticker]["skip_reason"]`에 기록
- **수정 (프론트)**: 카드에서 `score >= 6.0 && skip_reason`이면 빨간 배지로 사유 표시
- **수정 (타입)**: `MarketData`에 `skip_reason?: string | null` 추가

#### 9. 거래 내역 개선
- **이전**: 매수가/매도가 표시, 정렬 미확인
- **수정**: 매수가/매도가 → **투자금/손익(원)** 으로 변경 (예: 투자금 10,000원 → 손익 -52원)
- **정렬**: 백엔드 SQL이 이미 `sell_time DESC` (매도시간 최신순) 적용되어 있음을 확인

---

## 해결한 인프라 이슈 정리

| 이슈 | 원인 | 해결 |
|------|------|------|
| EC2 SSH 접속 불가 | xgboost 컴파일로 메모리 고갈 → 서버 먹통 | EC2 콘솔에서 재부팅 |
| PEM 키 경로 오류 | 이전 세션에서 잘못된 경로 사용 | `F:\Downloads\coinmate.pem` (사용자 직접 제공) |
| xgboost 설치 실패 | pip3가 Python 3.9에 설치, 서버는 3.11 | `python3.11 -m pip install xgboost` |
| 디스크 부족 | tmpfs 용량 초과 | `TMPDIR=/home/ec2-user/tmp_pip` |
| joblib 모듈 없음 | 동일 Python 버전 불일치 | `python3.11 -m pip install joblib` |

---

## 운영 현황 (2025-05-27 기준)
- 운영 기간: 5/18 ~ 5/27 (10일)
- 총 거래: 60건+
- ML 모델: 244개 코인 학습, 테스트 정확도 62.4%, 피처 38개
- 5/27 적용: 심야차단, 연속손절쿨오프, 거래량가중치하향, 최소보유30분, 코인블랙리스트
- 수동 매매: strategy_name "Manual(수동)"으로 DB 기록됨
- 대시보드 섹션: 보유자산 → 거래량주도주 → AI우량주 → **ML상승예측Top(신규)** → 거래성과

---

### 세션 3-2 (5/26 후반): 거래 데이터 분석 & 진입/손절 최적화

#### 거래 분석 결과 (46건)
| 구분 | 건수 | 승률 | 총 손익 |
|------|------|------|---------|
| 손절방어 | 19건 (41%) | 0% | -5,200원 |
| 트레일링 익절 | 12건 | 100% | +3,200원 |
| MFI/RSI 과열 | 6건 | 83% | +1,230원 |
| 점수하락 | 3건 | 0% | -142원 |
| 수동 | 5건 | 40% | -130원 |

**핵심 발견:**
- 1시간 미만 보유 11건의 승률 9.1% (평균 -1.95%) → 고점 진입 후 즉시 손절 패턴
- 1~6시간 보유 19건의 승률 63.2% (평균 +1.23%) → 적정 보유시간대
- 손절 1건(-3.2%)이 트레일링 익절 1건(+3.0%)을 상쇄 → 손익 비대칭 문제

#### 적용한 개선
1. **진입 필터 3종 추가** (trade_manager.py process_buying)
   - 직전 3시간 +5% 이상 급등 종목 차단
   - 직전 1시간 +3% 이상 급등 종목 차단
   - 6시간 최고가 대비 98% 이상 고점 근접 차단
   
2. **손절/익절 라인 최적화** (손실 규모 축소)
   - 이전 → 수정:
   - 손절: -2.5% → **-2.0%** (횡보: -2.0% → **-1.5%**)
   - 익절: 4.0% → **3.5%** (횡보: 2.5% → **2.0%**)
   - 트레일링 활성화: 2.0% → **1.5%**
   - 트레일링 거리: 1.5% → **1.2%**
   - 점수하락 탈출: score<2.5 → **score<3.0**, 즉시 → **-0.5% 이상 손실시**

---

### 세션 3-3 (5/26): 품질 개선

#### 1. 보안
- `fix_trades.py`의 하드코딩 API 키 → 환경변수로 변경
- `.gitignore`에 임시 스크립트(fix_*.py, analyze_*.py, check_*.py) 추가
- GitHub에 키 노출 이력 없음 확인 완료

#### 2. DB 백업 자동화
- `backup_db.sh` 스크립트 → systemd timer로 매일 자정 실행
- 7일 이상된 백업 자동 삭제
- 백업 경로: `~/db_backups/coin_mate_YYYYMMDD_HHMM.db`

#### 3. 매도 사유 정규화
- **이전**: `"손절방어(-3.05%,trending)"` — 매번 다른 문자열, 집계 불가
- **수정**: DB에 `"stop_loss"` 같은 고정 코드 저장, 로그에만 상세 출력
- 코드 목록: `stop_loss`, `trailing`, `take_profit`, `sideways_exit`, `rsi_overheat`, `mfi_overheat`, `score_drop`, `anomaly`
- 프론트: 코드별 한글 라벨 + 아이콘 매핑 (🔴 손절, 🟢 트레일링 등)

#### 4. 설정 API 추가
- `GET /trade/config` — 현재 설정값 조회
- `POST /trade/config` — 실시간 설정 변경 (재시작 불필요, 서버 재시작 시 초기화)
- 변경 가능 항목: stop_loss, profit_target, trailing_activation, trailing_distance, max_coin_count, min_order_krw, rebuy_cooldown, buy_threshold

#### 5. 헬스체크
- `GET /health` — 이미 존재 확인 (bot_active, realtime_feeds, open_trades 포함)

#### 6. 에러 핸들링 정리
- collector.py의 bare `except:` → `except Exception as e:` + 로그 추가
- 전체 코드에서 `except: pass` 패턴 없음 확인

#### 7. 프론트엔드 설정 패널
- ControlPanel에 ⚙️ 버튼 추가 → 클릭 시 설정 패널 토글
- 손절/익절/트레일링/보유한도/쿨타임/매수기준 실시간 변경 가능
- "서버 재시작 시 기본값 복원" 안내 포함

#### 8. React Error Boundary
- `ErrorBoundary` 컴포넌트 추가 → Dashboard 전체를 감싸서 크래시 방지
- 오류 시 "다시 시도" 버튼 표시

### 세션 3-4 (5/26): CI/CD + 테스트 + ATR + ML + 백테스트

#### 1. CI/CD (GitHub Actions)
- `.github/workflows/ci.yml` 생성
- push 시 자동: pytest → 테스트 통과 시 EC2 자동배포
- **GitHub secrets 설정 필요**: `EC2_HOST` (43.201.190.36), `EC2_SSH_KEY` (PEM 키 내용), `EC2_REGION` (ap-northeast-2)

#### 2. 단위 테스트 (23개)
- `tests/test_pnl_calculation.py`: 손익 계산 5건, 진입 필터 7건, 매도 사유 4건
- `tests/test_strategy.py`: Strategy 엔진 7건 (가중치, 데이터 검증, 필수 키, 범위 등)
- 전부 통과 확인

#### 3. ATR 기반 동적 손절 (trade_manager.py process_selling)
- **이전**: 고정 손절/익절 (추세 -2.0%, 횡보 -1.5%)
- **수정**: ATR(변동성) 기반 동적 조정
  - 추세장: `stop_loss = max(-2.0%, -(ATR% * 1.2))`, `profit_target = max(3.5%, ATR% * 2.0)`
  - 횡보장: `stop_loss = max(-1.5%, -(ATR% * 1.0))`, `profit_target = max(2.0%, ATR% * 1.5)`
  - 변동성 낮은 코인은 타이트하게, 높은 코인은 넓게 자동 조정

#### 4. ML 피처 강화 (23개 → 29개)
- 신규 6개: hour(시간패턴), momentum_accel(모멘텀가속도), vol_price_corr(거래량-가격상관), hl_range_pct(변동범위), rsi_change(RSI변화율), consecutive_candles(연속봉수)
- 하이퍼파라미터 최적화: depth 5→4, lr 0.05→0.03, 정규화 강화, early stopping 추가

#### 5. 백테스트 동기화
- backtester.py의 매도 조건을 trade_manager.py와 완전 일치시킴
- 손절 -2.0%, 익절 3.5%, 트레일링 1.5/1.2, 급등필터, 점수하락 조건 동기화

---

### 세션 4 (5/27 전반): 전체 거래 분석 기반 5대 개선

#### 분석 결과 (57건, 5/18~5/27)
- 승률 40.4%, 추정 총 손익 -1,388원
- **심야(22~03시) 매수 = 승률 11%, -3,143원** (최대 손실 구간)
- **0~30분 보유 = 승률 9%** (11건 중 10건 패)
- **volume 전략 포함 시 승률 25%** (미포함 시 56%)
- **최대 7연패** (-1,337원), RENDER 4전 4패

#### 적용한 개선
1. **심야(22~07시) 매수 완전 차단** (`process_buying` 진입부에서 return)
   - 기대: -3,143원 → 0원 절감
2. **연속 3패 시 1시간 매수 중단** (쿨오프)
   - `recent_trade_results` 리스트로 최근 10건 추적
   - 3연패 감지 시 `loss_cooloff_until` 설정
3. **volume 전략 가중치 하향** (1.5 → 0.75)
   - 총점 13.5 → 12.75, BUY_THRESHOLD 6.0 → 5.5
4. **최소 보유시간 30분** — 점수하락/이상징후 매도만 차단 (손절은 즉시 실행)
5. **같은 코인 2연패 시 2시간 블랙리스트**
   - `coin_loss_streak`, `coin_blacklist_until` 딕셔너리로 추적

#### 변경 파일
- `trade_manager.py`: 개선 1~5 전체 적용
- `strategy.py`: volume 가중치 0.75, BUY_THRESHOLD 5.5

---

### 세션 5 (5/27 후반): SHAP 기반 ML 근거 + ML Top 섹션 + 정확도 추적

#### 1. SHAP 기반 ML 근거 (ml_predictor.py)
- **문제**: `predict_with_reasons()`가 `model.feature_importances_` (전역값) 사용 → 모든 코인 동일 근거 표시, 17% 확률 코인도 "상승" 화살표
- **수정**: XGBoost `pred_contribs` (SHAP) 사용으로 완전 재작성
  - `model.get_booster().predict(xgb.DMatrix(last_row), pred_contribs=True)` → 코인별 고유 기여도
  - SHAP 양수 = 상승 확률 기여(up), 음수 = 하락 기여(down)
  - `|SHAP|` 절대값 순 정렬 → 가장 영향 큰 피처 8개 표시
- **결과**: BTC(19%) → 고저범위(-0.72, down), ATR(-0.41, down) / AKT(59%) → 거래량변화(-0.23, down), BB수축(+0.14, up) — 코인별 완전히 다른 근거

#### 2. AI 상승 예측 Top 섹션 (신규)
- **문제**: 대시보드가 거래량 주도주 + AI 추천만 표시 → ML이 높은 확률을 준 코인이 후보에 없으면 아예 확인 불가
- **백엔드**:
  - `backtester.py._analyze_one()`: 일일 스캔 시 `ml.predict(df)` 호출, `ml_prob` 캐시에 저장
  - `backtester.py.get_ml_top_coins(top_n=10)`: 50%+ 확률 상위 10개 반환
  - `trade_manager.py.update_frontend_cache()`: `ml_top_coins` 배열을 `frontend_cache`에 포함
- **프론트엔드**:
  - `Dashboard.tsx`: 파란색 테마의 "🧠 AI 상승 예측 Top" 섹션 추가 (AI 우량주와 거래 성과 사이)
  - `common.ts`: `MlDetailReason`에 `shap` 필드 추가
  - `CoinModal.tsx`: ML 근거 차트를 SHAP 기여도 기반으로 변경

#### 3. ML 예측 정확도 자동 추적 (신규)
- **구현**: `backtester.py._evaluate_yesterday_predictions()`
  - 매일 스캔 시작 전 자동 실행
  - 전일 캐시(`analysis_YYYY-MM-DD.json`)의 ml_prob + 당시 가격 → 현재 가격과 비교
  - 1%+ 상승 여부 (label 기준과 동일)를 실제 결과와 대조
  - 전체 정확도, Top10 정확도, Top10 평균 변동률 기록
- **저장**: `cache/ml_accuracy_log.json`에 날짜별 누적
- **API**: `GET /market/ml/accuracy` 엔드포인트 추가
- **활용**: 정확도 추이를 추적하여 모델 개선 효과 측정, 향후 ML 필터 재도입 기준점으로 활용

#### 4. CoinModal ML 차트 SHAP 전환
- `importance` 기반 바 차트 → `shap` 절대값 기반으로 변경
- 값 표시: 전역 중요도 숫자 → SHAP 기여도 (+0.144, -0.232 등)
- 색상: 양수(초록, 상승 기여) / 음수(빨강, 하락 기여)

#### 변경 파일 & 배포
- **백엔드** (EC2 SCP + systemctl restart):
  - `ml_predictor.py`: predict_with_reasons() SHAP 재작성
  - `backtester.py`: ml_prob 저장 + get_ml_top_coins() + 정확도 평가
  - `trade_manager.py`: frontend_cache에 ml_top_coins 포함
  - `market_api.py`: /market/ml/accuracy 엔드포인트
- **프론트엔드** (git push → Vercel 자동배포):
  - `Dashboard.tsx`: ML Top 섹션 + ml_top_coins 파싱
  - `CoinModal.tsx`: SHAP 기반 근거 표시
  - `common.ts`: MlDetailReason shap 필드

#### ML 현황 (5/27 재학습 후)
- 학습 데이터: 31,994행 / 테스트: 7,999행
- 학습 정확도: 67.6% / 테스트 정확도: 62.4%
- 피처 수: 38개
- ML Top 코인 예시: AZTEC(75.9%), BIRB(68.8%), BIO(64.9%), ORCA(64.6%)

---

### 세션 6 (5/28): 9연속 손절 원인 분석 & 시장 레짐 필터

#### 거래 분석 (마지막 승리 WLD +4.92% 이후 8연속 손절)
- AKT, NXPC, SEI, JTO, FF, JTO, AZTEC, IN — 전부 -0.9~-2.9% stop_loss
- **모든 코인이 매수 직후 하락** → 개별 코인 문제가 아닌 시장 전체 하락 신호

#### 근본 원인: 시장 전체 추세(BTC) 필터 부재
- BTC 3일 연속 하락 (5/26 -2.18%, 5/27 -2.56%, 5/28 -1.47%, 누적 -6.5%)
- 시간봉 MA24 대비 -2.2%, 24h -3.68% → 명백한 하락장
- 봇은 개별 코인 단기 상승신호(trend+adx+macd)만 보고 매수 → 하락장에서 알트는 BTC보다 더 빠져 전부 손절
- 1시간 쿨오프는 미봉책 (끝나면 같은 하락장 재진입)
- `recent_trade_results` 인메모리라 재시작 시 초기화됨

#### 적용한 개선 (trade_manager.py)
1. **[핵심] 시장 레짐 필터** `_get_market_regime()`
   - BTC 시간봉 MA24 위치 + 6h 모멘텀으로 bull/neutral/bear 판정 (30분 캐시)
   - bear: `현재가 < MA24 AND 6h모멘텀 < -1.0%` → `process_buying` 진입부에서 전체 매수 차단
   - 데이터 실패 시 fail-open (neutral, 매수 허용), 직전 캐시 fallback
2. **점진적 쿨오프** — 연패 지속 시 1h → 2h → 4h 강화 (`cooloff_level`, MAX 3단계), 승리 시 0으로 리셋
3. **재시작 시 연패 복원** `_restore_loss_state()` — DB 최근 10건으로 `recent_trade_results` + 쿨오프 재구성
4. **프론트 안내 배너** — summary에 `market_regime`, `cooloff_remaining_min` 추가, Dashboard 상단에 하락장/쿨오프 배너 표시

#### 검증 결과 (배포 후)
- 복원: "최근 거래 10건, 8연패" 정확히 인식
- 쿨오프: 8연패 → cooloff_level 3 → 4시간(240분) 자동 적용
- 레짐: "BTC레짐=bear (MA24 -2.3%, 6h -1.9%)" 정확히 감지 → 현재 매수 차단 중

#### 신규 인스턴스 변수 (trade_manager.py __init__)
```python
self.cooloff_level = 0; self.MAX_COOLOFF_LEVEL = 3
self._market_regime_cache = None; self._market_regime_ts = 0
self.MARKET_REGIME_TTL = 1800; self._last_bear_log = 0
```

---

### 세션 7 (5/28): P0 보안/안정성 개선 (완료)

5개 P0 항목 전부 적용·검증 완료.

#### 1. 스왑 2GB (OOM 방지) ✅
- EC2에 `/swapfile` 2GB 생성, `/etc/fstab` 영속화, `vm.swappiness=10`
- 풀스캔(244종목) 시 메모리 부족분을 디스크로 흡수 → OOM 다운 방지
- t3.small 유료 업그레이드는 추후 검토 (사용자 선택: 일단 스왑)

#### 2. SQLite WAL 모드 ✅
- `database.py init_db`: `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`
- `trade_repository.get_conn` + `data_loader`: `timeout=5.0` + `PRAGMA busy_timeout=5000`
- 매매루프 쓰기 + API 읽기 동시 접근 시 lock 에러 방지 (WAL은 DB파일에 영속)

#### 3. API 키 인증 (쓰기 엔드포인트만) ✅
- `core/auth.py verify_api_key`: `X-API-Key` 헤더 == `config.API_KEY` 검증
- 적용: `/trade/start`, `/stop`, `/manual/buy`, `/manual/sell`, `/config`(POST)
- 읽기(prices/history/stats/analysis/config GET)는 개방 유지
- `API_KEY` 미설정 시 통과(로컬 개발 호환) → EC2 `.env`에만 실제 키 존재
- **프론트**: `marketApi.ts` + `CoinModal.tsx`가 `VITE_API_KEY`를 헤더로 전송
- **키 보관 위치**: EC2 `.env`의 `API_KEY`, Vercel 환경변수 `VITE_API_KEY` (git 비커밋)
- 검증: 키 없이 401, 키 있으면 200, 읽기 200
- ⚠️ SPA 특성상 키가 브라우저 번들에 노출됨 — 완벽한 보안 아님(봇/스캐너 차단 + 키 교체 가능 수준). 멀티유저 전환 시 JWT 등으로 대체 필요

#### 4. 헬스체크 다운 알림 ✅
- `scripts/health_check.sh`: `/health` 2회 연속 비정상 시 Discord 1회 알림, 복구 시 복구 알림
- systemd timer `coinmate-health.timer` (5분 간격, crontab 미설치라 timer 사용)
- 상태파일 `/home/ec2-user/.health_state` (failcount alerted)

#### 5. CORS 도메인 제한 ✅
- `main.py`: `allow_origins=config.ALLOWED_ORIGINS` (env 쉼표구분, 미설정 시 `*` 폴백)
- `allow_credentials`는 특정 origin일 때만 True (와일드카드와 동시 사용 불가)
- EC2 `.env`: `ALLOWED_ORIGINS=https://coin-mate-frontend.vercel.app` (trailing slash 없이)
- 검증: 허용 origin 헤더 반영, 비허용 차단, preflight OPTIONS에 x-api-key 허용

#### 프론트 URL & 배포
- 프론트엔드 운영 URL: **https://coin-mate-frontend.vercel.app**
- 백엔드 배포: SCP + `systemctl restart coinmate`
- Vercel 환경변수 추가 후에는 반드시 redeploy해야 빌드에 주입됨 (확인법: 배포된 번들 JS에 키 문자열 존재 여부 grep)

#### ⚠️ 운영 주의 (세션7 추가)
- 백엔드 재시작 시 자동매매는 항상 OFF로 시작 → 대시보드에서 재활성화 필요
- `/trade/start`도 이제 인증 필요 → 프론트는 자동 처리, 수동 curl 시 `-H "X-API-Key: ..."` 필수

---

### 세션 8 (5/28): BTC 레짐 가시화 + P1 매수 컨텍스트 저장

#### 1. BTC 시장 레짐 상태 바 (항상 표시)
- `trade_manager._get_market_regime`: 레짐 판정 시 상세(`btc_price`, `ma24_dev_pct`, `mom6_pct`)를 `_market_regime_detail`에 저장
- `update_frontend_cache`에서 `_get_market_regime()` 호출 (30분 캐시라 실제 조회는 30분마다) → 쿨오프/심야로 process_buying이 조기 return돼도 BTC 지표 항상 갱신
- summary에 `market_regime`, `btc{...}`, `cooloff_remaining_min`, `night_block` 노출
- **프론트**: 대시보드 상단에 항상 보이는 2개 칩
  - BTC 레짐 칩: 🐂/😐/🐻 + 가격 + MA24이격% + 6h모멘텀%
  - 매수 상태 칩: ✅ 매수 가능 / 🚫 차단(하락장·쿨오프 N분·심야 사유 표기)

#### 2. [P1] 매수 시점 컨텍스트 저장
- **DB**: trades 테이블에 `buy_score, buy_ml_prob, buy_regime, buy_rsi` 컬럼 추가
  - `database.py`: CREATE TABLE에 포함 + 기존 DB용 마이그레이션(PRAGMA table_info로 누락 컬럼 ALTER ADD, idempotent)
- **저장 경로**: `process_buying`에서 buy_context 생성 → `executor.try_buy(..., context)` → `repo.log_buy(..., context)`
  - 수동 매수(`place_manual_buy`)도 regime 기록 (score/ml_prob/rsi는 None)
- **표시**: TradeHistory에 "매수근거" 컬럼 (점수 / AI% / 레짐 이모지)
- **활용**: 향후 "레짐=bear에서 산 거래 승률", "ml_prob 높을수록 승률 상관관계" 등 분석 → 레짐 필터/ML 임계값 튜닝 근거
- 검증: EC2 마이그레이션 완료(4컬럼 확인), INSERT 컬럼/값 일치 확인

#### P1 남은 항목 (미진행) → 세션 9에서 전부 완료

---

### 세션 9 (5/28): P1 잔여 작업 완료 (전략성과/ML버전/에러로깅)

#### 1. 전략별 성과 추적 ✅
- `trade_repository.get_strategy_stats()`: 거래를 3가지로 집계
  - **개별 성분별**: trend/adx/macd/volume/vwap/bollinger/rsi/mfi 각각이 포함된 거래의 승률/평균/손익
  - **전략 조합별**: strategy_name 전체 문자열 단위
  - **레짐별**: buy_regime(bull/neutral/bear/미기록) 단위 — 레짐 필터 효과 추적
- `GET /trade/strategy-stats` (읽기, 무인증)
- **프론트**: `StrategyStats` 컴포넌트 + 거래성과 섹션에 "전략별 성과" 토글 버튼
- **첫 분석 결과** (60여건): volume 성분 승률 25.9%/-2,910원(최악), trend+adx+vwap+macd 조합 50%/+386원(최선) → volume 추가 축소/제거 검토 근거

#### 2. ML 모델 버전 관리 + 롤백 ✅
- `_save_model`: 저장 시 `models/versions/xgb_model_YYYYMMDD_HHMMSS.pkl`로 복사 보관, `model_history.json`에 메타(날짜/정확도/피처수) 기록, 최근 10개만 유지(prune)
- `train()`: 정확도 급락 감지(이전 대비 -10%p 또는 50% 미만 시 경고 로그)
- `list_versions()` / `rollback(filename=None)` (미지정 시 직전 버전)
- `GET /market/ml/versions`(읽기), `POST /market/ml/rollback`(쓰기 인증)
- 베이스라인 버전 1개 시드 완료 (61.76%, 38피처)

#### 3. 프론트 에러 로깅 (Sentry 대신 자체 구현) ✅
- Sentry는 외부 계정 필요 → 자체 구현: `ErrorBoundary.componentDidCatch`가 에러를 백엔드로 fire-and-forget POST
- `POST /market/client-error` (쓰기 인증) → `notifier.notify_error` → Discord 전달
- message/url 길이 제한(truncate)
- 검증: 키 없이 401, 키 있으면 200 + Discord 테스트 메시지 도착 확인

#### 변경 파일
- 백엔드: `trade_repository.py`, `trade_api.py`(strategy-stats), `ml_predictor.py`(버전관리), `market_api.py`(ml versions/rollback + client-error)
- 프론트: `StrategyStats.tsx`(신규), `Dashboard.tsx`(토글), `marketApi.ts`, `ErrorBoundary.tsx`

#### P1 상태: 전부 완료 ✅ (다음은 P2)

---

### 세션 10 (6/1): 3일 운영 점검 & 약세장 대응 강화

#### 운영 점검 결과 (5/28~6/1, 3일)
- ✅ 모든 자동화 정상: 서비스 무재시작 3일, 일일 분석 매일 생성, ML 매일 학습(5버전 보관, 정확도 61.76~62.16%), ML 정확도 5일치 자동 평가, DB 백업/헬스체크 정상, 24h 에러 0건
- ✅ 매수 컨텍스트 완전 기록 (score/ml_prob/regime/rsi)
- ✅ 레짐 필터 bear 차단 입증 (3일간 bear 매수 0건)
- ⚠️ 디스크 78% (journal 372MB 증가) → 한도 100MB로 영구 제한, 즉시 정리 → 75%
- ⚠️ 거래 9건 승률 11%, 손익 -1,433원. 특히 neutral 4건 전패(-2.36%)
- ⚠️ ML Top10 5일 평균 변동 -1.65% (약세장에서 신뢰도 하락)

#### 적용한 개선 4가지
1. **레짐 판정 강화**: bull 기준 `cur > MA24 × 1.003 AND mom6 > 0.5%`로 엄격화 (이전: `>0` → 작은 반등도 bull)
2. **레짐별 매수 임계값 차등**:
   - bull: score≥5.5 / ML≥55% (기본)
   - neutral: score≥6.5 / ML≥60% (강제 상향)
   - bear: 차단 (기존)
   - 인스턴스 변수: `NEUTRAL_SCORE_BONUS`, `NEUTRAL_ML_BONUS`, `NEUTRAL_SCORE_FLOOR`, `NEUTRAL_ML_FLOOR`
3. **손절선 ATR 비례 확대** (`process_selling`):
   - 추세장: ATR 1.2배 → **1.5배**, profit 2.0배 → 2.5배
   - 횡보장: ATR 1.0배 → **1.2배**, floor -1.5 → -1.8
   - floor STOP_LOSS는 config 그대로(-2.0), 필요 시 ControlPanel에서 조정
4. **디스크 모니터링** (`health_check.sh`):
   - 디스크 85% 초과 시 Discord 1회 알림 (복구 시 자동 리셋)
   - `/home/ec2-user/.disk_alert_state` 사용

#### 인프라 추가 조치
- **journald 한도** `/etc/systemd/journald.conf.d/size.conf`: SystemMaxUse=100M, KeepFree=200M, MaxRetentionSec=14day (영구 적용, journal 무한 증가 차단)

#### 변경 파일
- 백엔드: `trade_manager.py`(레짐+임계값+ATR), `scripts/health_check.sh`(디스크 알림)
- 프론트: `Dashboard.tsx`(ML Top 약세장 경고 배지)
- EC2: `/etc/systemd/journald.conf.d/size.conf`(신규), `~/health_check.sh` 갱신

#### 다음 모니터링 포인트 (2~3일 후)
- neutral 거래 빈도 감소 + 승률 향상 여부
- ATR 손절 완화로 진입 직후 손절 비율 감소 여부
- bull로 분류되는 빈도 (너무 적으면 기준 약간 완화 검토)

---

### 세션 11 (6/1): P2 성능/UX 4종 완료

#### 1. 설정 영속화 ✅
- `cache/user_config.json`에 ControlPanel 변경값 자동 저장
- `trade_manager._load_persisted_config()`: __init__ 마지막에 호출, 8개 설정 + buy_threshold 복원
- `trade_manager.save_persisted_config()`: POST /trade/config 호출 시 자동 저장
- 검증: profit_target 3.5→3.7 변경 → 재시작 → 3.7 유지 확인

#### 2. ML 실시간 분석 보강 ✅
- 기존: `cached_day_dfs`에 없는 비선정 코인은 모달에서 ml_reasons 미표시
- 수정: analysis 엔드포인트에서 캐시 미스 시 pyupbit으로 day_df 즉시 조회 → ML 근거 계산
- 폴백: 조회 실패 시 일일 스캔의 ml_prob만 사용
- 검증: KRW-DOGE(비선정) 분석 시 ml_prob 29.3% + SHAP 근거 8개 정상

#### 3. 대시보드 모바일 반응형 ✅
- 3단계 미디어쿼리: 992px(태블릿), 768px(모바일), 480px(소형)
- 자산패널 wrap, divider 숨김, 폰트 축소
- 카드/통계/섹션 타이틀 크기 조정
- index.html title "frontend" → "CoinMate Pro" + theme-color 메타

#### 4. WebSocket 실시간 ✅
- 백엔드 `app/api/ws_api.py` 신규: `/ws/prices` + ConnectionManager + broadcast_loop(2초)
- main.py lifespan에서 broadcast_loop 백그라운드 태스크 시작
- 프론트 Dashboard.tsx: WS 우선 연결 + 실패/끊김 시 5초 폴링 폴백, 자동 재연결
- 폴링/WS 공용 파서 `applyPriceData()` 추출
- 검증: WS 연결 즉시 type=prices 메시지 + summary 키 정상 수신
- CORS 영향 없음 (Caddy reverse_proxy WS Upgrade 자동 지원)

#### 변경 파일
- 백엔드: `main.py`, `trade_manager.py`(영속화), `trade_api.py`(영속화 호출+안내문구), `market_api.py`(ML 보강), `ws_api.py`(신규)
- 프론트: `Dashboard.tsx`(WS+파서 분리), `Dashboard.module.css`(반응형), `ControlPanel.tsx`(영속화 안내), `index.html`(타이틀)
- EC2 파일: `cache/user_config.json`(런타임 생성)

---

### 세션 12 (6/1): Sydney → Seoul 리전 이전 + t3.small 업그레이드

#### 동기
- 업비트 서버는 한국 — Sydney에서 150ms vs Seoul 5ms → 슬리피지/체결 정확도 향상
- t3.micro 1GB RAM 한계 → 풀스캔 시 OOM 위험 (스왑으로 완화하긴 했지만 헤드룸 부족)
- 가격 비교: 두 옵션 거의 동가 (~$20/월)

#### 변경 사항 비교
| 항목 | 이전 (Sydney) | 이후 (Seoul) |
|---|---|---|
| 리전 | ap-southeast-2 | **ap-northeast-2** |
| 인스턴스 | t3.micro (1GB) | **t3.small (2GB)** |
| Public IP | 15.134.82.85 (EIP) | **43.201.190.36 (EIP)** |
| 업비트 latency | ~150ms | **~5ms** |
| 보안그룹 | sg-0d3354ad01650e0ae | sg-087f50b713f678080 |
| Instance ID | i-01a4592c0cebb3c08 | i-0885a2e0c07dee1a7 |
| 도메인 | coinmate1.duckdns.org (불변) | 동일 (DuckDNS A 레코드만 갱신) |

#### 마이그레이션 절차 (실수 시 참고)
1. 봇 정지 + DB 로컬 백업
2. EC2 stop → create-image (AMI) → copy-image to Seoul (10-30분)
3. Seoul에 import-key-pair(공개키만) + create-security-group + allocate-address(EIP)
4. AMI에서 t3.small run-instances + associate-address(EIP)
5. DuckDNS A 레코드 업데이트
6. **업비트 API 키 IP 화이트리스트 새 IP 추가** (사용자 수동)
7. 검증 후 Sydney terminate + AMI/snapshot 삭제 + EIP release

#### 주의사항 (다음 마이그레이션 시)
- **업비트 IP 화이트리스트** — API 키별로 허용 IP를 등록해야 함. IP 바뀌면 거래 차단 (`no_authorization_ip`)
- **DuckDNS NS 캐시 stale 가능성** — 글로벌 resolver가 옛 IP를 끈질기게 캐시할 수 있음. `&clear=true` 한 번 호출 후 다시 IP 설정 = SOA serial 강제 갱신 = 즉시 전파
- **EC2 키 페어는 리전별** — 공개키만 import하면 같은 PEM으로 양 리전 접속 가능 (`ssh-keygen -y -f` 로 추출 → `import-key-pair`)
- WAL DB 파일은 AMI에 그대로 복제됨 (일관성 OK)
- AMI는 1회용이라 마이그 후 즉시 deregister + snapshot 삭제 (월 $0.05/GB 절감)

#### 마이그레이션 중 발견된 버그 (별도 개선 필요)
- **좀비 청산 오작동**: `executor.get_all_balances()`가 API 인증 실패로 빈 리스트 반환 → 코드가 "지갑 비었음"으로 오해 → `close_zombie_trade` 호출 → DB의 open trade 사라짐
- 이번 마이그 시 INJ가 이렇게 잘못 청산됐고, 백업 DB에서 trade #81 status='closed'→'open' SQL UPDATE로 복원함
- **수정 필요**: `get_all_balances` 응답에 `error` 키가 있거나 비정상 응답일 때 좀비 처리 건너뛰는 안전장치 추가
- 추가 보호: 매도 신호 1회만으로 close_zombie 하지 말고 N회 연속 검증 후에 실행

#### 결과
- 봇 30~60분 다운타임 (실제 ~50분)
- 모든 상태 (DB 81건, ML 모델 + 버전, 설정, 캐시) 무손실 이전
- INJ 잔고 1.004개 보존, 컨텍스트(buy_score 5.75, ml_prob 69.77%, regime neutral) 그대로
- 메모리 1GB → 1.9GB (+ 스왑 2GB), CPU 1 → 2 vCPU
- **이전 비용**: AMI 데이터전송 ~$0.16, 잠시 스냅샷 저장 — 합 $1 미만

---

### 세션 13 (6/1): Phase 1A 뉴스 기반 분석 (수집 + 센티멘트 + 표시)

#### 아키텍처
```
[15분마다]
  news_collector.loop()
    ├─ 4개 RSS 병렬 fetch (httpx, follow_redirects)
    │   • CoinDesk / CoinTelegraph / Decrypt / The Block
    ├─ ticker 추출: 본문 정규식 (업비트 KRW 마켓 심볼 3-8자, lazy cache)
    ├─ score_text(): 키워드 기반 sentiment (-1.0~+1.0) + is_critical 플래그
    │   • critical: hack/exploit/scam/delist/규제 등 (영/한)
    │   • +/- 키워드: launch/partnership/rally vs crash/plunge/dump
    └─ INSERT OR IGNORE INTO news (external_id UNIQUE = source:url)

[일 1회]
  cleanup_old() — 30일 이상 자동 삭제

[REST API]
  /news/recent?limit=N        → 최신 N건
  /news/ticker/{TICKER}?hours → 코인별 N시간 내
  /news/sentiment/{TICKER}    → 코인별 평균 센티멘트 + critical 카운트
  /news/stats                 → 수집기 상태 (모니터링용)
```

#### 핵심 결정
- **CryptoCompare/CoinGecko/CryptoPanic 모두 무료 API 차단됨** (2025+ 정책 변경) → RSS 기반으로 전환
- **RSS는 ticker 메타데이터 없음** → 업비트 KRW 마켓 심볼 화이트리스트 정규식으로 본문 매칭 (3-8자만 — 1-2자는 일반 영단어와 충돌)
- **CryptoPanic는 옵션** — `CRYPTOPANIC_API_KEY` env 있으면 자동 추가
- **stdlib only**: xml.etree, email.utils, re — feedparser 등 추가 의존성 없음

#### 첫 수집 결과
- 4 RSS 소스에서 114건/사이클 (~250건/시간 가능)
- critical 자동 감지 정확 (AAVE exploit, ICO hack 등)
- ticker 추출 부분 작동 (XRP, SUI, AAVE, GAS 등 — false-positive 'ORDER' 같은 케이스 존재, 추후 튜닝)

#### 프론트
- `NewsFeed.tsx`: 필터(전체/긍정/부정/치명적), 최근 50건, 최대 520px 스크롤
- Dashboard 새 섹션 "📰 시장 뉴스" + 토글 (ML Top과 거래성과 사이)
- 센티멘트 이모지: 🚨(critical) 🟢(>0.5) ↗️(>0) ⚪(0) ↘️(<0) 🔴(≤-0.5)

#### 신규 파일
- `backend/app/services/news_sentiment.py` (키워드 사전 + score_text)
- `backend/app/services/news_collector.py` (수집 루프)
- `backend/app/api/news_api.py` (REST)
- `frontend/src/components/NewsFeed/NewsFeed.tsx`

#### 변경 파일
- `backend/app/core/database.py` (news 테이블 + 인덱스)
- `backend/app/main.py` (collector lifespan task + router)
- `frontend/src/api/marketApi.ts` (getRecentNews/getTickerNews/getTickerSentiment)
- `frontend/src/components/Dashboard/Dashboard.tsx` (뉴스 섹션 + 토글)

#### Phase 1A 미진행 (다음 후보)
- **1B 매매 통합**: process_buying에서 후보 코인의 4h 내 critical 뉴스가 있으면 매수 차단(veto), 평균 sentiment를 score에 가산
- **1C 실시간 알림**: 보유 코인 critical 발견 시 Discord 알림
- **1D 한국어 RSS**: 코인데스크코리아/토큰포스트 RSS 추가, ticker 매칭에 한글 코인명 dict
- **ticker 추출 정확도 개선**: 'ORDER' 같은 false positive 대응 (일반 영단어 블랙리스트, 또는 컨텍스트 기반)

---

### 세션 14 (6/1): Phase 1B — 뉴스 표시 + 매수 컨텍스트 기록 (매매에는 영향 없음)

#### 의사결정 배경
- 1A 수집기는 동작하지만 **신뢰성/정확도가 검증되지 않음**:
  - 키워드 기반 sentiment (LLM 아님) — 일부 false positive/negative
  - ticker 추출 정규식 — 'ORDER' 같은 false positive 존재
  - 1사이클(114건)만 누적, 뉴스↔실제 가격 변동 상관관계 미검증
- 사용자 의견: "지표/참고용으로 쓰고 직접 영향은 자제"
- **레짐 필터 도입 패턴 재사용**: 먼저 컨텍스트 기록 → 데이터 누적 → 사후 분석 → 효과 확신 시 매매 통합 (Phase 1B-2 후속)

#### A. 참고용 표시 (사용자가 봇 결정을 보완)
- **카드 뱃지** (Dashboard): 24h 내 뉴스 있는 코인에 `📰 N` (sentiment 중립~긍정) / `🚨 N` (critical), 색상으로 sentiment 표현
- **CoinModal 뉴스 섹션**: 클릭한 코인의 48h 뉴스 리스트 (sentiment + critical 표시)
- **TradeHistory**: 매수근거 컬럼에 `📰/🚨` 추가
- **MarketData 타입 확장**: `news: {count, sentiment, critical}` (선택적)

#### B. 매수 시점 컨텍스트 기록 (분석 기반 데이터)
- DB 마이그레이션 (idempotent ALTER ADD):
  - `trades.buy_news_sentiment REAL`
  - `trades.buy_news_critical_count INTEGER`
- `trade_manager.process_buying` + `place_manual_buy`에서 매수 직전 `news_collector.get_ticker_summary(ticker, 24)` 호출 → buy_context에 포함
- `log_buy`에 두 필드 저장

#### 성능 최적화: news_collector.get_all_ticker_summaries()
- 매 사이클마다 ticker별 SQL 호출 대신 **1회 쿼리로 전 ticker 집계**
- 1분 캐시 (`SUMMARY_CACHE_TTL=60`) — 매수 루프 1초 주기에도 부하 미미
- `update_frontend_cache`에서 호출 → 모든 카드 item에 news 메타 부착

#### 검증 (배포 직후)
- Migration: 두 컬럼 자동 추가 확인
- `/market/prices` 응답: 11개 카드 중 4개에 뉴스 메타 (XRP -0.6, ETH 🚨critical, NEAR 0, BTC 3건 0) 정상

#### 매매에는 영향 없음 (의도적)
- veto/score 가산 등 **자동 매매 로직 미적용**
- 다음 매수부터 컨텍스트가 DB에 쌓이며, 1~2주 후 데이터로 효과 분석 → Phase 1B-2(매매 통합 결정) 또는 1C(알림)로 진행

#### 변경 파일
- 백엔드: `database.py`(마이그레이션), `trade_repository.py`(log_buy 확장), `news_collector.py`(summary 캐시), `trade_manager.py`(buy_context+frontend_cache)
- 프론트: `types/common.ts`, `Dashboard.tsx`(2종 카드 뱃지), `CoinModal.tsx`(뉴스 섹션), `TradeHistory.tsx`(뉴스 컬럼)

#### Phase 1 잔여 (효과 확인 후 결정)
- **1B-2 매매 통합**: 데이터 1~2주 누적 + win-rate 차이 입증 후 critical-only veto 또는 sentiment 가산 활성화
- **1C 실시간 알림**: 보유 코인 critical 발견 시 Discord 알림 (구현 부담 작음, 가치 큼 — 다음 후보)
- ~~**1D 한국어 RSS**~~ → 세션 15에서 완료

### 세션 15 (6/1): Phase 1D — 한국어 뉴스 통합

#### 추가된 RSS (영문 4 + 한국어 2 = 총 6개)
| 소스 | URL | 비고 |
|---|---|---|
| 토큰포스트 | `https://www.tokenpost.kr/rss` | 50건/cycle |
| 블록미디어 | `https://www.blockmedia.co.kr/feed` | 10건/cycle |

테스트해보고 응답 안 되거나 RSS 폐쇄된 소스: 코인데스크코리아(404 HTML 응답), 디센터(404), 코인리더스(404), 코인니스KR(ConnectError) → 위 2개만 사용

#### 한글 코인명 → ticker 매핑
- 업비트 `/v1/market/all` API에서 자동 추출 (lazy load, 1회)
- **3자 이상 한글명만 사용** (1-2자 한글명 "신/위" 또는 "가스/네오/트론"은 일반 단어와 충돌 위험)
- 총 212개 매핑 (BTC/ETH/ADA/NEAR 등)

#### `_extract_tickers()` 알고리즘
```
1. 한글 매칭 (긴 이름 우선 + 매칭 후 마스킹으로 substring overlap 방지)
   예: "이더리움 클래식" → ETC (이후 텍스트에서 "이더리움" 마스킹되어 ETH 중복 매칭 방지)
2. 영문 ticker 정규식 매칭 (대문자 변환 + \b 단어경계)
3. dict.fromkeys로 dedup + 최대 6개
```

#### 검증 (배포 직후)
- 한 사이클 174건 (영문 114 + 한국어 60)
- 신규 60건, 치명적 3건
- ticker 추출 예시:
  - `"비트코인 73,031달러, 이더리움 1,985달러"` → `BTC, ETH, DOGE, SOL` (정확)
  - `"카르다노 재단, 2026년 서밋 취소"` → `ADA` (정확)
- 한글 매핑 로드 로그: `>>> 🗞️ [News] 한글 코인명 매핑 212개 로드`

#### 알려진 노이즈
- 토큰포스트는 코인 외 일반 경제 뉴스도 섞임 (삼성전자 등). ticker 추출 안 돼서 카드 뱃지엔 영향 없음
- 일반 영단어 충돌 가능 (예: 'ORDER', 'UP2') — 영문 ticker 추출 부분의 한계, false positive 블랙리스트는 미세 튜닝 단계로 미뤘음

#### Phase 1 남은 미진행
- **1B-2 매매 통합** (데이터 누적 후): critical veto / sentiment 가산
- **1C 실시간 알림**: 보유 코인 critical 즉시 Discord 알림 (다음 추천)

---

### 세션 16 (6/1): 자금관리 Kelly + ticker 블랙리스트 (안정화)

#### 배경
- 사용자 결정: Phase 2(선물거래) 진입 전 현 봇 안정화
- 동기:
  - 고정 `MAX_SINGLE_RATIO = 0.40` 은 승률 24%, b≈1.29인 현 상황엔 과다 노출
  - 뉴스 ticker 추출에서 ORDER/GAME/PAY 같은 일반 영단어 false positive

#### Kelly Criterion 자금관리
- `trade_repository.get_kelly_stats(limit=50)`: 최근 50건 closed로 (count, win_rate, avg_win, avg_loss) 반환
- `trade_manager._compute_kelly_fraction(pick, stats)`:
  - `f = p - (1-p)/b`, `b = avg_win/avg_loss`
  - **p (승률) = ML prob 우선 사용** (per-pick 추정 더 정확), fallback history win rate
  - **1/4 Kelly 안전계수** + clip 5%~40%
  - 음수 edge (f≤0) → KELLY_MIN(5%, 탐색용 최소)
- `process_buying`: 고정 `MAX_SINGLE_RATIO` 대신 per-pick `pick_max = available_krw * k_frac`
- 한 줄 로그: `💼 [Kelly] 통계 N=50, 승률=24%, 평균승=2.85%, 평균손=-2.21%`
- 검증 (현재 통계):
  | ML prob | 이전(고정 40%) | Kelly | 변화 |
  |---|---|---|---|
  | 55% | 40% | **5.0%** | -87% (KELLY_MIN clamp) |
  | 60% | 40% | **7.2%** | -82% |
  | 70% | 40% | **11.7%** | -71% |
  | 80% | 40% | **16.1%** | -60% |
- 효과: 약한 신호엔 최소 베팅, 강한 신호엔 자연스럽게 증가. 승률 올라가면 자동 확대.

#### ticker 블랙리스트
- `news_collector.TICKER_BLACKLIST`: ORDER, GAS, GAME, PAY, WIN, NOW, PRO, TOP, UP, UP2, NEW, ALL, BIG, GOOD, CASH, NEXT, PLAY, LOVE
- `_extract_tickers`에서 dedup + 6개 trim 전에 블랙리스트 필터
- **봇 매매에는 영향 0** (자체 score/ML 기반), 카드 뱃지/뉴스 표시만 정확해짐
- 검증: "Coinbase launches new game features, BTC at all-time high" → `[BTC]` 만 추출

#### 변경 파일
- `trade_repository.py` (get_kelly_stats)
- `trade_manager.py` (_compute_kelly_fraction + process_buying 적용)
- `news_collector.py` (TICKER_BLACKLIST + 필터)

---

### 세션 17 (6/2): ML 평가 버그 수정 + 적응형 ML 임계값

#### 발견된 문제
- 사용자 지적: "처음이랑 다르게 지금 전체적으로 상승확률이 너무 높게 잡힌다"
- 데이터로 확인:
  | 날짜 | 평균 ml_prob | ≥55% 코인 | ≥60% 코인 |
  |---|---|---|---|
  | 5/29 | 0.438 | 20% (48) | 11% (26) |
  | 6/02 | **0.517** | **33% (80)** | **16% (40)** |
  → 5일 만에 평균 +8%p 우 시프트. 고정 임계값 0.55 → 통과율 자연 증가 (위험)
- ML 정확도 평가가 5/31에서 멈춰있던 원인: `pyupbit.get_current_price(batch)`가 단일 상폐 ticker로 전체 'Code not found' 실패 → log entry 안 만들고 종료

#### 수정 1: ML 평가 batch 실패 fallback (`backtester._evaluate_yesterday_predictions`)
- **활성 KRW 마켓 사전 필터**: `pyupbit.get_tickers()` 결과와 intersect → 상폐 코인 사전 제외
- **batch 100개씩 호출** + 실패 시 **개별 호출 fallback**: 한 ticker가 실패해도 나머지 살림
- 효과: 6/1 평가 백필 성공 (수동 호출). 향후 자동 사이클에서도 안정

#### 수정 2: 적응형 ML 임계값 (`trade_manager._get_adaptive_ml_min`)
- 그 날 전 코인의 ml_prob 분포에서 **상위 (100 - percentile)%** 기준값 계산 (기본 percentile=85, 즉 상위 15%)
- `process_buying`에서 regime-based base_min과 max() → 더 엄격한 쪽 적용
- 예 (6/2 분포 기준):
  - bull base 0.55 → adaptive **0.606** (자동 +5.6%p 상향)
  - neutral base 0.60 → adaptive 0.606 (거의 동일)
- 효과: 분포 시프트되어 모델이 전반적으로 후한 점수 줘도 자동으로 상위만 통과시킴 (약한 신호 매수 차단)

#### 신규 인스턴스 변수
```python
self.ADAPTIVE_ML_PERCENTILE = 85       # 상위 15%
self.ADAPTIVE_ML_MIN_SAMPLE = 30       # 표본 부족 시 비활성
```

#### 검증 (배포 후)
- 6/1 백필 성공: 전체 56.8%, Top10 40%, Top10 평균변동 -3.11%
- 6일 누적 Top10 평균변동 **-1.9%** (약세장 영향, 적응형 임계값으로 개선 기대)
- 분포 percentile별 임계값 시뮬레이션:
  | percentile | 임계값 | 통과 코인 |
  |---|---|---|
  | 70% | 0.555 | 74 |
  | 80% | 0.581 | 49 |
  | **85% (기본)** | **0.606** | **37** |
  | 90% | 0.624 | 25 |
  | 95% | 0.665 | 13 |

#### 변경 파일
- `app/services/backtester.py` (`_evaluate_yesterday_predictions` 견고화)
- `app/services/trade_manager.py` (`_get_adaptive_ml_min` + `process_buying` 통합)

---

### 세션 18 (6/2): Probability Calibration (Isotonic) 적용

#### 문제 진단
- 6일치 ml_accuracy_log 분석 결과 **calibration error 평균 +40%p** 발견
- 예: ML 70~80% 구간 → 실제 양성률 26.7% (49%p 차이)
- 원인: XGBoost raw output은 분류는 잘하지만 **확률값 자체는 의미 없음** (단순 결정경계만 의미)

#### 적용한 calibration (`ml_predictor.train()`)
- **3-way 시계열 분할**: 70% 학습 / 15% 보정(cal) / 15% 테스트
  - 학습 set: XGBoost fit
  - 보정 set: `sklearn.isotonic.IsotonicRegression` fit (raw → 실제 확률 매핑)
  - 테스트 set: 보정 전후 비교 평가
- `predict()` / `predict_with_reasons()`: calibrator 있으면 자동 보정 적용
- 모델 저장 포맷: `{model, calibrator, feature_names, score, train_date}` (옛 모델은 calibrator=None, 자연스럽게 후방호환)

#### 즉시 효과 (학습 직후 측정)
- 테스트 평균확률: 미보정 52.5% → **보정 68.6%** (실제 양성률 67.9%에 0.7%p 차이로 일치)
- 테스트 정확도: 미보정 64.7% → **보정 69.5%** (+4.8%p)
- 실시간 분포 (전체 244 코인): 6/01 평균 49% → **6/02 평균 36.2%**
- ≥70% 코인: 9개 → 3개 (강한 신호만 남음, 약한 시그널 자동 차단)

#### 학습 자동화 호환
- 매일 자정 학습 사이클에서 자동 calibration 적용
- 적응형 임계값(percentile 85, 세션17)과 함께 두 안전망 작동
- "ML 70% = 실제 70%"가 진짜로 성립 → 매수 임계값/Kelly 자금관리 모두 더 정확

#### 변경 파일
- `app/services/ml_predictor.py` (__init__, _save_model, _load_model, train, predict, predict_with_reasons)

#### 다음 검증 포인트
- 6/3~6/9 동안 ml_accuracy_log의 calibration error 추세 (목표: ≤10%p)
- Top10 평균 실현 변동률이 음수에서 양수로 전환되는지

---

### 세션 19 (6/2): Discord 알림 6종 추가 + Embed 포맷 + 좀비 청산 안전장치

#### 사용자 요청
- 현재 Discord 알림 무엇이 있는지 확인 + 가치 있는 추가 연동 모두 진행

#### 기존 알림 (3종)
- 매수 체결 / 매도 체결 / 에러
- 헬스체크 별도 (`health_check.sh`가 직접 webhook)

#### 신규 알림 (6종) — `notifier.py` 전면 재작성 (Embed 포맷)
| # | 함수 | 트리거 | 색상 |
|---|---|---|---|
| 1 | `notify_lifecycle` | 서버 시작/종료 (main.py lifespan) | 🟣 purple |
| 2 | `notify_critical_news` | 보유 코인 + critical 뉴스 매칭 (Phase 1C) | 🔴 red |
| 3 | `notify_daily_summary` | 매일 23:50 KST (`daily_summary_loop`) | 🟢/🔴 (손익) |
| 4 | `notify_ml_trained` | 매일 자정 ML 학습 후 (`backtester.run_daily_scan`) | 🔵 blue |
| 5 | `notify_regime_change` | BTC 레짐 전환 시 (`_get_market_regime`) | bull🟢/neutral⚫/bear🔴 |
| 6 | `notify_upbit_auth_fail` | 업비트 API 인증 실패 (1시간 throttle) | 🔴 red |

#### Embed 포맷 (vs 이전 plain text)
- title + description + color + fields(name/value/inline) + footer + ISO timestamp
- 색상 가이드: green(0x2ECC71), blue(0x3498DB), yellow(0xF1C40F), red(0xE74C3C), purple(0x9B59B6), gray(0x95A5A6)
- 기존 notify_buy/sell/error도 Embed로 자연스럽게 업그레이드 (호환성 유지)

#### 추가 안전장치: 좀비 청산 차단
- 이전 마이그레이션 시 INJ가 잘못 close_zombie 처리됐던 버그 재발 방지
- `update_target_coins` 진입부에서 `get_all_balances` 응답 검증:
  - `{error: ...}` 응답
  - 비정상 타입
  - 빈 list + DB는 open 있음
- 위 조건 시 → `notify_upbit_auth_fail` 발송 + 좀비 청산 로직 raise로 건너뜀
- 1시간 throttle (`_last_upbit_auth_alert`)

#### 중복 방지 (critical 뉴스)
- `news_collector._alerted_critical_ids` set으로 보낸 external_id 추적
- 200개 상한 + 절반 prune (메모리 보호)

#### 일일 요약 데이터
- 오늘 closed 거래 건수/승률/손익 (수수료 왕복 반영)
- 총 자산/주문가능 KRW (실시간 잔고 조회)
- 보유 코인 수, BTC 레짐 이모지

#### 변경 파일
- `app/services/notifier.py` (Embed 포맷, 6 새 함수)
- `app/main.py` (lifecycle 알림 + daily_summary_loop task)
- `app/services/trade_manager.py` (daily_summary_loop + 좀비 청산 안전장치 + 레짐 전환 알림)
- `app/services/news_collector.py` (보유 코인 critical 알림 + 중복 방지)
- `app/services/backtester.py` (ML 학습 완료 알림)

#### 검증 (배포 후)
- startup 시 lifecycle 알림 자동 발송
- 테스트 호출 3종 (일일요약/레짐전환/ML학습) Discord 도착 확인

---

### 세션 20 (6/8): 분봉 ML 모델 v3 적용 (Colab 학습)

#### 배경 — ML 모델 근본 개선
- 기존 일봉 모델 한계: 라벨 "다음날 1%+ 터치" = 실제 매매 승률과 무관, calibration 후에도 의미 모호
- **Colab에서 5분봉 1년치 + forward-simulation 라벨로 재학습**
  - 라벨: 매수 후 1일 내 **익절(+3.5%)이 손절(-2%)보다 먼저 닿으면 1** = "봇이 익절할 확률"
  - 노트북: `notebooks/CoinMate_ML_Minute_Training.ipynb` (사용자가 제미나이로 편집한 11피처 버전 사용)
  - 결과: **테스트 정확도 71.1%** (일봉 62%보다 향상), Isotonic calibration 포함

#### 모델 사양 (xgb_model_minute_20260608)
- **11 피처**: volatility(20봉 std), ma_5/20/60(원시 가격), rsi(14 rolling), macd/signal/hist(12/26/9 raw), volume_ma_5/20, daily_range_pct
- ⚠️ ma/macd/volume이 **원시 절대값** (비율 아님) — Colab 노트북과 1:1 동일해야 작동
- calibrator: IsotonicRegression (sklearn 1.6→1.8 버전 경고 있으나 정상 작동)

#### 서버 변경
1. `ml_predictor.build_features`: 11피처로 **완전 교체** (Colab과 1:1, 임의수정 금지)
   - FEATURE_LABELS 11개 갱신, get_status version "v3-minute5"
   - 최소 60봉 필요 (ma_60)
2. **자동 재학습 OFF** (`backtester.run_daily_scan`): 일봉 데이터 재학습이 분봉 모델을 덮어쓰므로 비활성. 모델은 Colab에서 학습→수동 업로드(월 1회 권장)
3. **minute5 추론 파이프라인**:
   - `get_smart_candles`: minute5(count 200) 추가 fetch → `cached_5m_dfs`
   - `process_buying` + `refresh_target_scores`: ML 예측을 minute5로
   - `backtester._analyze_one`: 일일스캔 ml_prob도 minute5로 (count 200)
   - `market_api analysis`: 모달도 minute5

#### 검증 (배포 후)
- build_features 컬럼 == 모델 feature_names (순서까지 일치) ✓
- 추론 정상: BTC 0.139, ETH/XRP 0.202, PROVE 0.311 (코인별 다양, calibrated)
- 모달 SHAP: MA20/60/5 하락기여, 변동성 상승기여 → 약세장 정확 반영
- 약세장이라 전반적으로 낮은 확률 (익절보다 손절 먼저 닿을 확률 높음) = 의미 정합적

#### 모델 재학습 사이클 (운영 가이드)
1. Colab 노트북 재실행 (데이터 캐시 덕에 신규분만 다운)
2. 새 `.pkl` 다운로드
3. `scp ... ec2-user@43.201.190.36:/home/ec2-user/CoinMate-Backend/models/xgb_model.pkl`
4. `sudo systemctl restart coinmate`
5. ⚠️ Colab build_features 바꾸면 서버 `ml_predictor.build_features`도 동일하게 교체 필수

#### 중요 의미 변화
- 이제 **"ML 70% = 봇이 익절할 확률 70%"** (이전: "1% 터치 확률")
- ML 확률이 실제 매매 승률과 직접 연결 → Kelly 자금관리 + 적응형 임계값이 더 정확

---

### 세션 21 (6/8): 프론트 ML 의미 정정 + ML 모델 로드맵 확정

#### 프론트 ML 표시 수정 (분봉 모델 의미 반영)
- "상승 확률" → **"익절 확률 (24h내 +3.5% 먼저)"**
- 색상 기준: 고정 0.55 → **손익분기 36.4%** (익절+3.5%/손절-2% → p×3.5=(1-p)×2 → p=0.364)
  - ≥45% 익절유리(초록) / 36.4~45% 기대값+(노랑) / <36.4% 손실기대(빨강)
- "AI 상승 예측 Top" → "AI 익절 예측 Top (분봉 모델)"
- ML Top 임계값 `get_ml_top_coins` 0.5 → **0.364** (손익분기 이상만)
- 84% 버그: 옛 일봉 모델 잔여값 → 오늘 캐시 삭제 후 분봉 재스캔으로 실제값(37.9%) 반영

#### 일봉 모델 보존 확인
- ⚠️ 일봉 모델 **안 지움** — `models/versions/xgb_model_daily_backup_20260608_063235.pkl` + `xgb_model_20260608_000630.pkl` 백업됨

#### 🗺️ 확정된 ML 로드맵 (사용자 합의)
1. **단기 (지금)**: 분봉 모델(v3) 며칠 운영 → `ml_accuracy_log`로 실제 익절 적중률 추적
2. **중기**: 분봉 모델 **피처 추가**(현재 11개 → 더 늘림) → Colab 재학습 → 적용
3. **장기**: **일봉 + 분봉 병행 구조**
   - 일봉 모델: 정확도 개선(현재 60%대 → 향상) 후 사용. 역할 = 중기 **방향성**(우량주 선별)
   - 분봉 모델: 역할 = **진입 타이밍**(익절 vs 손절)
   - 카드에 둘 다 표시 또는 결합 매수 로직 (일봉 통과 → 분봉 타이밍)
   - 두 모델 별도 파일 보관 (`xgb_daily.pkl` + `xgb_minute.pkl`)

---

### 세션 22 (6/9): 매수/매도 로직 분봉 모델 정합성 점검 + 임계값 재설계

#### 🚨 발견·수정한 치명적 버그
- **ML_MIN_PROB 0.55가 분봉 모델과 불일치** → 매수 전면 차단 상태였음
  - 옛 일봉 모델("1% 상승확률")은 분포가 높아 0.55 통과 가능
  - 분봉 모델(익절확률)은 약세장 최대 0.37 → **아무도 0.55 못 넘어 ML 게이트에서 전부 차단**
- 수정: 손익분기 기반 재설계
  - `ML_MIN_PROB` 0.55 → **0.42** (손익분기 36.4% + 수수료/마진 = after-fee +EV)
  - `NEUTRAL_ML_FLOOR` 0.60 → **0.47**
  - 손익분기 계산: 익절+3.5%/손절-2% → p×3.5=(1-p)×2 → **p=0.364**

#### ML 익절확률이 매수에 반영되는 3단계 (사용자 질문 답변)
1. **차단 필터**: 익절확률 < 0.42(bull)/0.47(neutral) → 매수 안 함. ml_prob None(데이터 대기)도 보수적 차단
2. **정렬 우선순위**: 후보를 익절확률 높은 순 정렬 → 슬롯 내에서 높은 것부터 매수
3. **Kelly 사이징**: `p=익절확률`로 베팅크기 결정. 분봉 모델이 실제 승률이라 **Kelly가 이제 정확** (일봉 땐 편법)

#### 점수 로직 — 수정 불필요 (검증 완료)
- 앙상블 점수(strategy.py)는 day+min60 기술적 분석, ML(minute5)과 **독립·상보적**
- 매수 = `score≥5.5/6.5(기술) AND ml_prob≥0.42/0.47(승률) AND 기타필터` = AND 게이트
- 둘이 다른 것을 측정(셋업 품질 vs 단기 승률)하므로 병행이 적절. 임계값도 각자 의미에 맞음

#### ML Top 거래대금 필터 추가 (A)
- `get_ml_top_coins(vol_map, min_vol)`: 거래대금 **10억 이상**만 (매수 AI 기준과 동일)
- 잡코인(2원짜리 등) ML Top 노출 차단 → "화면에 보이는 것 = 실제 살 수 있는 것" 일치
- ⚠️ 매수 경로(`target_coins`)는 원래부터 거래대금 필터(50억/10억) 있어서 **잡코인 매수는 원래 불가능**했음. 표시만 정리한 것

#### 적응형 임계값 — 의미 갱신 (코드 동일)
- 분봉 모델은 calibrated → base(0.42)가 절대 floor
- adaptive는 max()로만 → 강세장에 +EV 코인 많으면 상위 15%만(더 선별), 약세장엔 0.42 유지

#### 약세장 동작 (정상)
- 현재 bear/3연패 쿨오프 → 매수 거의 없음 = **의도된 보수적 동작** (나쁜 환경에서 강제 거래 안 함)
- 강세장 전환 + 익절확률 42%+ 코인 등장 시 매수 재개

#### 향후 고려 (선택)
- score와 ML을 하나로 결합한 통합 스코어 (현재 AND 게이트 → 가중합) 검토 가능
- 일봉 모델 정확도 개선 후 병행 (세션21 로드맵)

---

### 세션 23 (6/9): 분봉 모델 평가 리포트 (일일 calibration + 주간 점검)

#### 일일 평가 재작성 (`_evaluate_yesterday_predictions`)
- 일봉 평가("24h후 1% 상승했나")는 분봉 모델과 안 맞음 → **TP/SL forward 시뮬**로 전면 재작성
- 방식: 전일 예측 시점(파일 mtime)부터 minute15 96봉(24h) 받아서 `_simulate_tp_sl`
  - 익절(+3.5%)이 손절(-2%)보다 먼저 닿으면 win(1), 아니면 loss(0), 같은 캔들이면 보수적 손절
- 기록 지표 (`ml_accuracy_log.json`, model="minute5-v3"):
  - 예측 평균 익절확률 vs **실제 익절률** → calibration_error_pp (+과대/-과소)
  - 구간별(0.3~0.6+) 예측 vs 실제 익절률
  - **매수기준(0.42)↑ 코인의 실제 익절률** ← 가장 중요 (실제 봇이 샀을 코인들)
  - Top10 실제 익절률
- pyupbit 인덱스 tz-naive라 entry_time도 `.replace(tzinfo=None)` 필요 (버그 수정)

#### 주간 모델 점검 (`_weekly_model_review`, 7일마다)
- 최근 7일 minute5-v3 평가 집계 → 진단 + 수정 방향 자동 생성
- 진단 로직:
  - calibration_error > +8%p → "과대평가, 재학습/임계값 상향 권장"
  - < -8%p → "과소평가, 좋은 진입 놓침, 임계값 하향 검토"
  - 매수기준↑ 익절률 < 36.4%(손익분기) → "🔴 임계값 상향 또는 피처 보강 필요"
  - 특정 확률구간 반복 과대평가 → "해당 구간 신뢰 주의"
- `ml_weekly_review.json` 저장, run_daily_scan에서 7일 경과 시 트리거

#### Discord 알림 2종 추가
- `notify_ml_eval_daily`: 매일 평가 후 (예측 vs 실제 vs calibration, 매수기준↑ 익절률)
- `notify_ml_eval_weekly`: 주 1회 점검 (누적 진단 + 권장 방향, 🔴면 빨강)
- 색상: calibration ±8%p 이내 초록, 벗어나면 노랑, 매수기준 미달 빨강

#### 일봉 모델 — 그대로 보존
- 일봉 모델 자체는 미변경 (백업 유지). 평가만 분봉용으로 전환

#### 운영 흐름
- 매일 자정 스캔: 전일 분봉 예측 평가 → Discord 일일 리포트
- 7일마다: 주간 점검 → Discord 수정방향 제안
- ⚠️ 첫 실제 평가는 분봉 예측이 24h 경과한 뒤(약 6/10~)부터 의미있는 데이터. 그 전엔 표본부족으로 skip
- 코드 경로는 옛 파일로 검증 완료 (forward 시뮬 정상 작동)

---

## 당장 해야 하는 개선점 (P2 완료 — 다음은 장기 로드맵 Phase 1~4)

### 🟠 마이그레이션 후 발견된 작은 버그 (시간 날 때)
- **좀비 청산 오작동**: get_all_balances API 실패를 빈 잔고로 오해 → 잘못 close_zombie
- 위치: `app/services/trade_manager.py update_target_coins` (real_balances 처리)
- 수정 아이디어: 응답이 list가 아니거나 error 키 있으면 sync 로직 건너뛰기 + 연속 N회 빈 응답일 때만 zombie 처리

### 🔴 P0: 서버 안정성 & 보안 — ✅ 전부 완료 (세션 7)
| 항목 | 해결 |
|------|------|
| ~~API 인증 없음~~ | ✅ X-API-Key 쓰기 엔드포인트 보호 |
| ~~t3.micro 메모리~~ | ✅ 스왑 2GB (t3.small은 추후 선택) |
| ~~SQLite 동시 접근~~ | ✅ WAL 모드 + busy_timeout |
| ~~서버 다운 알림 없음~~ | ✅ systemd timer 헬스체크 → Discord |
| ~~CORS 미설정~~ | ✅ ALLOWED_ORIGINS 제한 |

### 🟡 P1: 데이터 품질 & 투명성 — ✅ 전부 완료 (세션 8~9)
| 항목 | 해결 |
|------|------|
| ~~거래 시점 컨텍스트 미저장~~ | ✅ buy_score/buy_ml_prob/buy_regime/buy_rsi 저장 (세션8) |
| ~~ML 모델 버전 관리 없음~~ | ✅ 날짜별 .pkl 보관 + 롤백 API + 급락 경고 (세션9) |
| ~~프론트 에러 로깅 없음~~ | ✅ ErrorBoundary → /market/client-error → Discord (세션9) |
| ~~전략별 성과 추적 없음~~ | ✅ 성분/조합/레짐별 집계 + 프론트 패널 (세션9) |

### 🟢 P2: 성능 & UX — ✅ 전부 완료 (세션 11)
| 항목 | 해결 |
|------|------|
| ~~프론트 5초 폴링~~ | ✅ WS 우선 + 폴링 폴백 (`/ws/prices`, broadcast 2초) |
| ~~ML 예측이 선정 종목에만 실행~~ | ✅ analysis 엔드포인트가 비선정 코인도 즉시 day_df 조회→ML 근거 계산 |
| ~~대시보드 모바일 미최적화~~ | ✅ 3단계 미디어쿼리(992/768/480) + 자산패널 wrap + 카드 크기 조정 |
| ~~설정 영속화 안됨~~ | ✅ `cache/user_config.json` 자동 저장/복원 |

---

## 장기 로드맵

### Phase 1: 뉴스 기반 분석 (중기)
- 크립토 뉴스 API (CryptoPanic, CoinGecko News 등) 연동
- NLP 감성 분석 (긍정/부정 점수) → 매수 판단에 가중치 반영
- 특정 코인 관련 뉴스 발생 시 실시간 알림
- 트위터/텔레그램 커뮤니티 센티멘트 수집 고려

### Phase 2: 선물거래 지원 (중기)
- 바이낸스 또는 비트겟 선물 API 연동
- 롱/숏 포지션, 레버리지 설정
- 펀딩비 기반 전략 추가
- 리스크 관리: 최대 레버리지 제한, 강제 청산 방지 로직
- 기존 현물 전략을 선물에 맞게 변환 (방향성 + 진입/청산 기준)

### Phase 3: AI 모델 강화학습 (중기~장기)
- 현재 XGBoost (지도학습) → 강화학습(RL) 에이전트 추가
- 환경: 가격/거래량/지표 상태 → 행동: 매수/매도/홀드 → 보상: 수익률
- DQN 또는 PPO 알고리즘 검토
- ML 정확도 추적 데이터(`ml_accuracy_log.json`)를 피드백 루프로 활용
- 모델 앙상블: XGBoost 확률 + RL 행동 + 전략 점수를 결합

### Phase 4: 멀티유저 서비스 확장 (장기)
- **인증**: Firebase Auth 또는 자체 JWT 인증
- **멀티테넌시**: 유저별 API 키, 설정, 거래 기록 분리
- **DB 전환**: SQLite → PostgreSQL (동시 접근 필수)
- **인프라**: EC2 → ECS/Fargate 또는 k8s (유저 수 스케일링)
- **과금 모델**: 무료 기본 + 프리미엄 (ML 분석, 선물, 뉴스) 검토
- **대시보드**: 유저별 포트폴리오 관리, 수익률 랭킹
- **법적 검토**: 투자 자문업 등록 여부, 면책 조항

---

## 세션 연속 작업 가이드

### EC2 접속 전 항상 확인 (Seoul 리전)
```bash
# 1. 현재 IP 확인
curl -s https://checkip.amazonaws.com
# 2. 보안 그룹에 현재 IP 등록 (이전 IP 제거 후) — Seoul SG
aws ec2 revoke-security-group-ingress --region ap-northeast-2 --group-id sg-087f50b713f678080 --protocol tcp --port 22 --cidr {이전IP}/32
aws ec2 authorize-security-group-ingress --region ap-northeast-2 --group-id sg-087f50b713f678080 --ip-permissions IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges="[{CidrIp={현재IP}/32,Description=SSH}]"
# 3. SSH 접속
ssh -i "F:\Downloads\coinmate.pem" -o StrictHostKeyChecking=no ec2-user@43.201.190.36
```

### 배포 프로세스
```bash
# 백엔드: 파일 SCP 후 재시작
scp -i "F:\Downloads\coinmate.pem" -o StrictHostKeyChecking=no {로컬파일} ec2-user@43.201.190.36:/home/ec2-user/CoinMate-Backend/{경로}
ssh -i "F:\Downloads\coinmate.pem" ec2-user@43.201.190.36 "sudo systemctl restart coinmate"

# 프론트엔드: git push → Vercel 자동배포
cd F:\CoinMate\frontend && git add . && git commit -m "메시지" && git push origin main
```

### EC2 서버 구조
- 코드: `/home/ec2-user/CoinMate-Backend/`
- 캐시: `/home/ec2-user/CoinMate-Backend/cache/` (analysis_*.json, ml_accuracy_log.json)
- ML 모델: `/home/ec2-user/CoinMate-Backend/models/xgb_model.pkl`
- DB: `/home/ec2-user/CoinMate-Backend/coin_mate.db`
- 백업: `/home/ec2-user/db_backups/`
- 서비스: `sudo systemctl {start|stop|restart|status} coinmate`
- 로그: `sudo journalctl -u coinmate --since "10 min ago" --no-pager`

### 주의사항
- EC2 SSH 접속 시 IP가 자주 변경됨 → 매번 보안 그룹 업데이트 필요 (위 가이드 참고)
- EC2 사용자는 `ec2-user` (ubuntu 아님)
- 캐시 삭제 시 풀스캔 재실행됨 (t3.micro에서 약 4~5분 소요)
- PowerShell에서 git commit 시 한글 메시지는 heredoc 깨짐 → `commit_msg.txt` 파일 생성 후 `git commit -F commit_msg.txt` 사용
- ML 모델은 일일 스캔(`run_daily_scan`) 완료 후 자동 재학습됨
