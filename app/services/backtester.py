import asyncio
import pyupbit
import pandas as pd
import json
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
from app.services.strategy import Strategy
from app.services.ml_predictor import MLPredictor

# 캐시 디렉토리 설정
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cache")
if not os.path.exists(CACHE_DIR): os.makedirs(CACHE_DIR)

class Backtester:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Backtester, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self.initialized: return
        self.fee = 0.0005  # 업비트 수수료 (0.05%)
        self.strategy = Strategy()
        self.ml = MLPredictor()
        self.results_cache = {}
        self.ohlcv_cache = {}  # ML 학습용 OHLCV 저장
        self.is_running = False
        self.initialized = True
        self.semaphore = asyncio.Semaphore(10) 

    def get_today_filename(self):
        return os.path.join(CACHE_DIR, f"analysis_{datetime.now(KST).strftime('%Y-%m-%d')}.json")

    def get_report_filename(self):
        return os.path.join(CACHE_DIR, f"report_{datetime.now(KST).strftime('%Y-%m-%d')}.txt")

    async def run_daily_scan(self):
        if self.is_running:
            print(">>> ⚠️ 이미 스캔이 진행 중입니다.")
            return

        cache_file = self.get_today_filename()
        need_scan = True

        # 0. 전일 ML 예측 정확도 평가
        await self._evaluate_yesterday_predictions()

        # 1. 캐시 파일 확인
        if os.path.exists(cache_file):
            print(f">>> 📂 [Cache] 로드 중: {os.path.basename(cache_file)}")
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data and isinstance(data, dict) and len(data) > 0:
                    self.results_cache = data
                    print(f">>> ✅ [Cache] 로드 성공! ({len(self.results_cache)}개 코인)")

                    if not os.path.exists(self.get_report_filename()):
                        self._save_report_txt()
                    need_scan = False
                else:
                    print(f">>> ⚠️ [Cache] 비어있음. 재분석.")
            except Exception as e:
                print(f">>> ⚠️ [Cache] 오류 ({e}). 재분석.")
        else:
            print(f">>> 🆕 [Cache] 파일 없음. 신규 분석 시작.")

        if not need_scan: return

        # 2. 풀 스캔 시작
        self.is_running = True
        print(f">>> 🔎 [Full Scan] 전 종목 정밀 분석 시작... (약 1~2분 소요)")

        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
            tasks = [self._analyze_one_safe(ticker) for ticker in tickers]
            await asyncio.gather(*tasks)

            if self.results_cache:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self.results_cache, f, ensure_ascii=False, indent=4)

                self._save_report_txt()
                print(f">>> 💾 [Save] 저장 완료 ({len(self.results_cache)}개)")

                # ML 모델 학습 (수집한 OHLCV 활용)
                if self.ohlcv_cache:
                    await asyncio.to_thread(self.ml.train, self.ohlcv_cache)
                    self.ohlcv_cache.clear()  # 메모리 해제
        except Exception as e:
            print(f">>> ❌ [Scan Error] {e}")
        finally:
            self.is_running = False

    async def _evaluate_yesterday_predictions(self):
        """전일 ML 예측과 실제 가격 변동을 비교하여 정확도 기록"""
        try:
            yesterday = (datetime.now(KST) - timedelta(days=1)).strftime('%Y-%m-%d')
            yesterday_file = os.path.join(CACHE_DIR, f"analysis_{yesterday}.json")
            accuracy_file = os.path.join(CACHE_DIR, "ml_accuracy_log.json")

            if not os.path.exists(yesterday_file):
                return

            with open(yesterday_file, 'r', encoding='utf-8') as f:
                yesterday_data = json.load(f)

            # 이미 평가했는지 확인
            accuracy_log = []
            if os.path.exists(accuracy_file):
                with open(accuracy_file, 'r', encoding='utf-8') as f:
                    accuracy_log = json.load(f)
                evaluated_dates = {entry['date'] for entry in accuracy_log}
                if yesterday in evaluated_dates:
                    return

            # ML 확률이 있는 코인만 추출
            ml_predictions = {
                t: d for t, d in yesterday_data.items()
                if d.get('ml_prob') is not None
            }
            if not ml_predictions:
                return

            # 현재 가격 조회
            tickers = list(ml_predictions.keys())
            current_prices = await asyncio.to_thread(pyupbit.get_current_price, tickers)
            if current_prices is None:
                return
            if isinstance(current_prices, (float, int)):
                current_prices = {tickers[0]: current_prices}

            # 예측 vs 실제 비교
            correct = 0
            total = 0
            details = []
            for ticker, pred in ml_predictions.items():
                curr_price = current_prices.get(ticker)
                if curr_price is None:
                    continue
                pred_price = pred.get('current_price', 0)
                if pred_price <= 0:
                    continue

                actual_change_pct = ((curr_price - pred_price) / pred_price) * 100
                ml_prob = pred['ml_prob']
                predicted_up = ml_prob >= 0.5
                actual_up = actual_change_pct >= 1.0  # 1%+ 상승이 label 기준

                if predicted_up == actual_up:
                    correct += 1
                total += 1

                details.append({
                    "ticker": ticker,
                    "ml_prob": round(ml_prob, 4),
                    "predicted_up": predicted_up,
                    "actual_change_pct": round(actual_change_pct, 2),
                    "actual_up": actual_up,
                    "correct": predicted_up == actual_up,
                })

            if total == 0:
                return

            accuracy = round(correct / total * 100, 1)

            # 상위 확률 코인의 실제 성과
            top_10 = sorted(details, key=lambda x: x['ml_prob'], reverse=True)[:10]
            top_10_correct = sum(1 for d in top_10 if d['correct'])
            top_10_avg_change = round(sum(d['actual_change_pct'] for d in top_10) / len(top_10), 2) if top_10 else 0

            entry = {
                "date": yesterday,
                "total_evaluated": total,
                "correct": correct,
                "accuracy_pct": accuracy,
                "top10_accuracy_pct": round(top_10_correct / len(top_10) * 100, 1) if top_10 else 0,
                "top10_avg_change_pct": top_10_avg_change,
                "top10_details": top_10,
            }

            accuracy_log.append(entry)
            with open(accuracy_file, 'w', encoding='utf-8') as f:
                json.dump(accuracy_log, f, ensure_ascii=False, indent=2)

            print(f">>> 📊 [ML Accuracy] {yesterday}: {accuracy}% ({correct}/{total})")
            print(f">>>    Top10 정확도: {entry['top10_accuracy_pct']}%, 평균 변동: {top_10_avg_change}%")

        except Exception as e:
            print(f">>> ⚠️ [ML Accuracy Error] {e}")

    def _save_report_txt(self):
        try:
            report_file = self.get_report_filename()
            items = list(self.results_cache.values())
            
            sorted_items = sorted(
                items, 
                key=lambda x: (x['score'], x['win_rate'], x['total_yield']), 
                reverse=True
            )
            
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(f"=== CoinMate AI Analysis Report ===\n")
                f.write(f"Date: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Coins: {len(sorted_items)}\n")
                f.write("="*105 + "\n")
                f.write(f"{'Rank':<4} | {'Ticker':<10} | {'Score':<5} | {'WinRate':<7} | {'Yield':<8} | {'MDD':<6} | {'RSI':<5} | {'Price':<10}\n")
                f.write("-" * 105 + "\n")
                
                for rank, item in enumerate(sorted_items, 1):
                    f.write(
                        f"{rank:<4} | "
                        f"{item['ticker']:<10} | "
                        f"{item['score']:<5.1f} | "
                        f"{item['win_rate']:<6.1f}% | "
                        f"{item['total_yield']:<7.1f}% | "
                        f"{item['mdd']:<6.1f} | "
                        f"{item['rsi']:<5.0f} | "
                        f"{item['current_price']:<10,.0f}\n"
                    )
            print(f">>> 📄 [Report] 리포트 생성됨")
        except Exception as e:
            print(f">>> ⚠️ [Report Error] {e}")

    async def _analyze_one_safe(self, ticker):
        async with self.semaphore:
            await self._analyze_one(ticker)
            await asyncio.sleep(0.1) 

    async def _analyze_one(self, ticker):
        try:
            df = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="day", count=200)
            if df is None or len(df) < 50: return

            # ML 학습용 OHLCV 저장
            self.ohlcv_cache[ticker] = df.copy()

            df_for_backtest = df.iloc[:-1].copy() 

            result = await asyncio.to_thread(self._simulate, df_for_backtest)
            
            strategy_res = self.strategy.get_ensemble_signal(df, df)
            
            if not strategy_res: return

            strategies = {k: int(v) for k, v in strategy_res['strategies'].items()}
            
            # ML 확률 계산 (학습된 모델이 있을 때)
            ml_prob = None
            if self.ml.is_trained:
                try:
                    ml_prob = float(self.ml.predict(df))
                except Exception:
                    ml_prob = None

            self.results_cache[ticker] = {
                "ticker": ticker,
                "win_rate": float(result['win_rate']),
                "total_yield": float(result['total_return']),
                "mdd": float(result['mdd']),
                "score": float(strategy_res['score']),
                "should_buy": bool(strategy_res['should_buy']),
                "current_price": float(df.iloc[-1]['close']),
                "target_price": float(strategy_res.get('target_price', 0)),
                "stop_loss_price": float(strategy_res.get('stop_loss_price', 0)),
                "atr": float(strategy_res.get('atr', 0)),
                "rsi": float(strategy_res['rsi']),
                "mfi": float(strategy_res['mfi']),
                "strategies": strategies,
                "score_breakdown": strategy_res.get("score_breakdown", []),
                "regime": strategy_res.get("regime", "normal"),
                "adx": strategy_res.get("adx", 0),
                "ml_prob": ml_prob,
            }
        except Exception:
            pass

    def _simulate(self, df):
        """TradeManager와 동일한 조건으로 백테스트"""
        try:
            capital = 1_000_000
            balance = capital
            shares = 0
            avg_buy = 0
            high_since_buy = 0
            trade_count = 0
            win_count = 0
            max_balance = capital
            mdd = 0

            TRAILING_ACT = 1.5
            TRAILING_DIST = 1.2

            days_to_test = min(90, len(df) - 20)
            start_idx = len(df) - days_to_test
            days_held = 0

            for i in range(start_idx, len(df) - 1):
                past = df.iloc[:i+1]
                res = self.strategy.get_ensemble_signal(past, past)
                if not res: continue

                price = float(df.iloc[i]['close'])
                next_open = float(df.iloc[i+1]['open'])
                rsi = float(res['rsi'])
                mfi = float(res.get('mfi', 50))
                score = float(res['score'])
                is_strong_trend = res['strategies'].get('adx', 0) == 1
                regime = res.get('regime', 'normal')
                is_sideways = regime == 'sideways'

                stop_loss = -1.5 if is_sideways else -2.0
                profit_target = 2.0 if is_sideways else 3.5

                # 매도 판단
                if shares > 0:
                    days_held += 1
                    profit = ((price - avg_buy) / avg_buy) * 100
                    high_since_buy = max(high_since_buy, price)
                    dd_from_high = ((high_since_buy - price) / high_since_buy) * 100

                    sell = False
                    if profit <= stop_loss:
                        sell = True
                    elif is_strong_trend and profit >= TRAILING_ACT and dd_from_high >= TRAILING_DIST:
                        sell = True
                    elif not is_strong_trend and profit >= profit_target:
                        sell = True
                    elif is_sideways and days_held >= 2 and profit < 1.0:
                        sell = True
                    elif profit > 2.0 and (rsi >= 80 or mfi >= 85):
                        sell = True
                    elif score < 3.0 and profit < -0.5:
                        sell = True

                    if sell:
                        sell_val = shares * next_open * (1 - self.fee)
                        pnl = ((next_open - avg_buy) / avg_buy) * 100
                        if pnl > 0: win_count += 1
                        balance = sell_val
                        shares = 0
                        days_held = 0
                        trade_count += 1
                        max_balance = max(max_balance, balance)
                        dd = (max_balance - balance) / max_balance * 100
                        mdd = max(mdd, dd)

                # 매수 판단
                overheated = rsi >= 75 or mfi >= 85 or (rsi >= 65 and mfi < 35)
                # 급등 필터 (직전 3봉 +5% 이상)
                recent_surge = False
                if i >= 3:
                    p3_ago = float(df.iloc[i-3]['close'])
                    if p3_ago > 0 and ((price - p3_ago) / p3_ago) * 100 >= 5.0:
                        recent_surge = True
                if score >= self.strategy.BUY_THRESHOLD and not overheated and not recent_surge and shares == 0:
                    shares = (balance * (1 - self.fee)) / next_open
                    balance = 0
                    avg_buy = next_open
                    high_since_buy = next_open
                    days_held = 0

            final = balance if balance > 0 else shares * float(df.iloc[-1]['close'])
            return {
                "win_rate": round((win_count / trade_count * 100) if trade_count > 0 else 0, 1),
                "total_return": round(((final / capital) - 1) * 100, 1),
                "mdd": round(mdd, 1)
            }
        except Exception:
            return {"win_rate": 0, "total_return": 0, "mdd": 0}

    def get_analysis(self, ticker):
        return self.results_cache.get(ticker, None)

    def get_best_opportunities(self, top_n=5):
        candidates = list(self.results_cache.values())
        candidates = [c for c in candidates if c['score'] > 0]

        sorted_cands = sorted(
            candidates,
            key=lambda x: (x['score'], x['win_rate'], x['total_yield']),
            reverse=True
        )
        return [c['ticker'] for c in sorted_cands[:top_n]]

    def get_ml_top_coins(self, top_n=10):
        """ML 상승 확률이 높은 코인 Top N 반환 (일일 스캔 결과 기반)"""
        candidates = [
            c for c in self.results_cache.values()
            if c.get('ml_prob') is not None and c['ml_prob'] > 0.5
        ]
        sorted_cands = sorted(candidates, key=lambda x: x['ml_prob'], reverse=True)
        return sorted_cands[:top_n]