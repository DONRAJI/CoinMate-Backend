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
        # 0-1. 주간 모델 점검 (7일마다)
        await self._weekly_model_review()

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

                # 🔥 [분봉 모델 v3] 자동 재학습 비활성화.
                # 모델은 Colab(분봉 5분, forward-sim 라벨)에서 학습 후 수동 업로드한다.
                # 일봉 데이터로 서버에서 재학습하면 분봉 모델을 덮어쓰므로 금지.
                # (재활성화하려면 ML_AUTO_TRAIN=True + minute 데이터 수집 로직 필요)
                self.ohlcv_cache.clear()  # 메모리 해제만
        except Exception as e:
            print(f">>> ❌ [Scan Error] {e}")
        finally:
            self.is_running = False

    # 봇 매매 룰 (trade_manager와 동기화 — 분봉 모델 라벨 정의)
    EVAL_TP_PCT = 3.5
    EVAL_SL_PCT = -2.0
    EVAL_BUY_THRESHOLD = 0.42  # ML_MIN_PROB(bull)과 동기화

    @staticmethod
    def _simulate_tp_sl(entry_price, future_df, tp_pct=3.5, sl_pct=-2.0):
        """[분봉 모델 평가] 진입 후 future 캔들에서 익절(+tp)이 손절(sl)보다 먼저 닿는지.
        Returns: 1=익절 먼저(win), 0=손절 먼저 or 타임아웃(loss)
        """
        if future_df is None or len(future_df) == 0 or entry_price <= 0:
            return None
        tp_thresh = entry_price * (1 + tp_pct / 100)
        sl_thresh = entry_price * (1 + sl_pct / 100)
        for _, row in future_df.iterrows():
            hit_tp = row['high'] >= tp_thresh
            hit_sl = row['low'] <= sl_thresh
            if hit_tp and hit_sl:
                return 0  # 같은 캔들에 둘 다 → 보수적으로 손절
            if hit_tp:
                return 1
            if hit_sl:
                return 0
        return 0  # 24h 내 익절 못함 = loss

    async def _evaluate_yesterday_predictions(self):
        """[분봉 모델 v3] 전일 익절확률 예측 vs 실제 TP/SL 결과로 calibration 평가"""
        try:
            yesterday = (datetime.now(KST) - timedelta(days=1)).strftime('%Y-%m-%d')
            yesterday_file = os.path.join(CACHE_DIR, f"analysis_{yesterday}.json")
            accuracy_file = os.path.join(CACHE_DIR, "ml_accuracy_log.json")

            if not os.path.exists(yesterday_file):
                return None

            # 예측 시점 = 파일 생성시각 (이 시점 가격으로 진입했다고 가정)
            # pyupbit 인덱스는 tz-naive KST이므로 비교용도 naive로 맞춤
            entry_time = datetime.fromtimestamp(os.path.getmtime(yesterday_file), tz=KST).replace(tzinfo=None)
            eval_end = entry_time + timedelta(hours=24)

            with open(yesterday_file, 'r', encoding='utf-8') as f:
                yesterday_data = json.load(f)

            accuracy_log = []
            if os.path.exists(accuracy_file):
                with open(accuracy_file, 'r', encoding='utf-8') as f:
                    accuracy_log = json.load(f)
                if yesterday in {e['date'] for e in accuracy_log}:
                    return None

            ml_predictions = {
                t: d for t, d in yesterday_data.items() if d.get('ml_prob') is not None
            }
            try:
                active = set(await asyncio.to_thread(pyupbit.get_tickers, fiat="KRW") or [])
                if active:
                    ml_predictions = {t: d for t, d in ml_predictions.items() if t in active}
            except Exception:
                pass
            if not ml_predictions:
                return None

            # 각 코인: 진입가부터 24h forward 시뮬 (minute15, 96봉 = 24h)
            details = []
            to_str = eval_end.strftime('%Y-%m-%d %H:%M:%S')
            for ticker, pred in ml_predictions.items():
                entry_price = pred.get('current_price', 0)
                ml_prob = pred['ml_prob']
                if entry_price <= 0:
                    continue
                try:
                    fdf = await asyncio.to_thread(
                        pyupbit.get_ohlcv, ticker, interval="minute15", count=96, to=to_str
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    continue
                if fdf is None or len(fdf) < 4:
                    continue
                # 진입시각 이후 캔들만
                future = fdf[fdf.index >= entry_time]
                if len(future) < 2:
                    future = fdf  # fallback
                outcome = self._simulate_tp_sl(entry_price, future, self.EVAL_TP_PCT, self.EVAL_SL_PCT)
                if outcome is None:
                    continue
                details.append({
                    "ticker": ticker,
                    "ml_prob": round(ml_prob, 4),
                    "actual_win": outcome,  # 1=익절, 0=손절/타임아웃
                })

            if len(details) < 10:
                print(f">>> ⚠️ [ML Eval] 평가 표본 부족 ({len(details)})")
                return None

            n = len(details)
            predicted_avg = sum(d['ml_prob'] for d in details) / n  # 예측 평균 익절확률
            actual_win_rate = sum(d['actual_win'] for d in details) / n  # 실제 익절률
            calib_error = (predicted_avg - actual_win_rate) * 100  # +면 과대평가

            # 구간별 calibration (예측 확률 버킷별 실제 익절률)
            buckets = {}
            for lo in [0.0, 0.3, 0.4, 0.5, 0.6]:
                hi = lo + 0.1 if lo >= 0.3 else 0.3
                grp = [d for d in details if lo <= d['ml_prob'] < hi]
                if grp:
                    buckets[f"{lo:.1f}-{hi:.1f}"] = {
                        "n": len(grp),
                        "pred_avg": round(sum(d['ml_prob'] for d in grp) / len(grp), 3),
                        "actual_win_rate": round(sum(d['actual_win'] for d in grp) / len(grp), 3),
                    }

            # 매수기준(0.42) 이상 = 실제로 봇이 샀을 코인들의 익절률 (가장 중요)
            above = [d for d in details if d['ml_prob'] >= self.EVAL_BUY_THRESHOLD]
            above_win_rate = round(sum(d['actual_win'] for d in above) / len(above), 3) if above else None

            # 상위 10개(예측 높은 순) 실제 익절률
            top10 = sorted(details, key=lambda x: x['ml_prob'], reverse=True)[:10]
            top10_win_rate = round(sum(d['actual_win'] for d in top10) / len(top10), 3) if top10 else 0

            # 🔥 그날 매수 게이트(score+ML) 통과 후보 수 — 현행 vs 완화 비교 (거래 빈도 진단)
            # 일일 스캔 1회 스냅샷 기준 추정치(레짐/심야/쿨오프/슬롯 미적용 = 빈도 상한)
            def _gate_count(score_min, ml_min):
                return sum(
                    1 for d in yesterday_data.values()
                    if isinstance(d, dict) and d.get('ml_prob') is not None
                    and d.get('score', 0) >= score_min and d['ml_prob'] >= ml_min
                )
            buy_opportunities = {
                "current_bull": _gate_count(5.5, 0.42),      # 현행 bull 기준
                "current_neutral": _gate_count(6.5, 0.47),   # 현행 neutral 기준
                "breakeven_relaxed": _gate_count(5.0, 0.364),  # 완화안(손익분기)
            }

            entry = {
                "date": yesterday,
                "model": "minute5-v3",
                "n_evaluated": n,
                "predicted_avg_winrate": round(predicted_avg, 3),
                "actual_winrate": round(actual_win_rate, 3),
                "calibration_error_pp": round(calib_error, 1),  # +과대 / -과소
                "above_threshold_n": len(above),
                "above_threshold_winrate": above_win_rate,
                "top10_winrate": top10_win_rate,
                "buy_opportunities": buy_opportunities,
                "buckets": buckets,
            }
            accuracy_log.append(entry)
            with open(accuracy_file, 'w', encoding='utf-8') as f:
                json.dump(accuracy_log, f, ensure_ascii=False, indent=2)

            print(f">>> 📊 [ML Eval] {yesterday}: 예측 {predicted_avg*100:.1f}% vs 실제 {actual_win_rate*100:.1f}% "
                  f"(calib {calib_error:+.1f}%p, n={n})")
            print(f">>>    매수기준↑ {len(above)}개 실제익절 {(above_win_rate or 0)*100:.0f}% / Top10 {top10_win_rate*100:.0f}%")
            print(f">>>    매수후보(게이트통과): 현행bull {buy_opportunities['current_bull']} / 현행neutral {buy_opportunities['current_neutral']} / 완화 {buy_opportunities['breakeven_relaxed']}")

            # Discord 일일 평가 알림
            try:
                from app.services import notifier
                asyncio.create_task(notifier.notify_ml_eval_daily(entry))
            except Exception as e:
                print(f">>> ⚠️ [ML Eval alert] {e}")

            return entry

        except Exception as e:
            print(f">>> ⚠️ [ML Eval Error] {e}")
            return None

    async def _weekly_model_review(self):
        """[분봉 모델 v3] 7일마다 누적 평가를 집계해 모델 수정 방향 진단 + Discord 알림."""
        try:
            accuracy_file = os.path.join(CACHE_DIR, "ml_accuracy_log.json")
            review_file = os.path.join(CACHE_DIR, "ml_weekly_review.json")
            if not os.path.exists(accuracy_file):
                return
            with open(accuracy_file, encoding='utf-8') as f:
                log = json.load(f)

            # minute5-v3 평가만 (옛 일봉 평가 제외)
            recent = [e for e in log if e.get('model') == 'minute5-v3'][-7:]
            if len(recent) < 3:
                return  # 최소 3일치 누적돼야 점검 의미

            # 마지막 점검 후 7일 경과했는지 (중복 방지)
            last_review_date = None
            if os.path.exists(review_file):
                try:
                    with open(review_file, encoding='utf-8') as f:
                        prev = json.load(f)
                    last_review_date = prev[-1]['date'] if prev else None
                except Exception:
                    prev = []
            else:
                prev = []
            today = datetime.now(KST).strftime('%Y-%m-%d')
            if last_review_date:
                gap = (datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(last_review_date, '%Y-%m-%d')).days
                if gap < 7:
                    return

            # 집계
            n_days = len(recent)
            avg_pred = sum(e['predicted_avg_winrate'] for e in recent) / n_days
            avg_actual = sum(e['actual_winrate'] for e in recent) / n_days
            avg_calib = sum(e['calibration_error_pp'] for e in recent) / n_days
            above_rates = [e['above_threshold_winrate'] for e in recent if e.get('above_threshold_winrate') is not None]
            avg_above = sum(above_rates) / len(above_rates) if above_rates else None

            # 진단 + 권장 방향
            recs = []
            if avg_calib > 8:
                recs.append("⚠️ 과대평가 경향(+8%p↑) → calibration 재학습 또는 임계값 상향 권장")
            elif avg_calib < -8:
                recs.append("⚠️ 과소평가 경향(-8%p↓) → 좋은 진입을 놓치는 중, 임계값 하향 검토")
            else:
                recs.append("✅ calibration 양호 (±8%p 이내)")

            breakeven = 0.364
            if avg_above is not None:
                if avg_above < breakeven:
                    recs.append(f"🔴 매수기준↑ 실제익절률 {avg_above*100:.0f}% < 손익분기 36% → 임계값 상향(0.42→0.47) 또는 피처 보강 필요")
                elif avg_above < breakeven + 0.05:
                    recs.append(f"🟡 매수기준↑ 익절률 {avg_above*100:.0f}% 손익분기 근접 → 마진 부족, 주시")
                else:
                    recs.append(f"🟢 매수기준↑ 익절률 {avg_above*100:.0f}% — 기대수익 + 영역")

            # 구간 신뢰도 (예측 높은데 실제 낮은 구간 탐지)
            bucket_warn = []
            for e in recent:
                for k, v in (e.get('buckets') or {}).items():
                    if v['n'] >= 3 and v['pred_avg'] - v['actual_win_rate'] > 0.20:
                        bucket_warn.append(k)
            if bucket_warn:
                from collections import Counter
                common = Counter(bucket_warn).most_common(1)[0]
                if common[1] >= 2:
                    recs.append(f"📊 {common[0]} 구간이 반복적으로 과대평가됨 → 해당 확률대 신뢰 주의")

            review = {
                "date": today,
                "days_covered": n_days,
                "avg_predicted_winrate": round(avg_pred, 3),
                "avg_actual_winrate": round(avg_actual, 3),
                "avg_calibration_error_pp": round(avg_calib, 1),
                "avg_above_threshold_winrate": round(avg_above, 3) if avg_above is not None else None,
                "recommendations": recs,
            }
            prev.append(review)
            with open(review_file, 'w', encoding='utf-8') as f:
                json.dump(prev[-20:], f, ensure_ascii=False, indent=2)

            print(f">>> 📅 [주간점검] {n_days}일 — 예측 {avg_pred*100:.1f}% vs 실제 {avg_actual*100:.1f}% (calib {avg_calib:+.1f}%p)")
            for r in recs:
                print(f">>>    {r}")

            try:
                from app.services import notifier
                asyncio.create_task(notifier.notify_ml_eval_weekly(review))
            except Exception as e:
                print(f">>> ⚠️ [주간점검 alert] {e}")

        except Exception as e:
            print(f">>> ⚠️ [Weekly Review Error] {e}")

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
            
            # ML 확률 계산 (분봉 모델 v3 — minute5 데이터 사용)
            # ⚠️ 모델이 minute5로 학습됨 → 일봉(df) 아닌 minute5를 입력해야 함
            ml_prob = None
            if self.ml.is_trained:
                try:
                    df_5m = await asyncio.to_thread(
                        pyupbit.get_ohlcv, ticker, interval="minute5", count=360
                    )
                    if df_5m is not None and len(df_5m) >= 300:
                        from app.services.trade_manager import trade_manager as _tm
                        ml_prob = float(self.ml.predict(df_5m, _tm.get_btc_5m()))
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

    def get_ml_top_coins(self, top_n=10, vol_map=None, min_vol=0):
        """[분봉 모델 v3] 익절 확률 상위 코인 Top N.
        손익분기 36.4%(익절+3.5%/손절-2%) 이상만 표시 — 기대수익 + 인 코인.
        vol_map/min_vol 제공 시 거래대금 필터 적용 (잡코인 제외 — 실제 매수 후보와 일치).
        """
        candidates = []
        for c in self.results_cache.values():
            if c.get('ml_prob') is None or c['ml_prob'] < 0.364:
                continue
            # 거래대금 필터 (실제 매수 가능한 유동성 종목만)
            if vol_map is not None and min_vol > 0:
                if vol_map.get(c['ticker'], 0) < min_vol:
                    continue
            candidates.append(c)
        sorted_cands = sorted(candidates, key=lambda x: x['ml_prob'], reverse=True)
        return sorted_cands[:top_n]