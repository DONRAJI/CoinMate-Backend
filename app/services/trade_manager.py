import asyncio
import time
import gc
from datetime import datetime

from app.core.trade_repository import TradeRepository
from app.core.logger import get_logger
from app.services.order_executor import OrderExecutor
from app.services.strategy import Strategy
from app.services.backtester import Backtester
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

        self.REBUY_COOLDOWN = 3600 
        
        # 설정값
        self.MAX_COIN_COUNT = 4
        self.MIN_ORDER_KRW = 6000
        self.CACHE_TTL_SECONDS = 300
        self.MIN_OHLCV_INTERVAL = 60
        self.PROFIT_TARGET = 3.5
        self.STOP_LOSS = -2.0
        self.TRAILING_ACTIVATION = 2.0
        self.TRAILING_DISTANCE = 1.5
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
                    self.cleanup_old_cache()
                
                now = datetime.now()
                if now.hour == 0 and now.minute == 1 and loop_count % 60 == 0:
                    asyncio.create_task(self.backtester.run_daily_scan())
                    self.sell_timestamps.clear()

                await self.process_selling()
                if self.is_active:
                    await self.process_buying()
                
                self.update_frontend_cache()
                
                loop_count += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"[Loop Error] {e}", exc_info=True)
                asyncio.create_task(notifier.notify_error("MainLoop", str(e)))
                await asyncio.sleep(5)

    async def process_selling(self):
        """
        [수정 내역]
        기존: for trade_id, ticker, buy_price, _, _ in open_trades:
        변경: for trade in open_trades: ... trade['id']
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
            self._update_market_status(ticker, current, res)

            is_strong_trend = res['strategies'].get('adx', 0) == 1

            reason = ""
            # 1. 손절 (최우선)
            if profit_rate <= self.STOP_LOSS:
                reason = f"손절방어({profit_rate:.2f}%)"

            # 2. 트레일링 스탑 (강한 추세일 때만)
            elif is_strong_trend and profit_rate >= self.TRAILING_ACTIVATION and drawdown_from_high >= self.TRAILING_DISTANCE:
                reason = f"트레일링({profit_rate:.2f}%,고점대비-{drawdown_from_high:.1f}%)"

            # 3. 고정 익절 (추세 약할 때)
            elif not is_strong_trend and profit_rate >= self.PROFIT_TARGET:
                reason = f"익절달성({profit_rate:.2f}%)"

            # 4. 수익권 과열 체크
            elif profit_rate > 0.5:
                if res['rsi'] >= 80: reason = f"RSI과열({profit_rate:.2f}%)"
                elif res.get('mfi', 0) >= 85: reason = f"MFI과열({profit_rate:.2f}%)"

            # 5. 전략 점수 급락
            elif res['score'] < 3.5:
                reason = f"점수하락({res['score']}점)"

            # 6. 이상 징후
            elif res['rsi'] < 50 and res.get('mfi', 0) >= 75:
                reason = f"이상징후(설거지감지)"

            # --- [매도 실행] ---
            if reason and self.is_active:
                log.info(f"[매도 판단] {ticker} -> {reason}")
                success = await self.executor.try_sell(trade_id, ticker, current, reason)
                if success:
                    self.sell_timestamps[ticker] = time.time()
                    self.high_watermarks.pop(ticker, None)
                    asyncio.create_task(notifier.notify_sell(ticker, current, profit_rate, reason))

                    if ticker in self.market_status:
                        self.market_status[ticker]["category"] = "관찰 종목"

    async def process_buying(self):
        # --- [1] 먼저 예산/슬롯 확인 ---
        active_cnt = self.repo.get_trade_count()
        empty_slots = self.MAX_COIN_COUNT - active_cnt
        
        krw = self.executor.get_krw_balance()
        can_buy = (empty_slots > 0) and (krw >= self.MIN_ORDER_KRW)
        
        budget = 0
        if can_buy:
            budget = (krw * 0.99) / empty_slots
            if budget < self.MIN_ORDER_KRW: budget = krw * 0.99

        # --- [2] 종목 스캔 & 점수 업데이트 ---
        candidates = []
        
        for ticker in self.target_coins:
            last_sell = self.sell_timestamps.get(ticker, 0)
            is_cooldown = (time.time() - last_sell < self.REBUY_COOLDOWN)
            is_holding = self._is_holding(ticker)

            df_day, df_min, current, is_real = await self.get_smart_candles(ticker)
            if not is_real: continue
            
            res = self.strategy.get_ensemble_signal(df_day, df_min)
            
            # UI용 상태 업데이트
            self._update_market_status(ticker, current, res)
            
            if not can_buy or is_holding or is_cooldown: continue

            # --- 매수 후보 필터링 로직 ---
            res['ticker'] = ticker 
            res['current_price'] = current

            rsi = res['rsi']
            mfi = res.get('mfi', 50)
            score = res['score']

            if rsi >= 70: continue         
            if mfi >= 80: continue        
            if rsi >= 60 and mfi < 40: continue
            if score < self.strategy.BUY_THRESHOLD: continue

            last_open = df_min['open'].iloc[-1]
            last_close = df_min['close'].iloc[-1]
            last_high = df_min['high'].iloc[-1]
            last_low = df_min['low'].iloc[-1]
            body_size = abs(last_close - last_open)
            upper_wick = last_high - max(last_open, last_close)
            if body_size > 0 and upper_wick > (body_size * 2):
                continue

            volume_ma20 = df_min['volume'].rolling(20).mean().iloc[-1]
            price_change_pct = ((last_close - last_open) / last_open) * 100 if last_open > 0 else 0
            if price_change_pct > 3 and df_min['volume'].iloc[-1] < volume_ma20:
                continue

            price_range_pct = ((last_high - last_low) / last_open) * 100 if last_open > 0 else 0
            if price_range_pct > 10:
                continue

            candidates.append(res)
        
        # --- [3] 실제 매수 실행 ---
        if candidates and can_buy:
            candidates.sort(key=lambda x: (x['score'], x['mfi']), reverse=True)
            final_picks = candidates[:empty_slots]
            
            for pick in final_picks:
                ticker = pick.get('ticker')
                price = pick.get('current_price')
                
                if not ticker: continue
                
                strategies = [k for k, v in pick['strategies'].items() if v == 1]
                strategy_name = "+".join(strategies) if strategies else "AI_Ensemble"
                
                log.info(f"[Pick] {ticker} (점수:{pick['score']} / RSI:{pick['rsi']:.1f}) -> 매수")

                success = await self.executor.try_buy(ticker, price, budget, strategy_name)
                if success:
                    asyncio.create_task(notifier.notify_buy(ticker, price, budget, strategy_name))
                    if ticker in self.market_status:
                        self.market_status[ticker]['category'] = self.market_status[ticker].get("category", "") + " (보유중)"
                    await asyncio.sleep(0.2)

    async def place_manual_buy(self, ticker, krw_amount):
        """수동 매수 (시장가)"""
        try:
            current_krw = self.executor.get_krw_balance()
            if current_krw < krw_amount:
                return {"status": "error", "message": f"잔액 부족 (보유: {current_krw:,.0f}원)"}
            
            current_price = pyupbit.get_current_price(ticker)
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
            trade_row = self.repo.get_open_trade(ticker)
            trade_id = trade_row[0] if trade_row else 0
            
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
            for t in ai_candidates:
                if t in targets_map: continue
                if t in top_50_tickers:
                    targets_map[t] = "AI 추천(우량주)"
                    added_ai += 1
                if added_ai >= 5: break
            
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
                    if isinstance(prices, (float, int)): prices = {missing_tickers[0]: prices}
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
                        "score_breakdown": cached.get('score_breakdown', [])
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
            current_price = self.shared_data[ticker]['current_price']
            is_realtime = True
            
        if is_realtime and current_price > 0:
            if 'close' in df_day.columns:
                df_day.iloc[-1, df_day.columns.get_loc('close')] = current_price
            if 'close' in df_min.columns:
                df_min.iloc[-1, df_min.columns.get_loc('close')] = current_price
        else:
            current_price = df_day.iloc[-1]['close']
            
        return df_day, df_min, current_price, is_realtime

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
                "score_breakdown": result.get('score_breakdown', [])
            })
            
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
            if not isinstance(all_balances, list):
                raise ValueError(f"API 응답 오류: {all_balances}")
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
                item['price'] = self.shared_data[ticker]['current_price']
            
            if ticker in holdings_map:
                buy_price = holdings_map[ticker]
                current_price = item['price']
                if buy_price > 0:
                    profit_rate = ((current_price - buy_price) / buy_price) * 100
                    item['buy_price'] = buy_price
                    item['profit_rate'] = profit_rate
            
            items_list.append(item)

        self.frontend_cache = {
            "data": items_list,
            "summary": {
                "krw_balance": total_krw,
                "total_assets": total_krw + total_coin_val,
                "coin_value": total_coin_val
            }
        }

trade_manager = TradeManager()