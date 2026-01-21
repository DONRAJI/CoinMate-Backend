import asyncio
import time
import gc
from datetime import datetime

# 분리한 파일들 임포트
from app.core.trade_repository import TradeRepository
from app.services.order_executor import OrderExecutor
from app.services.strategy import Strategy
from app.services.backtester import Backtester
from app.core.database import init_db
import pyupbit

class TradeManager:
    def __init__(self):
        # 1. 하위 직원들 고용
        init_db()
        self.repo = TradeRepository()
        self.executor = OrderExecutor(self.repo)
        self.strategy = Strategy()
        self.backtester = Backtester()
        
        self.is_active = False
        self.shared_data = None
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
        # 🔥 [수정 포인트] 시드가 적을 때는 1~2개로 집중 투자 (현재 1로 설정됨)
        self.MAX_COIN_COUNT = 1
        self.MIN_ORDER_KRW = 6000
        self.PROFIT_TARGET = 3.5
        self.STOP_LOSS = -3.0
        
        self.STRATEGY_MAP = {
            "trend": "추세", "volume": "거래량폭발", "stoch": "골든크로스",
            "rsi": "RSI안정", "mfi": "자금유입", "bollinger": "밴드지지",
            "macd": "MACD", "adx": "강한추세", "vwap": "세력평단", "cci": "과매도탈출"
        }

    def set_shared_data(self, shared_dict):
        self.shared_data = shared_dict

    def start(self):
        self.is_active = True
        print(">>> 🚀 System STARTED")

    def stop(self):
        self.is_active = False
        print(">>> 🛑 System STOPPED")

    async def run_loop(self):
        print(">>> 🔄 Main Loop Initialized...")
        print(">>> ⏳ [System] 실시간 시세 데이터 수신 대기 중...")
        
        # 초기 데이터 대기
        while True:
            if self.shared_data and len(self.shared_data) > 10: 
                print(">>> 📶 [System] 실시간 데이터 수신 확인됨!")
                break
            await asyncio.sleep(1)
            
        print(">>> ⏳ [System] 초기 데이터 분석 중...")
        await self.backtester.run_daily_scan()
        await self.update_target_coins() # 첫 실행
        
        loop_count = 0
        while True:
            try:
                # 5분마다 타겟 갱신 & 동기화 & 캐시 정리
                if loop_count % 300 == 0:
                    await self.update_target_coins()
                    self.cleanup_old_cache()
                    
                # 09:01 정기 점검 (UTC 0시 = 한국 9시)
                now = datetime.now()
                if now.hour == 0 and now.minute == 1 and loop_count % 60 == 0:
                    asyncio.create_task(self.backtester.run_daily_scan())
                    self.sell_timestamps.clear()

                # 1. 매도 진행 (Executor에게 위임)
                await self.process_selling()
                
                # 2. 매수 진행 (Executor에게 위임)
                if self.is_active:
                    await self.process_buying()
                
                # 프론트엔드용 데이터 생성
                self.update_frontend_cache()
                
                loop_count += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                print(f"[Loop Error] {e}")
                await asyncio.sleep(5)

    async def process_selling(self):
        """
        [수정 내역]
        기존: for trade_id, ticker, buy_price, _, _ in open_trades: (개수 안 맞으면 에러남)
        변경: for trade in open_trades: ... trade['id'] (이름으로 찾으므로 안전함)
        """
        open_trades = self.repo.get_open_trades()
        
        # 🔥 [핵심 수정] 리스트에서 객체 하나(trade)를 통째로 가져옵니다.
        for trade in open_trades:
            # 🔥 [핵심 수정] 순서가 아니라 '이름'으로 값을 꺼냅니다. (DB 컬럼이 늘어나도 안전)
            # 주의: TradeRepository의 get_conn에서 row_factory = sqlite3.Row 설정이 되어 있어야 작동합니다.
            trade_id = trade['id']
            ticker = trade['ticker']
            buy_price = trade['buy_price']
            
            # --- 아래부터는 기존 로직과 동일 ---
            df_day, df_min, current, is_real = await self.get_smart_candles(ticker)
            if not is_real or current == 0: continue

            if buy_price <= 0: buy_price = current 
            profit_rate = ((current - buy_price) / buy_price) * 100
            
            res = self.strategy.get_ensemble_signal(df_day, df_min)
            self._update_market_status(ticker, current, res)

            reason = ""
            # 1. 익절/손절 기준 (최우선)
            if profit_rate >= self.PROFIT_TARGET:
                reason = f"💰익절달성({profit_rate:.2f}%)"
            elif profit_rate <= self.STOP_LOSS:
                reason = f"💧손절방어({profit_rate:.2f}%)"
            
            # 2. 수익권일 때 과열 지표 체크
            elif profit_rate > 0.5: 
                if res['rsi'] >= 80: reason = f"🔥RSI과열({profit_rate:.2f}%)"
                elif res.get('mfi', 0) >= 85: reason = f"🌊MFI과열({profit_rate:.2f}%)"
            
            # 3. 전략 점수 급락
            elif res['score'] < 3.5:
                reason = f"📉점수하락({res['score']}점)"
            
            # 4. 이상 징후 (가격은 내렸는데 MFI만 비정상적으로 높거나 등등)
            elif res['rsi'] < 50 and res.get('mfi', 0) >= 75:
                reason = f"⚠️이상징후(설거지감지)"

            # 매도 실행 로직
            if reason and self.is_active:
                print(f"👋 [매도 판단] {ticker} -> {reason}")
                success = await self.executor.try_sell(trade_id, ticker, current, reason)
                if success:
                    self.sell_timestamps[ticker] = time.time()
                    
                    # 매도 성공 시 카테고리 초기화 (UI에서 '보유중' 태그 즉시 삭제됨)
                    if ticker in self.market_status:
                        self.market_status[ticker]["category"] = "관찰 종목"

    async def process_buying(self):
        # 1. 자리 있나 확인
        active_cnt = self.repo.get_trade_count()
        empty_slots = self.MAX_COIN_COUNT - active_cnt
        if empty_slots <= 0: return
        
        # 2. 예산 확인
        krw = self.executor.get_krw_balance()
        if krw < self.MIN_ORDER_KRW: return
        
        budget = (krw * 0.99) / empty_slots
        if budget < self.MIN_ORDER_KRW: budget = krw * 0.99

        # 3. 종목 스캔
        candidates = []
        for ticker in self.target_coins:
            # 쿨타임 & 보유중 체크
            last_sell = self.sell_timestamps.get(ticker, 0)
            if time.time() - last_sell < self.REBUY_COOLDOWN: continue
            if self._is_holding(ticker): continue

            # 전략 분석
            df_day, df_min, current, is_real = await self.get_smart_candles(ticker)
            if not is_real: continue
            
            res = self.strategy.get_ensemble_signal(df_day, df_min)
            
            # 이름표 붙이기
            res['ticker'] = ticker 
            res['current_price'] = current
            
            self._update_market_status(ticker, current, res)
            
            # ---------------------------------------------------------
            # 🔥 [핵심 업그레이드] 과열/함정 필터링 로직
            # ---------------------------------------------------------
            rsi = res['rsi']
            mfi = res.get('mfi', 50)
            score = res['score']

            # 1. 절대 과열 기준 (너무 비쌈)
            if rsi >= 70: continue         # RSI 과열
            if mfi >= 80: continue         # 자금 유입 과다 (고점 징후)
            
            # 2. 가짜 상승 필터 (가격은 오르는데 돈이 안 들어옴)
            # RSI는 65로 높은데 MFI가 40 밑이다? -> 개미 꼬시기일 확률 높음
            if rsi >= 60 and mfi < 40: continue

            # 3. 점수 커트라인 (지표 중복을 고려해 6.0 -> 7.0으로 상향 조정)
            # 추세 지표가 많아서 6점은 너무 쉽게 넘기 때문입니다.
            if score < 7.0: continue

            candidates.append(res)
        
        # 4. 점수순 정렬 및 매수 실행
        # 점수 높은 순 -> MFI 낮은 순 (아직 돈이 덜 들어와서 먹을 게 남은 놈)
        candidates.sort(key=lambda x: (x['score'], x['mfi']), reverse=True)
        final_picks = candidates[:empty_slots]
        
        for pick in final_picks:
            ticker = pick.get('ticker')
            price = pick.get('current_price')
            
            if not ticker: continue
            
            strategies = [k for k, v in pick['strategies'].items() if v == 1]
            strategy_name = "+".join(strategies) if strategies else "AI_Ensemble"
            
            print(f"🏆 [Pick] {ticker} (점수:{pick['score']} / RSI:{pick['rsi']:.1f} / MFI:{pick['mfi']:.1f}) -> 매수")
            
            success = await self.executor.try_buy(ticker, price, budget, strategy_name)
            if success:
                if ticker in self.market_status:
                    self.market_status[ticker]['category'] = self.market_status[ticker].get("category", "") + " (보유중)"
                await asyncio.sleep(0.2)

    # -----------------------------------------------------------
    # ✋ [신규] 수동 매매 기능 (프론트엔드 버튼 클릭 시 호출)
    # -----------------------------------------------------------
    async def place_manual_buy(self, ticker, krw_amount):
        """수동 매수 (시장가)"""
        try:
            # 1. 예산 확인
            current_krw = self.executor.get_krw_balance()
            if current_krw < krw_amount:
                return {"status": "error", "message": f"잔액 부족 (보유: {current_krw:,.0f}원)"}
            
            # 2. 현재가 조회 (기록용)
            current_price = pyupbit.get_current_price(ticker)
            
            # 3. 매수 실행 (전략명: Manual)
            success = await self.executor.try_buy(ticker, current_price, krw_amount, "Manual(수동)")
            
            if success:
                # UI 즉시 반영
                if ticker in self.market_status:
                    self.market_status[ticker]['category'] = self.market_status[ticker].get("category", "") + " (보유중)"
                self.update_frontend_cache() # 캐시 강제 갱신
                return {"status": "success", "message": f"{ticker} 매수 성공!"}
            else:
                return {"status": "error", "message": "API 매수 주문 실패"}
                
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def place_manual_sell(self, ticker):
        """수동 매도 (전량 시장가)"""
        try:
            # 1. 보유량 확인
            balance = self.executor.get_coin_balance(ticker)
            if balance <= 0:
                return {"status": "error", "message": "매도할 잔액이 없습니다."}
            
            # 2. 현재가 조회 및 Trade ID 찾기 (DB 기록용)
            current_price = pyupbit.get_current_price(ticker)
            trade_row = self.repo.get_open_trade(ticker) # (id, buy_price, ...) 가져오는 함수 필요
            
            # get_open_trade가 없으면 임시 처리 (TradeManager 로직상 repo 수정 필요할 수 있음)
            # 여기서는 편의상 Executor가 알아서 처리하도록 위임
            trade_id = trade_row[0] if trade_row else 0
            
            # 3. 매도 실행 (이유: Manual)
            success = await self.executor.try_sell(trade_id, ticker, current_price, "Manual(수동)")
            
            if success:
                self.sell_timestamps[ticker] = time.time()
                # UI 즉시 반영 (보유중 태그 삭제)
                if ticker in self.market_status:
                    cat = self.market_status[ticker].get("category", "")
                    self.market_status[ticker]["category"] = cat.replace(" (보유중)", "")
                self.update_frontend_cache() # 캐시 강제 갱신
                return {"status": "success", "message": f"{ticker} 매도 성공!"}
            else:
                return {"status": "error", "message": "API 매도 주문 실패"}
                
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def update_target_coins(self):
        try:
            if not self.shared_data: return
            
            # 1. 종목 선정 로직 (기존 유지)
            MIN_TRADE_PRICE = 5_000_000_000 
            all_data = list(self.shared_data.items())
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

            # -----------------------------------------------------------
            # 🔥 [수정] 2. 지갑 동기화 (양방향 Sync)
            # -----------------------------------------------------------
            try:
                # (1) 실제 지갑 잔고 조회
                real_balances = await asyncio.to_thread(self.executor.get_all_balances)
                
                # (2) DB에 있는 거래 내역 조회
                db_trades = self.repo.get_open_trades() # [(id, ticker, price), ...]
                db_tickers = [t[1] for t in db_trades]
                
                real_wallet_tickers = []
                
                # A. 지갑 데이터 가공
                if real_balances:
                    for b in real_balances:
                        if b['currency'] == 'KRW': continue
                        
                        ticker = f"KRW-{b['currency']}"
                        qty = float(b['balance']) + float(b['locked'])
                        avg_price = float(b['avg_buy_price'])
                        total_val = qty * avg_price
                        
                        # 5000원 이상인 코인만 유효한 것으로 인정
                        if total_val > 5000:
                            real_wallet_tickers.append(ticker)
                            
                            # 🔥 [핵심 추가] 지갑엔 있는데 DB에 없으면 -> DB에 추가 (Import)
                            if ticker not in db_tickers:
                                print(f"📥 [Sync] {ticker} 지갑 보유분 발견 -> DB에 등록합니다.")
                                self.repo.log_buy(ticker, avg_price, total_val)
                                # 등록했으니 db_tickers 목록에도 즉시 추가 (아래 UI 로직 위해)
                                db_tickers.append(ticker) 

                for t_id, t_ticker, _, _, _ in db_trades:
                    if t_ticker not in real_wallet_tickers:
                        print(f"🧹 [Sync] {t_ticker} 지갑에 없음 -> DB 정리")
                        self.repo.close_zombie_trade(t_id)
                
                # C. UI용 카테고리 업데이트
                # 방금 동기화된 최신 DB 목록을 다시 가져와서 태그 달기
                final_open_tickers = self.repo.get_all_open_tickers()
                
                for t in final_open_tickers:
                    if t in targets_map:
                        if "(보유중)" not in targets_map[t]: 
                            targets_map[t] += " (보유중)"
                    else:
                        targets_map[t] = "내 보유 코인 (관리중)"
                        
            except Exception as e:
                print(f"Sync Error: {e}")

            # 3. Market Status 업데이트 (기존 동일)
            final_targets = list(targets_map.keys())
            missing_tickers = [t for t in final_targets if t not in self.shared_data]
            if missing_tickers:
                try:
                    prices = await asyncio.to_thread(pyupbit.get_current_price, missing_tickers)
                    if isinstance(prices, (float, int)): prices = {missing_tickers[0]: prices}
                    for t, p in prices.items():
                        self.shared_data[t] = {"current_price": float(p), "acc_trade_price_24h": 0}
                except: pass

            new_status = {}
            for ticker in final_targets:
                existing = self.market_status.get(ticker, {})
                cached = self.backtester.get_analysis(ticker)
                realtime_price = self.shared_data.get(ticker, {}).get('current_price', 0)
                
                new_status[ticker] = {
                    "price": realtime_price,
                    "score": cached.get('score', 0) if cached else existing.get('score', 0),
                    "reasons": existing.get('reasons', []),
                    "target": cached.get('target_price', 0) if cached else existing.get("target", 0),
                    "rsi": cached.get('rsi', 50) if cached else 50,
                    "mfi": cached.get('mfi', 50) if cached else 50,
                    "atr": cached.get('atr', 0) if cached else 0,
                    "stop_loss_price": cached.get('stop_loss_price', 0) if cached else 0,
                    "strategies": cached.get('strategies', {}) if cached else {},
                    "score_breakdown": cached.get('score_breakdown', []) if cached else [],
                    "category": targets_map.get(ticker, "관찰 종목")
                }
            self.target_coins = final_targets
            self.market_status = new_status
            
        except Exception as e: print(f"Target Update Error: {e}")

    async def get_smart_candles(self, ticker):
        now = time.time()
        if now - self.last_api_call_time.get(ticker, 0) > 60 or ticker not in self.cached_day_dfs:
            try:
                df_day = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="day", count=60)
                df_min = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="minute60", count=60)
                if df_day is not None:
                    self.cached_day_dfs[ticker] = df_day
                    self.cached_min_dfs[ticker] = df_min if df_min is not None else df_day
                    self.last_api_call_time[ticker] = now
            except: pass
        
        if ticker not in self.cached_day_dfs: return None, None, 0, False
        
        df_day = self.cached_day_dfs[ticker].copy()
        df_min = self.cached_min_dfs[ticker].copy()
        is_realtime = False
        current_price = 0
        
        if self.shared_data and ticker in self.shared_data:
            current_price = self.shared_data[ticker]['current_price']
            is_realtime = True
            
        if is_realtime and current_price > 0:
            df_day.iloc[-1, df_day.columns.get_loc('close')] = current_price
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
        
        now = time.time()
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
        # 1. DB에서 보유 중인 코인 목록 가져오기
        open_trades = self.repo.get_open_trades() 
        holdings_map = {t[1]: t[2] for t in open_trades} # {ticker: buy_price}

        total_krw = 0
        total_coin_val = 0
        
        try:
            all_balances = self.executor.get_all_balances()
            
            # 검색하기 편하게 딕셔너리로 변환: {'KRW-BTC': 0.1, 'KRW': 10000, ...}
            balance_dict = {}
            for b in all_balances:
                if b['currency'] == 'KRW':
                    total_krw = float(b['balance'])
                else:
                    ticker = f"KRW-{b['currency']}"
                    balance_dict[ticker] = float(b['balance']) + float(b['locked'])

            # 계산
            for ticker in holdings_map.keys():
                qty = balance_dict.get(ticker, 0) # API 호출 없이 메모리에서 조회
                current_price = self.shared_data.get(ticker, {}).get('current_price', 0)
                total_coin_val += (qty * current_price)
                
        except Exception as e:
            # 네트워크 에러가 나도 봇이 죽지 않게 예외 처리
            print(f"⚠️ [Frontend Update Error] {e}")

        # 리스트 구성
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