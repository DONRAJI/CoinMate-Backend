import asyncio
import time
import gc
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

from app.core.trade_repository import TradeRepository
from app.core.logger import get_logger
from app.services.order_executor import OrderExecutor
from app.services.strategy import Strategy
from app.services.backtester import Backtester
from app.services.ml_predictor import MLPredictor
from app.services import notifier
from app.core.database import init_db
import pyupbit

log = get_logger("trade")

class TradeManager:
    def __init__(self):
        # 1. 하위 직원들 고용
        init_db()
        self.repo = TradeRepository()
        self.executor = OrderExecutor(self.repo)
        self.strategy = Strategy()
        self.backtester = Backtester()
        self.ml = MLPredictor()
        # [분봉 모델 v3] ML_MIN_PROB = 익절 확률 하한.
        # 손익분기 36.4%(익절+3.5%/손절-2%) + 수수료/마진 고려 → 0.42 (after-fee +EV)
        # 이전 0.55는 옛 일봉 모델("1%상승확률") 기준이라 분봉 모델에선 전부 차단됐음
        self.ML_MIN_PROB = 0.42
        
        self.is_active = False
        self.shared_data = {}
        self._data_lock = None
        self.market_status = {}
        self.target_coins = []
        
        # 🔥 [누락된 부분 추가] 프론트엔드용 캐시 초기화
        self.frontend_cache = {} 
        
        # 캐시 및 쿨타임
        self.cached_day_dfs = {}
        self.cached_min_dfs = {}
        self.cached_5m_dfs = {}   # [분봉 모델 v3] ML 추론용 minute5 캐시
        self.last_api_call_time = {}
        self.sell_timestamps = {}

        self.REBUY_COOLDOWN = 1800

        # ═══ [개선 1] 심야 매수 차단 (22~07시 KST) ═══
        self.NIGHT_BUY_BLOCK_START = 22  # 22시부터
        self.NIGHT_BUY_BLOCK_END = 7     # 07시까지

        # ═══ [개선 2] 연속 손절 쿨오프 (점진적 강화) ═══
        self.recent_trade_results = []    # 최근 거래 결과 (True=승, False=패)
        self.CONSECUTIVE_LOSS_LIMIT = 3   # N연패 시 매수 중단
        self.loss_cooloff_until = 0       # 쿨오프 해제 시간 (timestamp)
        self.LOSS_COOLOFF_SECONDS = 3600  # 기본 1시간 (연패 지속 시 2h→4h로 강화)
        self.cooloff_level = 0            # 쿨오프 강도 단계 (승리 시 0으로 리셋)
        self.MAX_COOLOFF_LEVEL = 3        # 최대 4시간 (1h * 2^2)

        # ═══ [개선 6] 시장 전체 추세(BTC) 필터 ═══
        self._market_regime_cache = None  # "bull"/"neutral"/"bear"
        self._market_regime_ts = 0
        self.MARKET_REGIME_TTL = 1800     # 30분 캐시
        self._last_bear_log = 0
        self._last_neutral_log = 0
        self._market_regime_detail = {}   # {btc_price, ma24_dev_pct, mom6, updated} — 프론트 표시용

        # ═══ [개선 7] 레짐별 매수 임계값 — 약한 시장에서 엄격 진입 ═══
        # 데이터(3일/9건) 분석: neutral 전패, bull도 승률 20% → 약한 레짐엔 더 엄격해야 함
        # bull: 사용자 BUY_THRESHOLD/ML_MIN_PROB 그대로
        # neutral: +1.0점 / +5%p (혹은 6.5/0.60 중 큰값)
        self.NEUTRAL_SCORE_BONUS = 1.0
        self.NEUTRAL_ML_BONUS = 0.05
        self.NEUTRAL_SCORE_FLOOR = 6.5
        # [분봉 모델 v3] neutral 시 익절확률 하한 상향 (손익분기 + 더 큰 마진)
        self.NEUTRAL_ML_FLOOR = 0.47

        # ═══ [개선 9] 적응형 ML 임계값 (강세장 선별 강화용) ═══
        # 분봉 모델은 calibrated(익절확률 = 실제 승률)라 base(0.42)가 절대 기준.
        # adaptive는 max()로만 동작 → 강세장에 +EV 코인이 많으면 상위 15%만 통과(더 선별적),
        # 약세장엔 base 0.42 floor 유지(절대 아래로 안 내려감).
        self.ADAPTIVE_ML_PERCENTILE = 85  # 상위 15%
        self.ADAPTIVE_ML_MIN_SAMPLE = 30  # 표본 30개 미만이면 적응형 비활성

        # ═══ [개선 4] 최소 보유 시간 ═══
        self.MIN_HOLD_MINUTES = 30        # 매수 후 30분간 점수하락 매도 차단

        # ═══ [개선 5] 코인별 연속 손절 블랙리스트 ═══
        self.coin_loss_streak = {}        # {ticker: 연속 손실 횟수}
        self.coin_blacklist_until = {}    # {ticker: 블랙리스트 해제 시간}
        self.COIN_LOSS_STREAK_LIMIT = 2   # 같은 코인 2연패 시 블랙리스트
        self.COIN_BLACKLIST_SECONDS = 7200  # 2시간 블랙리스트

        # ═══ [개선 10] 호가창 진입 필터 — 56% 손절(진입 직후 하락) 대응 ═══
        # 매수 직전 매도벽이 매수벽보다 두꺼우면(매도 압력) 진입 보류
        # bid/ask 비율 = 상단 N호가 매수잔량(금액) / 매도잔량(금액). 낮을수록 매도 우세
        self.ORDERBOOK_FILTER_ON = True
        self.ORDERBOOK_DEPTH = 5          # 상위 5호가로 판단
        self.ORDERBOOK_MIN_BID_ASK = 0.7  # 매수/매도 비율 0.7 미만이면 차단(매도벽)

        # ═══ [섀도우 모드] 페이퍼 트레이딩 ═══
        # ON이면 실제 주문 대신 가상 매수/매도로 기록 (위험 없이 전략 검증)
        from app.core.paper_repository import paper_repository
        self.paper_repo = paper_repository
        self.SHADOW_MODE = False  # ControlPanel/config로 토글
        # 섀도우 완화안(손익분기 기준) — 라이브 영향 0, SHADOW_MODE일 때만 적용
        # 목적: "기준 완화 시 거래 빈도·수익성"을 위험 없이 검증
        self.SHADOW_ML_MIN_PROB = 0.364       # 손익분기 익절확률 (익절+3.5%/손절-2%)
        self.SHADOW_BUY_THRESHOLD = 5.0       # 완화 점수 기준
        self.SHADOW_ALLOW_ALL_REGIMES = True  # bear/neutral 포함 전 레짐 매수(게이트는 적용)

        # 설정값
        self.MAX_COIN_COUNT = 5
        self.MIN_ORDER_KRW = 6000
        self.CACHE_TTL_SECONDS = 300
        self.MIN_OHLCV_INTERVAL = 60
        self.PROFIT_TARGET = 3.5
        self.STOP_LOSS = -2.0
        self.TRAILING_ACTIVATION = 1.5
        self.TRAILING_DISTANCE = 1.2
        self.high_watermarks = {}
        
        self.STRATEGY_MAP = {
            "trend": "추세", "volume": "거래량폭발", "stoch": "골든크로스",
            "rsi": "RSI안정", "mfi": "자금유입", "bollinger": "밴드지지",
            "macd": "MACD", "adx": "강한추세", "vwap": "세력평단", "cci": "과매도탈출"
        }

        # ═══ [개선 2-b] 재시작 시 연패 상태 복원 ═══
        self._restore_loss_state()
        # ═══ [P2] 영속화된 설정 복원 (ControlPanel 변경값 유지) ═══
        self._load_persisted_config()

    # ═══════════════ 설정 영속화 ═══════════════
    @property
    def _config_path(self):
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        cache_dir = os.path.join(base, "cache")
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        return os.path.join(cache_dir, "user_config.json")

    def _load_persisted_config(self):
        """저장된 사용자 설정을 인스턴스에 반영 (재시작 후에도 ControlPanel 값 유지)"""
        import os, json
        path = self._config_path
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception as e:
            print(f">>> ⚠️ [Config] 로드 실패: {e}")
            return
        ATTR_MAP = {
            "stop_loss": "STOP_LOSS",
            "profit_target": "PROFIT_TARGET",
            "trailing_activation": "TRAILING_ACTIVATION",
            "trailing_distance": "TRAILING_DISTANCE",
            "max_coin_count": "MAX_COIN_COUNT",
            "min_order_krw": "MIN_ORDER_KRW",
            "rebuy_cooldown": "REBUY_COOLDOWN",
        }
        applied = []
        for k, attr in ATTR_MAP.items():
            if k in cfg:
                setattr(self, attr, cfg[k])
                applied.append(f"{k}={cfg[k]}")
        if "buy_threshold" in cfg:
            self.strategy.BUY_THRESHOLD = cfg["buy_threshold"]
            applied.append(f"buy_threshold={cfg['buy_threshold']}")
        if "shadow_mode" in cfg:
            self.SHADOW_MODE = bool(cfg["shadow_mode"])
            applied.append(f"shadow_mode={self.SHADOW_MODE}")
        if applied:
            print(f">>> 🔧 [Config] 영속화된 설정 복원: {', '.join(applied)}")

    def save_persisted_config(self):
        """현재 인스턴스 설정값을 영속화 (POST /trade/config에서 호출)"""
        import json
        cfg = {
            "stop_loss": self.STOP_LOSS,
            "profit_target": self.PROFIT_TARGET,
            "trailing_activation": self.TRAILING_ACTIVATION,
            "trailing_distance": self.TRAILING_DISTANCE,
            "max_coin_count": self.MAX_COIN_COUNT,
            "min_order_krw": self.MIN_ORDER_KRW,
            "rebuy_cooldown": self.REBUY_COOLDOWN,
            "buy_threshold": self.strategy.BUY_THRESHOLD,
            "shadow_mode": self.SHADOW_MODE,
        }
        try:
            with open(self._config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f">>> ⚠️ [Config] 저장 실패: {e}")

    def _restore_loss_state(self):
        """재시작 시 DB의 최근 거래 결과로 연패 카운터 복원 (인메모리 초기화 방지).
        🔥 쿨오프는 '마지막 손절 시각' 기준으로 복원 → 재배포 반복해도 타이머가 리셋되지 않음.
        """
        try:
            rows = self.repo.get_closed_trades(limit=10)  # sell_time DESC
            results = []
            for r in reversed(rows):
                pr = r['profit_rate'] if 'profit_rate' in r.keys() else None
                if pr is None:
                    continue
                results.append(pr > 0)  # True=승, False=패
            self.recent_trade_results = results[-10:]

            consecutive = 0
            for win in reversed(self.recent_trade_results):
                if not win:
                    consecutive += 1
                else:
                    break
            if consecutive > 0:
                print(f">>> 🔁 [복원] 최근 거래 {len(self.recent_trade_results)}건, 현재 {consecutive}연패 상태")

            if consecutive >= self.CONSECUTIVE_LOSS_LIMIT:
                self.cooloff_level = min(consecutive - self.CONSECUTIVE_LOSS_LIMIT + 1, self.MAX_COOLOFF_LEVEL)
                duration = self.LOSS_COOLOFF_SECONDS * (2 ** (self.cooloff_level - 1))

                # 🔥 마지막 손절 시각(sell_time)을 기준으로 쿨오프 종료시점 계산
                last_sell_ts = None
                if rows:
                    try:
                        st = rows[0]['sell_time'] if 'sell_time' in rows[0].keys() else None
                        if st:
                            s = str(st).replace('T', ' ').split('.')[0].split('+')[0].strip()
                            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                                try:
                                    dt = datetime.strptime(s, fmt).replace(tzinfo=KST)
                                    last_sell_ts = dt.timestamp()
                                    break
                                except ValueError:
                                    continue
                    except Exception:
                        last_sell_ts = None

                if last_sell_ts is None:
                    # sell_time 파싱 실패 시에만 보수적으로 now 기준
                    self.loss_cooloff_until = time.time() + duration
                    print(f">>> 🧊 [복원] {consecutive}연패 → 쿨오프 {duration//60}분 (시각 미상, now 기준)")
                else:
                    cooloff_end = last_sell_ts + duration
                    remaining = cooloff_end - time.time()
                    if remaining > 0:
                        self.loss_cooloff_until = cooloff_end
                        print(f">>> 🧊 [복원] {consecutive}연패 → 쿨오프 잔여 {remaining/60:.0f}분 (마지막 손절 기준)")
                    else:
                        # 이미 쿨오프 시간이 지남 → 적용 안 함 (재배포 반복해도 안 걸림)
                        print(f">>> ✅ [복원] {consecutive}연패지만 마지막 손절 후 {(-remaining)/3600:.1f}h 경과 → 쿨오프 해제됨")
        except Exception as e:
            print(f"⚠️ [Loss State Restore Error] {e}")

    def _get_market_regime(self):
        """BTC 기준 시장 전체 추세 판정 (bull/neutral/bear, 30분 캐시).
        하락장에서 알트코인 동반 하락으로 인한 연속 손절을 막기 위한 게이트."""
        now = time.time()
        if self._market_regime_cache and (now - self._market_regime_ts < self.MARKET_REGIME_TTL):
            return self._market_regime_cache
        regime = "neutral"  # 데이터 실패 시 매수 허용(fail-open)
        try:
            df = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=48)
            if df is not None and len(df) >= 25:
                closes = df['close']
                cur = closes.iloc[-1]
                ma24 = closes.rolling(24).mean().iloc[-1]
                mom6 = ((cur - closes.iloc[-7]) / closes.iloc[-7]) * 100 if closes.iloc[-7] > 0 else 0
                # 강화된 레짐 판정: bull은 의미 있는 추세만 인정 (작은 반등은 neutral)
                if cur < ma24 and mom6 < -1.0:
                    regime = "bear"
                elif cur > ma24 * 1.003 and mom6 > 0.5:  # MA24 0.3%+ 상회 AND 6h모멘텀 0.5%+
                    regime = "bull"
                else:
                    regime = "neutral"
                prev_regime = self._market_regime_cache
                self._market_regime_cache = regime
                self._market_regime_ts = now
                ma24_dev = (cur - ma24) / ma24 * 100 if ma24 > 0 else 0
                self._market_regime_detail = {
                    "btc_price": float(cur),
                    "ma24_dev_pct": round(ma24_dev, 2),
                    "mom6_pct": round(mom6, 2),
                    "updated_ts": now,
                }
                print(f">>> 🌐 [Market] BTC레짐={regime} (MA24이격 {ma24_dev:+.1f}%, 6h {mom6:+.1f}%)")
                # 레짐 전환 시 Discord 알림 (prev가 None이면 첫 판정이라 알림 X)
                if prev_regime and prev_regime != regime:
                    try:
                        from app.services import notifier
                        asyncio.create_task(notifier.notify_regime_change(prev_regime, regime, dict(self._market_regime_detail)))
                    except Exception as e:
                        print(f"⚠️ [Regime alert] {e}")
        except Exception as e:
            print(f"⚠️ [Market Regime Error] {e}")
            # 에러 시 직전 캐시가 있으면 그것을 사용
            if self._market_regime_cache:
                return self._market_regime_cache
        return regime

    def set_shared_data(self, shared_data, lock=None):
        self.shared_data = shared_data
        self._data_lock = lock
        print(f">>> 🔗 [TradeManager] 데이터 통 연결 완료! (ID: {id(self.shared_data)})")

    def _snapshot_shared_data(self):
        if self._data_lock:
            with self._data_lock:
                return dict(self.shared_data)
        return dict(self.shared_data)

    def start(self):
        self.is_active = True
        print(">>> 🚀 System STARTED")

    def stop(self):
        self.is_active = False
        print(">>> 🛑 System STOPPED")

    async def daily_summary_loop(self):
        """KST 23:50 한 번씩 일일 요약 알림 (간단 sleep 기반)."""
        from app.services import notifier
        sent_date = None
        while True:
            try:
                now = datetime.now(KST)
                if now.hour == 23 and now.minute >= 50 and sent_date != now.date():
                    stats = self._build_daily_summary()
                    await notifier.notify_daily_summary(stats)
                    sent_date = now.date()
            except Exception as e:
                print(f"⚠️ [Daily summary] {e}")
            await asyncio.sleep(60)

    def _build_daily_summary(self) -> dict:
        """오늘 거래 요약 통계 (자정 알림용)."""
        try:
            with self.repo.get_conn() as conn:
                conn.row_factory = __import__("sqlite3").Row
                today = datetime.now(KST).strftime("%Y-%m-%d")
                rows = conn.execute(
                    """SELECT profit_rate, buy_amount FROM trades
                       WHERE status='closed' AND DATE(sell_time)=? AND profit_rate IS NOT NULL""",
                    (today,),
                ).fetchall()
            today_trades = len(rows)
            wins = sum(1 for r in rows if r["profit_rate"] > 0)
            win_rate = (wins / today_trades * 100) if today_trades > 0 else 0
            today_pnl = sum(
                (r["buy_amount"] or 0) * (1 - 0.0005) * (1 + (r["profit_rate"] or 0) / 100) * (1 - 0.0005)
                - (r["buy_amount"] or 0)
                for r in rows
            )
        except Exception:
            today_trades, win_rate, today_pnl = 0, 0, 0

        # 자산
        krw = 0.0
        total_assets = 0.0
        open_count = self.repo.get_trade_count()
        try:
            balances = self.executor.get_all_balances()
            if isinstance(balances, list):
                for b in balances:
                    if not isinstance(b, dict):
                        continue
                    if b.get("currency") == "KRW":
                        krw = float(b.get("balance", 0))
                        total_assets += krw
                    else:
                        ticker = f"KRW-{b['currency']}"
                        qty = float(b.get("balance", 0)) + float(b.get("locked", 0))
                        cur = self.shared_data.get(ticker, {}).get("current_price", 0) if self.shared_data else 0
                        total_assets += qty * cur
        except Exception:
            pass

        regime_emoji = {"bull": "🐂 상승장", "neutral": "😐 중립", "bear": "🐻 하락장"}
        return {
            "today_trades": today_trades,
            "today_win_rate": win_rate,
            "today_pnl": int(today_pnl),
            "total_assets": int(total_assets),
            "krw_balance": int(krw),
            "open_count": open_count,
            "regime": regime_emoji.get(self._market_regime_cache or "neutral", "?"),
        }

    async def run_loop(self):
        print(">>> 🔄 Main Loop Initialized...")
        print(">>> ⏳ [System] 실시간 시세 데이터 수신 대기 중...")
        
        # --- [데이터 수신 대기 구간] ---
        MAX_WAIT = 600
        wait_seconds = 0
        while wait_seconds < MAX_WAIT:
            data_len = len(self.shared_data) if self.shared_data else 0

            if data_len > 10:
                print(f"\n>>> 📶 [System] 실시간 데이터 수신 확인됨! (현재 {data_len}개)")
                break

            if wait_seconds % 10 == 0:
                print(f">>> ⏳ 데이터 대기 중... (현재: {data_len}개 / 목표: 10개) - {wait_seconds}초 경과")

            wait_seconds += 1
            await asyncio.sleep(1)
        else:
            print(">>> ❌ [System] 데이터 수신 타임아웃 (10분). 가용 데이터로 시작합니다.")
        
        # --- [본격적인 매매 루프] ---
        print(">>> 🚀 [System] 매매 로직 가동 시작!")
        
        await self.backtester.run_daily_scan()
        await self.update_target_coins()
        
        loop_count = 0
        while True:
            try:
                # ... (기존 매매 로직 유지) ...
                if loop_count % 300 == 0:
                    await self.update_target_coins()
                    await self.refresh_target_scores()
                    self.cleanup_old_cache()
                
                now = datetime.now(KST)
                if now.hour == 0 and now.minute == 1 and loop_count % 60 == 0:
                    asyncio.create_task(self.backtester.run_daily_scan())
                    self.sell_timestamps.clear()

                # 야간 매매 제한 (22시~07시 KST)
                is_night = now.hour >= self.NIGHT_BUY_BLOCK_START or now.hour < self.NIGHT_BUY_BLOCK_END

                if is_night:
                    # 야간: 손절만 허용, 매수 완전 차단 (process_buying 내부에서도 이중 차단)
                    await self.process_selling(night_mode=True)
                else:
                    await self.process_selling()
                    if self.is_active:
                        await self.process_buying()  # 내부에서 심야+쿨오프+블랙리스트 체크
                
                self.update_frontend_cache()
                
                loop_count += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"[Loop Error] {e}", exc_info=True)
                asyncio.create_task(notifier.notify_error("MainLoop", str(e)))
                await asyncio.sleep(5)

    async def process_selling(self, night_mode=False):
        """
        night_mode=True: 손절만 실행 (야간 보호)
        섀도우 모드면 가상 포지션을 매도 판단.
        """
        open_trades = self.paper_repo.get_open_trades() if self.SHADOW_MODE else self.repo.get_open_trades()

        for trade in open_trades:
            trade_id = trade['id']
            ticker = trade['ticker']
            buy_price = trade['buy_price']
            
            # 캔들 데이터 조회
            df_day, df_min, current, is_real = await self.get_smart_candles(ticker)
            if not is_real or current == 0: continue

            if buy_price <= 0: buy_price = current
            profit_rate = ((current - buy_price) / buy_price) * 100

            # 고점 갱신 (트레일링 스탑용)
            prev_high = self.high_watermarks.get(ticker, current)
            self.high_watermarks[ticker] = max(prev_high, current)
            high = self.high_watermarks[ticker]
            drawdown_from_high = ((high - current) / high) * 100 if high > 0 else 0

            res = self.strategy.get_ensemble_signal(df_day, df_min)
            if not res: continue
            self._update_market_status(ticker, current, res)

            is_strong_trend = res['strategies'].get('adx', 0) == 1
            regime = res.get('regime', 'normal')
            is_sideways = regime == 'sideways'

            # ATR 기반 동적 손절/익절
            atr = res.get('atr', 0)
            atr_pct = (atr / buy_price * 100) if buy_price > 0 and atr > 0 else 0

            # 🔥 [개선 8] 손절선 ATR 비례 확대 — 진입 직후 -2% 도달 패턴 완화
            # 데이터 분석: 손절 대부분이 -2%대에 몰림 → floor 묶임 + ATR 배수 부족
            # → 배수 ↑(1.2→1.5, 1.0→1.2) + floor 완화(-2.0→-2.5, -1.5→-1.8)로 변동성 흡수
            if is_sideways:
                # 횡보장: 기본 타이트 + ATR 보정 (배수 1.0→1.2, floor -1.5→-1.8)
                stop_loss = max(-1.8, -(atr_pct * 1.2)) if atr_pct > 0 else -1.8
                profit_target = max(2.0, atr_pct * 1.5) if atr_pct > 0 else 2.0
            else:
                # 추세장/일반: ATR 기반 동적 (배수 1.2→1.5, profit 2.0→2.5)
                # floor는 self.STOP_LOSS(.env/config로 조정 가능, 기본 -2.0)
                # 더 완화하려면 ControlPanel에서 STOP_LOSS=-2.5로 조정 가능
                if atr_pct > 0:
                    stop_loss = max(self.STOP_LOSS, -(atr_pct * 1.5))   # ATR 1.5배
                    profit_target = max(self.PROFIT_TARGET, atr_pct * 2.5)  # ATR 2.5배
                else:
                    stop_loss = self.STOP_LOSS
                    profit_target = self.PROFIT_TARGET

            # 보유 시간 계산 (시간 기반 탈출용)
            bought_at = trade['buy_time'] if 'buy_time' in trade.keys() else None
            holding_hours = 0
            if bought_at:
                try:
                    from datetime import datetime as dt
                    if isinstance(bought_at, str):
                        bought_time = dt.fromisoformat(bought_at)
                    else:
                        bought_time = bought_at
                    holding_hours = (dt.now(KST) - bought_time.replace(tzinfo=KST)).total_seconds() / 3600
                except Exception:
                    pass

            reason = ""
            reason_detail = ""
            # 1. 손절 (최우선 — 야간에도 실행)
            if profit_rate <= stop_loss:
                reason = "stop_loss"
                reason_detail = f"{profit_rate:.2f}%,{regime}"

            # 야간 모드: 손절 외 매도 차단
            elif night_mode:
                pass

            # 2. 트레일링 스탑 (강한 추세일 때만)
            elif is_strong_trend and profit_rate >= self.TRAILING_ACTIVATION and drawdown_from_high >= self.TRAILING_DISTANCE:
                reason = "trailing"
                reason_detail = f"{profit_rate:.2f}%,고점대비-{drawdown_from_high:.1f}%"

            # 3. 익절 (레짐별 목표가 다름)
            elif not is_strong_trend and profit_rate >= profit_target:
                reason = "take_profit"
                reason_detail = f"{profit_rate:.2f}%,{regime}"

            # 4. 횡보장 시간 기반 탈출: 48시간 보유 + 수익 < 1%
            elif is_sideways and holding_hours >= 48 and profit_rate < 1.0:
                reason = "sideways_exit"
                reason_detail = f"{holding_hours:.0f}h,{profit_rate:.2f}%"

            # 5. 수익권 과열 체크 (최소 2% 이상 수익일 때만)
            elif profit_rate > 2.0:
                if res['rsi'] >= 80:
                    reason = "rsi_overheat"
                    reason_detail = f"{profit_rate:.2f}%"
                elif res.get('mfi', 0) >= 85:
                    reason = "mfi_overheat"
                    reason_detail = f"{profit_rate:.2f}%"

            # 6. 전략 점수 급락 (손실 중일 때만 — 수익 중이면 홀딩)
            elif res['score'] < 3.0 and profit_rate < -0.5:
                reason = "score_drop"
                reason_detail = f"{res['score']}점,{profit_rate:.2f}%"

            # 7. 이상 징후 (강한 괴리만)
            elif res['rsi'] < 40 and res.get('mfi', 0) >= 85:
                reason = "anomaly"
                reason_detail = "설거지감지"

            # ═══ [개선 4] 최소 보유 시간: 30분 미만이면 점수하락/이상징후 매도 차단 ═══
            if reason in ("score_drop", "anomaly") and holding_hours < (self.MIN_HOLD_MINUTES / 60):
                reason = ""  # 매도 취소 — 아직 보유 시간 부족
                log.info(f"[홀딩] {ticker}: {reason}이나 보유 {holding_hours*60:.0f}분 < {self.MIN_HOLD_MINUTES}분 → 매도 보류")

            # --- [매도 실행] ---
            if reason and self.is_active:
                sell_reason_full = f"{reason}({reason_detail})" if reason_detail else reason
                log.info(f"[매도 판단] {ticker} -> {sell_reason_full}")
                # 🔥 [섀도우 모드] 가상 매도 분기
                if self.SHADOW_MODE:
                    success = self.paper_repo.paper_sell(trade_id, current, reason)
                else:
                    success = await self.executor.try_sell(trade_id, ticker, current, reason)
                if success:
                    self.sell_timestamps[ticker] = time.time()
                    self.high_watermarks.pop(ticker, None)
                    if not self.SHADOW_MODE:
                        asyncio.create_task(notifier.notify_sell(ticker, current, profit_rate, reason))

                    if ticker in self.market_status:
                        self.market_status[ticker]["category"] = "관찰 종목"

                    # ═══ [개선 2] 연속 손절 추적 (점진적 쿨오프) ═══
                    is_loss = profit_rate <= 0
                    self.recent_trade_results.append(not is_loss)
                    if len(self.recent_trade_results) > 10:
                        self.recent_trade_results = self.recent_trade_results[-10:]

                    if not is_loss:
                        self.cooloff_level = 0  # 승리 시 쿨오프 강도 초기화

                    # 최근 N건 연속 패배 체크
                    consecutive_losses = 0
                    for r in reversed(self.recent_trade_results):
                        if not r:
                            consecutive_losses += 1
                        else:
                            break

                    if consecutive_losses >= self.CONSECUTIVE_LOSS_LIMIT:
                        # 연패가 지속될수록 쿨오프 강화: 1h → 2h → 4h
                        self.cooloff_level = min(self.cooloff_level + 1, self.MAX_COOLOFF_LEVEL)
                        duration = self.LOSS_COOLOFF_SECONDS * (2 ** (self.cooloff_level - 1))
                        self.loss_cooloff_until = time.time() + duration
                        log.warning(f"[쿨오프] {consecutive_losses}연패 감지! {duration//60}분간 매수 중단 (강도 {self.cooloff_level})")
                        asyncio.create_task(notifier.notify_error(
                            "연속손절 쿨오프",
                            f"{consecutive_losses}연패 → {duration//60}분 매수 중단"
                        ))

                    # ═══ [개선 5] 코인별 연속 손절 블랙리스트 ═══
                    if is_loss:
                        self.coin_loss_streak[ticker] = self.coin_loss_streak.get(ticker, 0) + 1
                        if self.coin_loss_streak[ticker] >= self.COIN_LOSS_STREAK_LIMIT:
                            self.coin_blacklist_until[ticker] = time.time() + self.COIN_BLACKLIST_SECONDS
                            log.warning(f"[블랙리스트] {ticker}: {self.coin_loss_streak[ticker]}연패 → {self.COIN_BLACKLIST_SECONDS//3600}시간 차단")
                    else:
                        self.coin_loss_streak[ticker] = 0  # 승리 시 연패 초기화

    # ═══════════════ 적응형 ML 임계값 (분포 기반) ═══════════════
    # ═══════════════ 호가창 진입 필터 ═══════════════
    async def _check_orderbook(self, ticker: str) -> tuple[bool, float]:
        """매수 직전 호가창 점검.
        Returns: (통과여부, bid_ask_ratio)
        bid_ask_ratio = 상위 N호가 매수금액 / 매도금액. 1보다 크면 매수우세.
        """
        try:
            ob = await asyncio.to_thread(pyupbit.get_orderbook, ticker)
            if isinstance(ob, list):
                ob = ob[0] if ob else None
            if not ob or 'orderbook_units' not in ob:
                return True, 1.0  # 조회 실패 시 통과(fail-open)

            units = ob['orderbook_units'][:self.ORDERBOOK_DEPTH]
            bid_value = sum(u['bid_price'] * u['bid_size'] for u in units)
            ask_value = sum(u['ask_price'] * u['ask_size'] for u in units)
            if ask_value <= 0:
                return True, 1.0
            ratio = bid_value / ask_value
            return ratio >= self.ORDERBOOK_MIN_BID_ASK, round(ratio, 2)
        except Exception as e:
            print(f"⚠️ [Orderbook] {ticker}: {e}")
            return True, 1.0  # 에러 시 통과

    def _get_adaptive_ml_min(self, base_min: float) -> float:
        """그 날 전 코인의 ml_prob 분포에서 상위 (100 - PERCENTILE)% 기준값과
        regime-based base_min 중 더 큰 값을 반환.
        분포가 시간에 따라 우로 시프트해도 자동으로 상위만 통과시킴.
        """
        try:
            cache = self.backtester.results_cache or {}
            probs = [c.get("ml_prob") for c in cache.values() if c.get("ml_prob") is not None]
            if len(probs) < self.ADAPTIVE_ML_MIN_SAMPLE:
                return base_min
            sorted_p = sorted(probs)
            idx = min(int(len(sorted_p) * self.ADAPTIVE_ML_PERCENTILE / 100), len(sorted_p) - 1)
            return max(base_min, sorted_p[idx])
        except Exception:
            return base_min

    # ═══════════════ Kelly Criterion 자금관리 ═══════════════
    def _compute_kelly_fraction(self, pick: dict, stats: dict | None) -> float:
        """[Kelly] per-pick Kelly fraction (1/4 Kelly 안전계수).
        - p (승률): ML prob 우선, 없으면 history 승률
        - b (win/loss 비율): history avg_win / avg_loss
        - f = p - (1-p)/b → 1/4 Kelly 적용 → 5%~40% 범위로 clip
        - 표본 부족(<10)이면 보수적 fallback 20%
        """
        KELLY_SAFETY = 0.25   # 1/4 Kelly (분산↓)
        KELLY_MIN = 0.05      # 최소 5% (음수 edge일 때 탐색용 최소)
        KELLY_MAX = 0.40      # 최대 40% (단일 종목 과집중 방지)
        FALLBACK = 0.20

        if not stats or not stats.get("avg_loss"):
            return FALLBACK
        avg_loss = abs(stats["avg_loss"])
        avg_win = stats["avg_win"]
        if avg_loss == 0 or avg_win <= 0:
            return FALLBACK
        b = avg_win / avg_loss

        # 승률은 ML prob 우선 (per-pick 추정 더 정확)
        ml_prob = pick.get("ml_prob")
        if ml_prob is not None:
            p = float(ml_prob)
        else:
            p = stats["win_rate"]

        f = p - (1 - p) / b

        if f <= 0:
            # 이론상 거래 안 해야 하지만 매수 신호가 통과한 후보 → 최소 사이즈로 탐색
            return KELLY_MIN

        f *= KELLY_SAFETY
        return max(KELLY_MIN, min(f, KELLY_MAX))

    async def process_buying(self):
        # ═══ [개선 1] 심야 매수 차단 ═══
        now_hour = datetime.now(KST).hour
        if now_hour >= self.NIGHT_BUY_BLOCK_START or now_hour < self.NIGHT_BUY_BLOCK_END:
            return  # 22시~07시 매수 완전 차단

        # ═══ [개선 2] 연속 손절 쿨오프 체크 ═══
        if time.time() < self.loss_cooloff_until:
            remaining = int((self.loss_cooloff_until - time.time()) / 60)
            if remaining % 10 == 0 and remaining > 0:  # 10분마다 로그
                print(f"  🧊 [쿨오프] 연속손절 쿨오프 중... 매수 재개까지 {remaining}분")
            return

        # ═══ [개선 6] 시장 전체 추세(BTC) 필터 — 하락장이면 전체 매수 차단 ═══
        # 섀도우 완화 검증 모드는 bear도 매수 허용(게이트는 적용) → 약세장 거래 데이터 수집
        regime = self._get_market_regime()
        shadow_relax = self.SHADOW_MODE
        if regime == "bear" and not (shadow_relax and self.SHADOW_ALLOW_ALL_REGIMES):
            if time.time() - self._last_bear_log > 600:  # 10분마다 로그
                print(f"  🐻 [Market] BTC 하락장 감지 → 신규 매수 전면 차단")
                self._last_bear_log = time.time()
            return

        if shadow_relax:
            # ═══ [섀도우 완화안] 손익분기 기준 — adaptive/neutral 가산 미적용 (순수 완화 게이트 검증) ═══
            local_buy_threshold = self.SHADOW_BUY_THRESHOLD
            local_ml_min = self.SHADOW_ML_MIN_PROB
            if time.time() - self._last_neutral_log > 600:
                print(f"  🧪 [Shadow] 완화 진입 (score≥{local_buy_threshold}, ML≥{local_ml_min:.0%}, regime={regime})")
                self._last_neutral_log = time.time()
        else:
            # ═══ [개선 7] 레짐별 매수 임계값 계산 (neutral은 더 엄격) ═══
            base_score = self.strategy.BUY_THRESHOLD
            base_ml = self.ML_MIN_PROB
            if regime == "neutral":
                local_buy_threshold = max(base_score + self.NEUTRAL_SCORE_BONUS, self.NEUTRAL_SCORE_FLOOR)
                local_ml_min = max(base_ml + self.NEUTRAL_ML_BONUS, self.NEUTRAL_ML_FLOOR)
                if time.time() - self._last_neutral_log > 600:
                    print(f"  ⚖️ [Market] BTC neutral → 엄격 진입 (score≥{local_buy_threshold}, ML≥{local_ml_min:.0%})")
                    self._last_neutral_log = time.time()
            else:  # bull
                local_buy_threshold = base_score
                local_ml_min = base_ml

            # ═══ [개선 9] 적응형 ML 임계값 (분포 시프트 자동 대응) ═══
            adaptive_ml_min = self._get_adaptive_ml_min(local_ml_min)
            if adaptive_ml_min > local_ml_min:
                print(f"  📊 [Adaptive ML] 분포 상위 {100 - self.ADAPTIVE_ML_PERCENTILE}% 기준 → ML≥{adaptive_ml_min:.0%} (regime base {local_ml_min:.0%})")
                local_ml_min = adaptive_ml_min

        # --- [1] 먼저 예산/슬롯 확인 (섀도우면 가상 잔고/슬롯 기준) ---
        if self.SHADOW_MODE:
            active_cnt = self.paper_repo.get_open_count()
            krw = self.paper_repo.get_krw_balance()
        else:
            active_cnt = self.repo.get_trade_count()
            krw = self.executor.get_krw_balance()
        empty_slots = self.MAX_COIN_COUNT - active_cnt
        can_buy = (empty_slots > 0) and (krw >= self.MIN_ORDER_KRW)

        available_krw = krw * 0.99 if can_buy else 0
        MAX_SINGLE_RATIO = 0.4

        # --- [2] 종목 스캔 & 점수 업데이트 ---
        candidates = []

        for ticker in self.target_coins:
            last_sell = self.sell_timestamps.get(ticker, 0)
            is_cooldown = (time.time() - last_sell < self.REBUY_COOLDOWN)
            is_holding = self._is_holding(ticker)

            df_day, df_min, current, is_real = await self.get_smart_candles(ticker)
            if not is_real: continue
            
            res = self.strategy.get_ensemble_signal(df_day, df_min)
            if not res: continue

            # ML 예측 + 근거 (UI 표시용 — 모든 종목)
            # [분봉 모델 v3] minute5 데이터로 예측 (일봉 df_day 아님)
            df_5m = self.cached_5m_dfs.get(ticker)
            if self.ml.is_trained and df_5m is not None and len(df_5m) >= 60:
                ml_result = self.ml.predict_with_reasons(df_5m)
                res['ml_prob'] = ml_result['prob']
                # 카드에 표시할 상위 3개 근거 (라벨만)
                res['ml_top_reasons'] = [
                    {"label": r["label"], "direction": r["direction"], "value": r["value"]}
                    for r in ml_result['reasons'][:3]
                ]
            else:
                res['ml_prob'] = None
                res['ml_top_reasons'] = []

            # UI용 상태 업데이트
            self._update_market_status(ticker, current, res)

            if not can_buy:
                continue
            if is_holding:
                continue
            if is_cooldown:
                print(f"  ⏳ [Skip] {ticker}: 쿨타임")
                self._set_skip_reason(ticker, "⏳ 쿨타임")
                continue

            # ═══ [개선 5] 코인별 블랙리스트 체크 ═══
            blacklist_until = self.coin_blacklist_until.get(ticker, 0)
            if time.time() < blacklist_until:
                remaining_min = int((blacklist_until - time.time()) / 60)
                self._set_skip_reason(ticker, f"🚫 연속손절 차단 ({remaining_min}분)")
                continue
            elif blacklist_until > 0:
                # 블랙리스트 해제 → 정리
                self.coin_blacklist_until.pop(ticker, None)
                self.coin_loss_streak.pop(ticker, None)

            # --- 매수 후보 필터링 로직 ---
            res['ticker'] = ticker
            res['current_price'] = current

            rsi = res['rsi']
            mfi = res.get('mfi', 50)
            score = res['score']

            if score < local_buy_threshold:
                # 점수 미달은 skip_reason 불필요 (프론트에서 점수로 판단)
                continue

            # 점수 통과 → 이후 필터 로그 출력
            if rsi >= 75:
                print(f"  🚫 [Skip] {ticker}: RSI과열({rsi:.1f})")
                self._set_skip_reason(ticker, f"🔥 RSI 과열 ({rsi:.0f})")
                continue
            if mfi >= 85:
                print(f"  🚫 [Skip] {ticker}: MFI과열({mfi:.1f})")
                self._set_skip_reason(ticker, f"🔥 MFI 과열 ({mfi:.0f})")
                continue
            if rsi >= 65 and mfi < 35:
                print(f"  🚫 [Skip] {ticker}: RSI/MFI괴리(RSI:{rsi:.1f},MFI:{mfi:.1f})")
                self._set_skip_reason(ticker, f"⚠️ RSI/MFI 괴리")
                continue

            # === 급등 직후 진입 차단 (고점 물림 방지) ===
            # 직전 3봉(3시간) 동안의 가격 상승률 확인
            if len(df_min) >= 4:
                price_3h_ago = df_min['close'].iloc[-4]
                recent_surge = ((current - price_3h_ago) / price_3h_ago) * 100 if price_3h_ago > 0 else 0
                if recent_surge >= 5.0:
                    print(f"  🚫 [Skip] {ticker}: 직전급등({recent_surge:.1f}% in 3h)")
                    self._set_skip_reason(ticker, f"🚀 직전 급등 ({recent_surge:.1f}%)")
                    continue

            # 직전 1봉(1시간) 급등 — 더 타이트하게
            if len(df_min) >= 2:
                price_1h_ago = df_min['close'].iloc[-2]
                recent_1h = ((current - price_1h_ago) / price_1h_ago) * 100 if price_1h_ago > 0 else 0
                if recent_1h >= 3.0:
                    print(f"  🚫 [Skip] {ticker}: 1시간급등({recent_1h:.1f}%)")
                    self._set_skip_reason(ticker, f"🚀 1시간 급등 ({recent_1h:.1f}%)")
                    continue

            # === 고점 근접 진입 차단 ===
            # 현재가가 직전 6봉(6시간) 최고가 대비 98% 이상이면 고점 진입
            if len(df_min) >= 6:
                recent_high = df_min['high'].iloc[-6:].max()
                if recent_high > 0:
                    high_ratio = current / recent_high
                    if high_ratio >= 0.98:
                        print(f"  🚫 [Skip] {ticker}: 고점근접(6h최고대비 {high_ratio:.1%})")
                        self._set_skip_reason(ticker, f"📈 고점 근접 ({high_ratio:.1%})")
                        continue

            # [분봉 모델 v3] ML 익절확률 필터 (손익분기 기반 임계값, 레짐별 적용)
            ml_prob = res.get('ml_prob')
            if self.ml.is_trained and ml_prob is not None:
                print(f"  🤖 [ML] {ticker}: 익절확률 {ml_prob:.1%} (기준 {local_ml_min:.0%}, 레짐 {regime})")
                if ml_prob < local_ml_min:
                    print(f"  🚫 [Skip] {ticker}: 익절확률 낮음 ({ml_prob:.1%} < {local_ml_min:.0%}) [레짐: {regime}]")
                    self._set_skip_reason(ticker, f"🤖 익절확률 낮음 ({ml_prob:.0%}) [{regime}]")
                    continue
            elif self.ml.is_trained and ml_prob is None:
                # minute5 데이터 없어 ML 판단 불가 → 보수적으로 차단
                self._set_skip_reason(ticker, "🤖 ML 데이터 대기중")
                continue

            last_open = df_min['open'].iloc[-1]
            last_close = df_min['close'].iloc[-1]
            last_high = df_min['high'].iloc[-1]
            last_low = df_min['low'].iloc[-1]
            body_size = abs(last_close - last_open)
            upper_wick = last_high - max(last_open, last_close)
            if body_size > 0 and upper_wick > (body_size * 2):
                print(f"  🚫 [Skip] {ticker}: 슈팅스타(윗꼬리)")
                self._set_skip_reason(ticker, "📌 슈팅스타 (윗꼬리)")
                continue

            volume_ma20 = df_min['volume'].rolling(20).mean().iloc[-1]
            price_change_pct = ((last_close - last_open) / last_open) * 100 if last_open > 0 else 0
            if price_change_pct > 3 and df_min['volume'].iloc[-1] < volume_ma20:
                print(f"  🚫 [Skip] {ticker}: 저거래량펌프({price_change_pct:.1f}%)")
                self._set_skip_reason(ticker, f"📌 저거래량 펌프 ({price_change_pct:.1f}%)")
                continue

            price_range_pct = ((last_high - last_low) / last_open) * 100 if last_open > 0 else 0
            if price_range_pct > 10:
                print(f"  🚫 [Skip] {ticker}: 극단변동성({price_range_pct:.1f}%)")
                self._set_skip_reason(ticker, f"📌 극단 변동성 ({price_range_pct:.1f}%)")
                continue

            # 필터 통과 → skip_reason 제거
            self._set_skip_reason(ticker, None)
            print(f"  ✅ [Pass] {ticker}: 점수{score} RSI:{rsi:.0f} MFI:{mfi:.0f} → 매수후보")
            candidates.append(res)
        
        # --- [3] 점수 가중 예산 배분 + 매수 실행 ---
        if candidates and can_buy:
            candidates.sort(key=lambda x: (x.get('ml_prob', 0.5), x['score']), reverse=True)
            final_picks = candidates[:empty_slots]

            total_score = sum(p['score'] for p in final_picks)
            # 🔥 [Kelly] 자금관리 — 고정 MAX_SINGLE_RATIO 대신 per-pick Kelly fraction
            kelly_stats = self.repo.get_kelly_stats(limit=50)
            if kelly_stats:
                print(f"  💼 [Kelly] 통계 N={kelly_stats['count']}, 승률={kelly_stats['win_rate']:.0%}, "
                      f"평균승={kelly_stats['avg_win']}%, 평균손={kelly_stats['avg_loss']}%")

            for pick in final_picks:
                ticker = pick.get('ticker')
                price = pick.get('current_price')
                if not ticker: continue

                # per-pick Kelly fraction (ML prob 우선, history는 b 계산용)
                k_frac = self._compute_kelly_fraction(pick, kelly_stats)
                pick_max = available_krw * k_frac

                if len(final_picks) == 1:
                    budget = pick_max
                else:
                    weight = pick['score'] / total_score if total_score > 0 else 1.0 / len(final_picks)
                    # score 가중치와 Kelly 상한 중 작은 쪽
                    budget = min(available_krw * weight, pick_max)

                if budget < self.MIN_ORDER_KRW:
                    print(f"  💸 [Skip] {ticker}: 예산부족({budget:.0f}원 < {self.MIN_ORDER_KRW}원)")
                    continue

                # 🔥 [개선 10] 호가창 진입 필터 — 매수 직전 매도벽 점검
                # (진입 직후 하락=손절 56% 대응. 최종 후보에만 호출해 API 부하 최소화)
                ob_ratio = None
                if self.ORDERBOOK_FILTER_ON:
                    ob_pass, ob_ratio = await self._check_orderbook(ticker)
                    if not ob_pass:
                        print(f"  🚫 [Skip] {ticker}: 매도벽 우세 (매수/매도 {ob_ratio} < {self.ORDERBOOK_MIN_BID_ASK})")
                        self._set_skip_reason(ticker, f"📊 매도벽 우세 ({ob_ratio})")
                        continue

                strategies = [k for k, v in pick['strategies'].items() if v == 1]
                strategy_name = "+".join(strategies) if strategies else "AI_Ensemble"

                ml_p = pick.get('ml_prob') or 0.5
                log.info(f"[Pick] {ticker} (점수:{pick['score']} / RSI:{pick['rsi']:.1f} / ML:{ml_p:.0%} / Kelly:{k_frac:.1%} / 호가:{ob_ratio} / 예산:{budget:.0f}원) -> 매수")

                # 🔥 [P1] 매수 시점 컨텍스트 (사후 분석용)
                buy_context = {
                    "score": round(float(pick.get('score', 0)), 2),
                    "ml_prob": round(float(pick['ml_prob']), 4) if pick.get('ml_prob') is not None else None,
                    "regime": self._market_regime_cache or "neutral",
                    "rsi": round(float(pick.get('rsi', 0)), 1),
                    "orderbook_ratio": ob_ratio,
                }
                # [Phase 1B] 매수 시점 뉴스 컨텍스트 (참고/분석용, 매매에 직접 영향 없음)
                try:
                    from app.services.news_collector import news_collector as _nc
                    news_sum = _nc.get_ticker_summary(ticker, hours=24)
                    buy_context["news_sentiment"] = news_sum.get("avg_sentiment")
                    buy_context["news_critical_count"] = news_sum.get("critical_count", 0)
                except Exception as _e:
                    pass
                # 🔥 [섀도우 모드] 가상 매수 분기
                if self.SHADOW_MODE:
                    success = self.paper_repo.paper_buy(ticker, price, budget, strategy_name, buy_context)
                else:
                    success = await self.executor.try_buy(ticker, price, budget, strategy_name, buy_context)
                if success:
                    available_krw -= budget
                    if not self.SHADOW_MODE:
                        asyncio.create_task(notifier.notify_buy(ticker, price, budget, strategy_name))
                        if ticker in self.market_status:
                            self.market_status[ticker]['category'] = self.market_status[ticker].get("category", "") + " (보유중)"
                    await asyncio.sleep(0.2)

    async def place_manual_buy(self, ticker, krw_amount):
        """수동 매수 (시장가). krw_amount=0이면 전량 매수"""
        try:
            current_krw = self.executor.get_krw_balance()
            if krw_amount <= 0:
                krw_amount = current_krw * 0.9995  # 전량 매수 (수수료 여유분)
            if current_krw < krw_amount:
                return {"status": "error", "message": f"잔액 부족 (보유: {current_krw:,.0f}원)"}
            
            current_price = pyupbit.get_current_price(ticker)
            if not current_price:
                return {"status": "error", "message": f"{ticker} 시세 조회 실패"}
            # 🔥 [P1] 수동 매수도 레짐 기록 (score/ml_prob은 없음)
            manual_ctx = {"score": None, "ml_prob": None,
                          "regime": self._market_regime_cache or "neutral", "rsi": None}
            # [Phase 1B] 뉴스 컨텍스트도 기록
            try:
                from app.services.news_collector import news_collector as _nc
                news_sum = _nc.get_ticker_summary(ticker, hours=24)
                manual_ctx["news_sentiment"] = news_sum.get("avg_sentiment")
                manual_ctx["news_critical_count"] = news_sum.get("critical_count", 0)
            except Exception:
                pass
            success = await self.executor.try_buy(ticker, current_price, krw_amount, "Manual(수동)", manual_ctx)
            
            if success:
                if ticker in self.market_status:
                    self.market_status[ticker]['category'] = self.market_status[ticker].get("category", "") + " (보유중)"
                self.update_frontend_cache()
                return {"status": "success", "message": f"{ticker} 매수 성공!"}
            else:
                return {"status": "error", "message": "API 매수 주문 실패"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def place_manual_sell(self, ticker):
        """수동 매도 (전량 시장가)"""
        try:
            balance = self.executor.get_coin_balance(ticker)
            if balance <= 0:
                return {"status": "error", "message": "매도할 잔액이 없습니다."}
            
            current_price = pyupbit.get_current_price(ticker)
            if not current_price:
                return {"status": "error", "message": f"{ticker} 시세 조회 실패"}
            trade_row = self.repo.get_open_trade(ticker)
            if not trade_row:
                return {"status": "error", "message": f"{ticker} 보유 기록을 찾을 수 없습니다."}
            trade_id = trade_row['id']

            success = await self.executor.try_sell(trade_id, ticker, current_price, "Manual(수동)")
            
            if success:
                self.sell_timestamps[ticker] = time.time()
                if ticker in self.market_status:
                    cat = self.market_status[ticker].get("category", "")
                    self.market_status[ticker]["category"] = cat.replace(" (보유중)", "")
                self.update_frontend_cache()
                return {"status": "success", "message": f"{ticker} 매도 성공!"}
            else:
                return {"status": "error", "message": "API 매도 주문 실패"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def update_target_coins(self):
        try:
            if not self.shared_data: return
            
            # --- [1] 종목 선정 로직 ---
            MIN_TRADE_PRICE = 5_000_000_000 
            all_data = list(self._snapshot_shared_data().items())
            sorted_by_vol = sorted(all_data, key=lambda x: x[1]['acc_trade_price_24h'], reverse=True)
            valid_tickers = [item[0] for item in sorted_by_vol if item[1]['acc_trade_price_24h'] >= MIN_TRADE_PRICE]
            top_50_tickers = set(valid_tickers[:50])
            
            targets_map = {}
            top_5_vol = valid_tickers[:5]
            for t in top_5_vol: targets_map[t] = "거래량 최상위"
            
            ai_candidates = self.backtester.get_best_opportunities(top_n=20)
            added_ai = 0
            # 거래량 50위 제약 제거: AI 점수가 높으면 거래량 무관하게 추가
            # 단, 최소 거래대금 필터(50억)는 유지하여 유동성 확보
            all_ticker_vol = {item[0]: item[1]['acc_trade_price_24h'] for item in all_data}
            MIN_AI_TRADE_PRICE = 1_000_000_000  # AI 추천은 10억 이상이면 허용
            for t in ai_candidates:
                if t in targets_map: continue
                vol = all_ticker_vol.get(t, 0)
                if vol >= MIN_AI_TRADE_PRICE:
                    targets_map[t] = "AI 추천(우량주)"
                    added_ai += 1
                if added_ai >= 5: break

            ai_list = [t for t, c in targets_map.items() if 'AI' in c]
            print(f">>> 📊 [Target] 거래량:{len(top_5_vol)}개, AI:{added_ai}개, 캐시:{len(self.backtester.results_cache)}개")
            print(f">>> 📊 [Target] 전체목록: {list(targets_map.keys())}")
            if ai_list:
                print(f">>> 📊 [Target] AI종목: {ai_list}")

            if len(targets_map) < 10:
                for t in top_50_tickers:
                    if t not in targets_map:
                        targets_map[t] = "거래량 상위(보충)"
                        if len(targets_map) >= 10: break

            # --- [2] 지갑 동기화 ---
            try:
                real_balances = await asyncio.to_thread(self.executor.get_all_balances)
                db_trades = self.repo.get_open_trades()
                db_tickers = [t['ticker'] for t in db_trades]
                real_wallet_tickers = []

                # 🔥 [안전장치] 업비트 API 인증 실패 감지 → 좀비 청산 차단 + Discord 알림
                # (이전 INJ 사고: get_balances가 {error:...} 또는 빈 list 반환 시 "지갑 비었음"
                #  으로 오해해 close_zombie 호출했던 버그 재발 방지)
                api_failed = False
                if isinstance(real_balances, dict) and real_balances.get("error"):
                    api_failed = True
                    detail = str(real_balances.get("error"))
                elif not isinstance(real_balances, list):
                    api_failed = True
                    detail = f"비정상 응답: {type(real_balances).__name__}"
                elif len(real_balances) == 0 and db_trades:
                    # 보유 거래는 있는데 잔고 응답 빈 list = 의심스러움
                    api_failed = True
                    detail = "empty list (보유 거래 있음에도)"

                if api_failed:
                    if not getattr(self, "_last_upbit_auth_alert", 0) or time.time() - self._last_upbit_auth_alert > 3600:
                        try:
                            from app.services import notifier
                            asyncio.create_task(notifier.notify_upbit_auth_fail(detail))
                        except Exception:
                            pass
                        self._last_upbit_auth_alert = time.time()
                    print(f"⚠️ [Wallet Sync] 업비트 응답 이상: {detail} → 좀비 청산 건너뜀")
                    raise RuntimeError(f"upbit balance api failed: {detail}")

                if isinstance(real_balances, list):
                    for b in real_balances:
                        if not isinstance(b, dict) or b['currency'] == 'KRW': continue
                        ticker = f"KRW-{b['currency']}"
                        qty = float(b['balance']) + float(b['locked'])
                        avg_price = float(b['avg_buy_price'])
                        total_val = qty * avg_price
                        if total_val > 5000:
                            real_wallet_tickers.append(ticker)
                            if ticker not in db_tickers:
                                self.repo.log_buy(ticker, avg_price, total_val)
                                db_tickers.append(ticker) 

                for trade in db_trades:
                    if trade['ticker'] not in real_wallet_tickers:
                        self.repo.close_zombie_trade(trade['id'])
                
                final_open_tickers = self.repo.get_all_open_tickers()
                for t in final_open_tickers:
                    if t in targets_map:
                        if "(보유중)" not in targets_map[t]: targets_map[t] += " (보유중)"
                    else:
                        targets_map[t] = "내 보유 코인 (관리중)"
            except Exception as e:
                print(f"Sync Error: {e}")

            # --- [3] Market Status 업데이트 ---
            final_targets = list(targets_map.keys())
            
            missing_tickers = [t for t in final_targets if t not in self.shared_data]
            if missing_tickers:
                try:
                    prices = await asyncio.to_thread(pyupbit.get_current_price, missing_tickers)
                    if prices is None: prices = {}
                    elif isinstance(prices, (float, int)): prices = {missing_tickers[0]: prices}
                    for t, p in prices.items():
                        self.shared_data[t] = {"current_price": float(p), "acc_trade_price_24h": 0}
                except Exception as e:
                    print(f"⚠️ [Price Fill Error] {missing_tickers}: {e}")

            new_status = {}
            for ticker in final_targets:
                existing = self.market_status.get(ticker, {})
                cached = self.backtester.get_analysis(ticker)
                realtime_price = self.shared_data.get(ticker, {}).get('current_price', 0)
                
                base_data = {
                    "price": realtime_price,
                    "score": 0,
                    "reasons": [],
                    "target": 0,
                    "rsi": 50,
                    "mfi": 50,
                    "atr": 0,
                    "stop_loss_price": 0,
                    "strategies": {},
                    "score_breakdown": [],
                    "category": targets_map.get(ticker, "관찰 종목")
                }
                
                if existing:
                    base_data.update(existing)
                
                if cached:
                    base_data.update({
                        "score": cached.get('score', 0),
                        "target": cached.get('target_price', 0),
                        "rsi": cached.get('rsi', 50),
                        "mfi": cached.get('mfi', 50),
                        "atr": cached.get('atr', 0),
                        "stop_loss_price": cached.get('stop_loss_price', 0),
                        "strategies": cached.get('strategies', {}),
                        "score_breakdown": cached.get('score_breakdown', []),
                        "regime": cached.get('regime', 'normal'),
                        "adx": cached.get('adx', 0),
                    })
                
                base_data["price"] = realtime_price
                base_data["category"] = targets_map.get(ticker, "관찰 종목")
                
                new_status[ticker] = base_data
                
            self.target_coins = final_targets
            self.market_status = new_status
            
        except Exception as e: print(f"Target Update Error: {e}")

    async def get_smart_candles(self, ticker):
        now = time.time()
        last_call = self.last_api_call_time.get(ticker, 0)
        
        # 🔥 [시스템최적화] API 호출 제한 (MIN_OHLCV_INTERVAL) 적용
        if now - last_call > self.MIN_OHLCV_INTERVAL or ticker not in self.cached_day_dfs:
            try:
                df_day = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="day", count=60)
                df_min = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="minute60", count=60)
                # [분봉 모델 v3] ML 추론용 minute5 (ma_60 위해 최소 60봉, 여유 200봉)
                df_5m = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="minute5", count=200)
                if df_day is not None:
                    self.cached_day_dfs[ticker] = df_day
                    self.cached_min_dfs[ticker] = df_min if df_min is not None else df_day
                    if df_5m is not None:
                        self.cached_5m_dfs[ticker] = df_5m
                    self.last_api_call_time[ticker] = now
            except Exception as e:
                # 🔥 [시스템최적화] 조용한 에러 방지 (로그 출력)
                print(f"⚠️ [Candle Error] {ticker}: {e}")
        
        if ticker not in self.cached_day_dfs: return None, None, 0, False
        
        # 🔥 [시스템최적화] 데이터 복사(copy) 대신 덮어쓰기 방식으로 최적화
        df_day = self.cached_day_dfs[ticker].copy()
        df_min = self.cached_min_dfs[ticker].copy()
        is_realtime = False
        current_price = 0
        
        if self.shared_data and ticker in self.shared_data:
            current_price = self.shared_data[ticker].get('current_price', 0)
            is_realtime = current_price > 0
            
        if is_realtime and current_price > 0:
            if 'close' in df_day.columns:
                df_day.iloc[-1, df_day.columns.get_loc('close')] = current_price
            if 'close' in df_min.columns:
                df_min.iloc[-1, df_min.columns.get_loc('close')] = current_price
        else:
            current_price = df_day.iloc[-1]['close']
            
        return df_day, df_min, current_price, is_realtime

    async def refresh_target_scores(self):
        """타겟 종목만 실시간 OHLCV로 점수 재계산 (5분 주기)"""
        refreshed = 0
        for ticker in list(self.target_coins):
            try:
                df_day, df_min, current_price, _ = await self.get_smart_candles(ticker)
                if df_day is None or len(df_day) < 30:
                    continue
                res = self.strategy.get_ensemble_signal(df_day, df_min)
                if not res:
                    continue

                # [분봉 모델 v3] ML 예측 (minute5) — 매수루프가 조기 return해도 카드에 항상 표시
                df_5m = self.cached_5m_dfs.get(ticker)
                if self.ml.is_trained and df_5m is not None and len(df_5m) >= 60:
                    ml_result = self.ml.predict_with_reasons(df_5m)
                    res['ml_prob'] = ml_result['prob']
                    res['ml_top_reasons'] = [
                        {"label": r["label"], "direction": r["direction"], "value": r["value"]}
                        for r in ml_result['reasons'][:3]
                    ]
                else:
                    res['ml_prob'] = None
                    res['ml_top_reasons'] = []

                self.backtester.results_cache[ticker] = {
                    "ticker": ticker,
                    "win_rate": self.backtester.results_cache.get(ticker, {}).get('win_rate', 0),
                    "total_yield": self.backtester.results_cache.get(ticker, {}).get('total_yield', 0),
                    "mdd": self.backtester.results_cache.get(ticker, {}).get('mdd', 0),
                    "score": float(res['score']),
                    "should_buy": bool(res['should_buy']),
                    "current_price": float(current_price),
                    "target_price": float(res.get('target_price', 0)),
                    "stop_loss_price": float(res.get('stop_loss_price', 0)),
                    "atr": float(res.get('atr', 0)),
                    "rsi": float(res['rsi']),
                    "mfi": float(res['mfi']),
                    "strategies": {k: int(v) for k, v in res['strategies'].items()},
                    "score_breakdown": res.get("score_breakdown", []),
                    "regime": res.get('regime', 'normal'),
                    "adx": res.get('adx', 0),
                    "ml_prob": res.get('ml_prob'),
                }
                self._update_market_status(ticker, current_price, res)
                refreshed += 1
                await asyncio.sleep(0.1)
            except Exception:
                pass
        if refreshed > 0:
            print(f">>> 🔄 [Refresh] 타겟 {refreshed}개 종목 점수 갱신 완료")

    def cleanup_old_cache(self):
        active_tickers = set(self.target_coins)
        for ticker in list(self.cached_day_dfs.keys()):
            if ticker not in active_tickers: del self.cached_day_dfs[ticker]
        for ticker in list(self.cached_min_dfs.keys()):
            if ticker not in active_tickers: del self.cached_min_dfs[ticker]
        for ticker in list(self.cached_5m_dfs.keys()):
            if ticker not in active_tickers: del self.cached_5m_dfs[ticker]
        for ticker in list(self.last_api_call_time.keys()):
            if ticker not in active_tickers: del self.last_api_call_time[ticker]
        for ticker in list(self.high_watermarks.keys()):
            if ticker not in active_tickers: del self.high_watermarks[ticker]
        
        # 🔥 [시스템최적화] TTL 만료된 캐시 강제 삭제
        now = time.time()
        stale = [
            t for t, ts in self.last_api_call_time.items()
            if now - ts > self.CACHE_TTL_SECONDS
        ]
        for t in stale:
            self.cached_day_dfs.pop(t, None)
            self.cached_min_dfs.pop(t, None)
            self.last_api_call_time.pop(t, None)
        
        expired = [t for t, ts in self.sell_timestamps.items() if now - ts > self.REBUY_COOLDOWN]
        for t in expired:
            del self.sell_timestamps[t]

    def _update_market_status(self, ticker, price, result):
        if not result: return
        active_reasons = [self.STRATEGY_MAP.get(k, k) for k, v in result['strategies'].items() if v == 1]
        
        last_sell_time = self.sell_timestamps.get(ticker, 0)
        remaining = self.REBUY_COOLDOWN - (time.time() - last_sell_time)
        if remaining > 0:
            if "❄️쿨타임" not in active_reasons:
                active_reasons.append(f"❄️쿨타임({int(remaining/60)}분)")

        if ticker in self.market_status:
            payload = {
                "price": price,
                "score": result['score'],
                "reasons": active_reasons,
                "target": result.get('target_price', 0),
                "rsi": result['rsi'],
                "mfi": result.get('mfi', 50),
                "atr": result.get('atr', 0),
                "stop_loss_price": result.get('stop_loss_price', 0),
                "strategies": result['strategies'],
                "score_breakdown": result.get('score_breakdown', []),
                "regime": result.get('regime', 'normal'),
                "adx": result.get('adx', 0),
                "skip_reason": None,
            }
            # 🔥 ml_prob/ml_top_reasons는 result에 값이 있을 때만 갱신 — 값이 없는 경로
            # (process_selling, 매수차단 조기 return 등)가 직전 캐시값을 None으로 덮어쓰는 것 방지
            if result.get('ml_prob') is not None:
                payload["ml_prob"] = result['ml_prob']
                payload["ml_top_reasons"] = result.get('ml_top_reasons', [])
            self.market_status[ticker].update(payload)
            
    def _set_skip_reason(self, ticker, reason):
        """점수는 통과했지만 필터에 걸린 이유를 market_status에 기록"""
        if ticker in self.market_status:
            self.market_status[ticker]["skip_reason"] = reason

    def _is_holding(self, ticker):
        if self.SHADOW_MODE:
            return self.paper_repo.is_holding(ticker)
        if ticker in self.market_status:
            return "(보유중)" in self.market_status[ticker].get("category", "")
        return False

    def update_frontend_cache(self):
        open_trades = self.repo.get_open_trades() 
        holdings_map = {t['ticker']: t['buy_price'] for t in open_trades}

        total_krw = 0
        total_coin_val = 0
        
        try:
            all_balances = self.executor.get_all_balances()
            balance_dict = {}
            for b in all_balances:
                if b['currency'] == 'KRW':
                    total_krw = float(b['balance'])
                else:
                    ticker = f"KRW-{b['currency']}"
                    balance_dict[ticker] = float(b['balance']) + float(b['locked'])

            for ticker in holdings_map.keys():
                qty = balance_dict.get(ticker, 0)
                current_price = self.shared_data.get(ticker, {}).get('current_price', 0)
                total_coin_val += (qty * current_price)
                
        except Exception as e:
            print(f"⚠️ [Frontend Update Error] {e}")

        # [Phase 1B] 모든 ticker의 24h 뉴스 sentiment summary 한 번에 로드 (1분 캐시)
        try:
            from app.services.news_collector import news_collector as _nc
            news_summaries = _nc.get_all_ticker_summaries(hours=24)
        except Exception:
            news_summaries = {}

        items_list = []
        for ticker, data in self.market_status.items():
            item = data.copy()
            item['ticker'] = ticker

            if not item.get('reasons') and item.get('strategies'):
                active_reasons = [self.STRATEGY_MAP.get(k, k) for k, v in item['strategies'].items() if v == 1]
                item['reasons'] = active_reasons

            if self.shared_data and ticker in self.shared_data:
                item['price'] = self.shared_data[ticker].get('current_price', 0)

            if ticker in holdings_map:
                buy_price = holdings_map[ticker]
                current_price = item['price']
                if buy_price > 0:
                    profit_rate = ((current_price - buy_price) / buy_price) * 100
                    item['buy_price'] = buy_price
                    item['profit_rate'] = profit_rate

            # [Phase 1B] 뉴스 메타 (UI 뱃지용)
            symbol = ticker.replace('KRW-', '').upper()
            ns = news_summaries.get(symbol)
            if ns and ns.get('count', 0) > 0:
                item['news'] = {
                    'count': ns['count'],
                    'sentiment': ns['avg_sentiment'],
                    'critical': ns['critical_count'],
                }

            items_list.append(item)

        # ML Top 코인 (일일 스캔 기반, 프론트 전용)
        # [A] 거래대금 10억 이상만 (잡코인 제외 — 매수 AI 기준과 동일, "보이는 것=살 수 있는 것")
        ml_top_coins = []
        try:
            vol_map = {}
            if self.shared_data:
                snap = self._snapshot_shared_data()
                vol_map = {t: d.get('acc_trade_price_24h', 0) for t, d in snap.items()}
            raw_ml_top = self.backtester.get_ml_top_coins(top_n=10, vol_map=vol_map, min_vol=1_000_000_000)
            existing_tickers = {item['ticker'] for item in items_list}
            for c in raw_ml_top:
                ticker = c['ticker']
                if ticker in existing_tickers:
                    continue
                realtime_price = 0
                if self.shared_data and ticker in self.shared_data:
                    realtime_price = self.shared_data[ticker].get('current_price', 0)
                symbol = ticker.replace('KRW-', '').upper()
                ns = news_summaries.get(symbol)
                ml_item = {
                    "ticker": ticker,
                    "price": realtime_price or c.get('current_price', 0),
                    "score": c.get('score', 0),
                    "ml_prob": c.get('ml_prob'),
                    "rsi": c.get('rsi', 50),
                    "target": c.get('target_price', 0),
                    "reasons": [],
                    "category": "ML 상승예측",
                    "regime": c.get('regime', 'normal'),
                    "adx": c.get('adx', 0),
                    "ml_top_reasons": [],
                    "skip_reason": None,
                }
                if ns and ns.get('count', 0) > 0:
                    ml_item['news'] = {
                        'count': ns['count'],
                        'sentiment': ns['avg_sentiment'],
                        'critical': ns['critical_count'],
                    }
                ml_top_coins.append(ml_item)
        except Exception as e:
            print(f"⚠️ [ML Top Coins Error] {e}")

        # 시장 레짐 & 쿨오프 상태 (프론트 안내용)
        # _get_market_regime은 30분 캐시 → 매 루프 호출해도 실제 조회는 30분마다.
        # 쿨오프/심야로 process_buying이 조기 return돼도 BTC 지표가 항상 갱신되도록 여기서 호출.
        regime = self._get_market_regime()
        cooloff_remaining = 0
        if time.time() < self.loss_cooloff_until:
            cooloff_remaining = int((self.loss_cooloff_until - time.time()) / 60)

        # 심야 매수 차단 여부
        now_hour = datetime.now(KST).hour
        is_night_block = now_hour >= self.NIGHT_BUY_BLOCK_START or now_hour < self.NIGHT_BUY_BLOCK_END

        self.frontend_cache = {
            "data": items_list,
            "ml_top_coins": ml_top_coins,
            "summary": {
                "krw_balance": total_krw,
                "total_assets": total_krw + total_coin_val,
                "coin_value": total_coin_val,
                "market_regime": regime or "neutral",
                "btc": self._market_regime_detail,
                "cooloff_remaining_min": cooloff_remaining,
                "night_block": is_night_block,
            }
        }

trade_manager = TradeManager()