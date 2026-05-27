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
        self.ML_MIN_PROB = 0.55  # ML 최소 상승 확률
        
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
        self.last_api_call_time = {}
        self.sell_timestamps = {}

        self.REBUY_COOLDOWN = 1800

        # ═══ [개선 1] 심야 매수 차단 (22~07시 KST) ═══
        self.NIGHT_BUY_BLOCK_START = 22  # 22시부터
        self.NIGHT_BUY_BLOCK_END = 7     # 07시까지

        # ═══ [개선 2] 연속 손절 쿨오프 ═══
        self.recent_trade_results = []    # 최근 거래 결과 (True=승, False=패)
        self.CONSECUTIVE_LOSS_LIMIT = 3   # N연패 시 매수 중단
        self.loss_cooloff_until = 0       # 쿨오프 해제 시간 (timestamp)
        self.LOSS_COOLOFF_SECONDS = 3600  # 1시간 매수 중단

        # ═══ [개선 4] 최소 보유 시간 ═══
        self.MIN_HOLD_MINUTES = 30        # 매수 후 30분간 점수하락 매도 차단

        # ═══ [개선 5] 코인별 연속 손절 블랙리스트 ═══
        self.coin_loss_streak = {}        # {ticker: 연속 손실 횟수}
        self.coin_blacklist_until = {}    # {ticker: 블랙리스트 해제 시간}
        self.COIN_LOSS_STREAK_LIMIT = 2   # 같은 코인 2연패 시 블랙리스트
        self.COIN_BLACKLIST_SECONDS = 7200  # 2시간 블랙리스트

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
        """
        open_trades = self.repo.get_open_trades()
        
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

            if is_sideways:
                # 횡보장: 기본 타이트 + ATR 보정
                stop_loss = max(-1.5, -(atr_pct * 1.0)) if atr_pct > 0 else -1.5
                profit_target = max(2.0, atr_pct * 1.5) if atr_pct > 0 else 2.0
            else:
                # 추세장/일반: ATR 기반 동적 또는 기본값
                if atr_pct > 0:
                    stop_loss = max(self.STOP_LOSS, -(atr_pct * 1.2))   # ATR 1.2배, 기본값 이하로는 안 내림
                    profit_target = max(self.PROFIT_TARGET, atr_pct * 2.0)  # ATR 2배
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
                success = await self.executor.try_sell(trade_id, ticker, current, reason)
                if success:
                    self.sell_timestamps[ticker] = time.time()
                    self.high_watermarks.pop(ticker, None)
                    asyncio.create_task(notifier.notify_sell(ticker, current, profit_rate, reason))

                    if ticker in self.market_status:
                        self.market_status[ticker]["category"] = "관찰 종목"

                    # ═══ [개선 2] 연속 손절 추적 ═══
                    is_loss = profit_rate <= 0
                    self.recent_trade_results.append(not is_loss)
                    if len(self.recent_trade_results) > 10:
                        self.recent_trade_results = self.recent_trade_results[-10:]

                    # 최근 N건 연속 패배 체크
                    consecutive_losses = 0
                    for r in reversed(self.recent_trade_results):
                        if not r:
                            consecutive_losses += 1
                        else:
                            break

                    if consecutive_losses >= self.CONSECUTIVE_LOSS_LIMIT:
                        self.loss_cooloff_until = time.time() + self.LOSS_COOLOFF_SECONDS
                        log.warning(f"[쿨오프] {consecutive_losses}연패 감지! {self.LOSS_COOLOFF_SECONDS//60}분간 매수 중단")
                        asyncio.create_task(notifier.notify_error(
                            "연속손절 쿨오프",
                            f"{consecutive_losses}연패 → {self.LOSS_COOLOFF_SECONDS//60}분 매수 중단"
                        ))

                    # ═══ [개선 5] 코인별 연속 손절 블랙리스트 ═══
                    if is_loss:
                        self.coin_loss_streak[ticker] = self.coin_loss_streak.get(ticker, 0) + 1
                        if self.coin_loss_streak[ticker] >= self.COIN_LOSS_STREAK_LIMIT:
                            self.coin_blacklist_until[ticker] = time.time() + self.COIN_BLACKLIST_SECONDS
                            log.warning(f"[블랙리스트] {ticker}: {self.coin_loss_streak[ticker]}연패 → {self.COIN_BLACKLIST_SECONDS//3600}시간 차단")
                    else:
                        self.coin_loss_streak[ticker] = 0  # 승리 시 연패 초기화

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

        # --- [1] 먼저 예산/슬롯 확인 ---
        active_cnt = self.repo.get_trade_count()
        empty_slots = self.MAX_COIN_COUNT - active_cnt

        krw = self.executor.get_krw_balance()
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
            if self.ml.is_trained:
                ml_result = self.ml.predict_with_reasons(df_day)
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

            if score < self.strategy.BUY_THRESHOLD:
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

            # ML 예측 (v2: 낮은 확률이면 차단)
            ml_prob = res.get('ml_prob') or 0.5
            if self.ml.is_trained:
                print(f"  🤖 [ML] {ticker}: 상승확률 {ml_prob:.1%}")
                if ml_prob < self.ML_MIN_PROB:
                    print(f"  🚫 [Skip] {ticker}: ML 확률 낮음 ({ml_prob:.1%} < {self.ML_MIN_PROB:.0%})")
                    self._set_skip_reason(ticker, f"🤖 ML 확률 낮음 ({ml_prob:.0%})")
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
            max_per_coin = available_krw * MAX_SINGLE_RATIO

            for pick in final_picks:
                ticker = pick.get('ticker')
                price = pick.get('current_price')
                if not ticker: continue

                if len(final_picks) == 1:
                    budget = min(available_krw, max_per_coin)
                else:
                    weight = pick['score'] / total_score if total_score > 0 else 1.0 / len(final_picks)
                    budget = min(available_krw * weight, max_per_coin)

                if budget < self.MIN_ORDER_KRW:
                    print(f"  💸 [Skip] {ticker}: 예산부족({budget:.0f}원 < {self.MIN_ORDER_KRW}원)")
                    continue

                strategies = [k for k, v in pick['strategies'].items() if v == 1]
                strategy_name = "+".join(strategies) if strategies else "AI_Ensemble"

                ml_p = pick.get('ml_prob') or 0.5
                log.info(f"[Pick] {ticker} (점수:{pick['score']} / RSI:{pick['rsi']:.1f} / ML:{ml_p:.0%} / 예산:{budget:.0f}원) -> 매수")

                success = await self.executor.try_buy(ticker, price, budget, strategy_name)
                if success:
                    available_krw -= budget
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
            success = await self.executor.try_buy(ticker, current_price, krw_amount, "Manual(수동)")
            
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
                if df_day is not None:
                    self.cached_day_dfs[ticker] = df_day
                    self.cached_min_dfs[ticker] = df_min if df_min is not None else df_day
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
            self.market_status[ticker].update({
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
                "ml_prob": result.get('ml_prob', None),
                "ml_top_reasons": result.get('ml_top_reasons', []),
                "skip_reason": None,
            })
            
    def _set_skip_reason(self, ticker, reason):
        """점수는 통과했지만 필터에 걸린 이유를 market_status에 기록"""
        if ticker in self.market_status:
            self.market_status[ticker]["skip_reason"] = reason

    def _is_holding(self, ticker):
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
            
            items_list.append(item)

        # ML Top 코인 (일일 스캔 기반, 프론트 전용)
        ml_top_coins = []
        try:
            raw_ml_top = self.backtester.get_ml_top_coins(top_n=10)
            existing_tickers = {item['ticker'] for item in items_list}
            for c in raw_ml_top:
                ticker = c['ticker']
                if ticker in existing_tickers:
                    continue
                realtime_price = 0
                if self.shared_data and ticker in self.shared_data:
                    realtime_price = self.shared_data[ticker].get('current_price', 0)
                ml_top_coins.append({
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
                })
        except Exception as e:
            print(f"⚠️ [ML Top Coins Error] {e}")

        self.frontend_cache = {
            "data": items_list,
            "ml_top_coins": ml_top_coins,
            "summary": {
                "krw_balance": total_krw,
                "total_assets": total_krw + total_coin_val,
                "coin_value": total_coin_val
            }
        }

trade_manager = TradeManager()