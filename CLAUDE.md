# CLAUDE.md — CoinMate 프로젝트

## 언어 설정
모든 응답은 **한국어**로 작성한다.

## 프로젝트 개요
업비트(Upbit) 거래소 기반 암호화폐 자동매매 봇. AI 앙상블 전략 + XGBoost ML로 매수 판단, FastAPI 백엔드 + React 프론트엔드 구성.

---

## 인프라
| 항목 | 값 |
|------|-----|
| 백엔드 | Python 3.11 + FastAPI, EC2 t3.micro (1GB RAM) |
| EC2 IP | 15.134.82.85 |
| PEM 키 | `F:\Downloads\coinmate.pem` |
| 프론트엔드 | React 19 + TypeScript + Vite, Vercel 자동배포 |
| 리버스 프록시 | Caddy |
| 백엔드 repo | github.com/DONRAJI/CoinMate-Backend |
| 프론트 repo | github.com/DONRAJI/CoinMate-frontend |
| DB | SQLite (`coin_mate.db`) |

## 배포 명령어
```bash
# 백엔드 EC2 배포
ssh -i "F:\Downloads\coinmate.pem" ec2-user@15.134.82.85 "cd ~/CoinMate-Backend && git pull origin main && sudo systemctl restart coinmate"

# 프론트엔드: git push하면 Vercel 자동배포
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
| ML_MIN_PROB | 0.55 | ML 최소 확률 |
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
│   ├── market_api.py          # /market/prices, /market/analysis/{ticker}, /market/ml/status, /market/ml/accuracy
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
- **GitHub secrets 설정 필요**: `EC2_HOST` (15.134.82.85), `EC2_SSH_KEY` (PEM 키 내용)

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

## 당장 해야 하는 개선점 (긴급)

### 🔴 P0: 서버 안정성 & 보안
| 항목 | 현황 | 리스크 | 해결책 |
|------|------|--------|--------|
| API 인증 없음 | 누구나 `/trade/manual/buy` 호출 가능 | 자금 탈취 가능 | API Key 또는 JWT 인증 추가 |
| t3.micro 메모리 | 풀스캔 시 1GB RAM 근접 | OOM → 서버 다운 (이미 경험) | t3.small 업그레이드 또는 스왑 파일 추가 |
| SQLite 동시 접근 | 매매 루프 + API가 동시 쓰기 | DB lock 에러 | WAL 모드 활성화 또는 PostgreSQL 전환 |
| 서버 다운 알림 없음 | 텔레그램은 매매만 알림 | 몇 시간째 다운돼도 모름 | 헬스체크 + 텔레그램 알림 (cron) |
| CORS 미설정 | 모든 origin 허용 추정 | API 악용 가능 | 허용 도메인 제한 |

### 🟡 P1: 데이터 품질 & 투명성
| 항목 | 현황 | 개선 |
|------|------|------|
| 거래 시점 컨텍스트 미저장 | trades 테이블에 매수 당시 score, ml_prob 없음 | 컬럼 추가: `buy_score`, `buy_ml_prob`, `buy_reasons` |
| ML 모델 버전 관리 없음 | xgb_model.pkl 1개만 덮어쓰기 | 날짜별 모델 보관 + 롤백 기능 |
| 프론트 에러 로깅 없음 | console.error만 | Sentry 또는 에러 리포팅 연동 |
| 전략별 성과 추적 없음 | 전체 승률만 표시 | 전략 조합별(trend+macd 등) 승률/수익률 집계 |

### 🟢 P2: 성능 & UX
| 항목 | 현황 | 개선 |
|------|------|------|
| 프론트 5초 폴링 | setInterval 5000ms | WebSocket 실시간 업데이트로 전환 |
| ML 예측이 선정 종목에만 실행 | 나머지는 일일 스캔 결과만 | 실시간 분석 시 ML 근거도 함께 계산 (현재 analysis API에서만) |
| 대시보드 모바일 미최적화 | PC 기준 UI | 반응형 CSS 개선 |
| 설정 영속화 안됨 | 서버 재시작 시 기본값 복원 | .env 또는 DB에 저장 |

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

### EC2 접속 전 항상 확인
```bash
# 1. 현재 IP 확인
curl -s https://checkip.amazonaws.com
# 2. 보안 그룹에 현재 IP 등록 (이전 IP 제거 후)
aws ec2 revoke-security-group-ingress --group-id sg-0d3354ad01650e0ae --protocol tcp --port 22 --cidr {이전IP}/32
aws ec2 authorize-security-group-ingress --group-id sg-0d3354ad01650e0ae --ip-permissions IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges="[{CidrIp={현재IP}/32,Description=SSH}]"
# 3. SSH 접속
ssh -i "F:\Downloads\coinmate.pem" -o StrictHostKeyChecking=no ec2-user@15.134.82.85
```

### 배포 프로세스
```bash
# 백엔드: 파일 SCP 후 재시작
scp -i "F:\Downloads\coinmate.pem" -o StrictHostKeyChecking=no {로컬파일} ec2-user@15.134.82.85:/home/ec2-user/CoinMate-Backend/{경로}
ssh -i "F:\Downloads\coinmate.pem" ec2-user@15.134.82.85 "sudo systemctl restart coinmate"

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
