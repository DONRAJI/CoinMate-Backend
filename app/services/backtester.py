import asyncio
import pyupbit
import pandas as pd
import json
import os
from datetime import datetime, timedelta
from app.services.strategy import Strategy

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
        self.results_cache = {}
        self.is_running = False
        self.initialized = True
        self.semaphore = asyncio.Semaphore(10) # 동시 요청 제한 (API 과부하 방지)

    def get_today_filename(self):
        return os.path.join(CACHE_DIR, f"analysis_{datetime.now().strftime('%Y-%m-%d')}.json")

    def get_report_filename(self):
        return os.path.join(CACHE_DIR, f"report_{datetime.now().strftime('%Y-%m-%d')}.txt")

    async def run_daily_scan(self): 
        if self.is_running: 
            print(">>> ⚠️ 이미 스캔이 진행 중입니다.")
            return
        
        cache_file = self.get_today_filename()
        need_scan = True
        
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
            # 비동기 작업 생성 및 실행
            tasks = [self._analyze_one_safe(ticker) for ticker in tickers]
            await asyncio.gather(*tasks)

            if self.results_cache:
                # 결과 저장
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self.results_cache, f, ensure_ascii=False, indent=4)
                
                self._save_report_txt()
                print(f">>> 💾 [Save] 저장 완료 ({len(self.results_cache)}개)")
        except Exception as e:
            print(f">>> ❌ [Scan Error] {e}")
        finally:
            self.is_running = False

    def _save_report_txt(self):
        """분석 결과를 리포트로 저장"""
        try:
            report_file = self.get_report_filename()
            items = list(self.results_cache.values())
            
            # 정렬 기준: 점수(높은순) > 승률(높은순) > 수익률(높은순)
            sorted_items = sorted(
                items, 
                key=lambda x: (x['score'], x['win_rate'], x['total_yield']), 
                reverse=True
            )
            
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(f"=== CoinMate AI Analysis Report ===\n")
                f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
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
            # API 호출 제한 준수 (업비트 초당 요청 제한 고려)
            await asyncio.sleep(0.1) 

    async def _analyze_one(self, ticker):
        try:
            # 200일치 데이터 가져오기
            # [수정 1] to_thread 사용: I/O 블로킹 방지
            df = await asyncio.to_thread(pyupbit.get_ohlcv, ticker, interval="day", count=200)
            
            if df is None or len(df) < 50: return

            # [수정 2] 백테스팅용 데이터 분리
            # 마지막 행(오늘, 진행중인 캔들)은 제외하고 순수 과거 데이터만 사용
            df_for_backtest = df.iloc[:-1].copy() 

            # 1. 과거 데이터 시뮬레이션 (CPU 연산이므로 별도 스레드 처리)
            # [수정 3] to_thread 사용: for문이 메인 루프를 멈추는 것 방지
            result = await asyncio.to_thread(self._simulate, df_for_backtest)
            
            # 2. 현재 시점(오늘 포함) 전략 분석 -> 실시간 매매 판단용
            # 여기서는 최신 데이터(df)를 그대로 씁니다.
            strategy_res = self.strategy.get_ensemble_signal(df, df)
            
            if not strategy_res: return

            strategies = {k: int(v) for k, v in strategy_res['strategies'].items()}
            
            self.results_cache[ticker] = {
                "ticker": ticker,
                "win_rate": float(result['win_rate']),
                "total_yield": float(result['total_return']),
                "mdd": float(result['mdd']),
                "score": float(strategy_res['score']),
                "should_buy": bool(strategy_res['should_buy']),
                "current_price": float(df.iloc[-1]['close']),
                "rsi": float(strategy_res['rsi']),
                "mfi": float(strategy_res['mfi']),
                "strategies": strategies
            }
        except Exception:
            # 개별 코인 실패는 무시하고 계속 진행
            pass

    def _simulate(self, df):
        """
        과거 90일 데이터로 백테스팅
        [핵심 수정 사항] 
        - 미래 참조 편향 제거: i일의 신호로 i+1일 시가(Open)에 매수
        """
        try:
            capital = 1000000
            balance = capital
            shares = 0
            avg_buy_price = 0
            trade_count = 0
            win_count = 0
            max_balance = capital
            mdd = 0
            
            days_to_test = min(90, len(df) - 20)
            start_idx = len(df) - days_to_test
            
            # 마지막 날은 '내일'이 없으므로 len(df)-1 까지만 루프
            for i in range(start_idx, len(df) - 1):
                # i 시점까지의 데이터로 신호 생성 (오늘 장 마감 기준)
                past = df.iloc[:i+1]
                res = self.strategy.get_ensemble_signal(past, past)
                
                if not res: continue
                
                # [수정] 매매 체결 가격: 다음 날(i+1) 시가(Open)
                # 이유: 오늘 종가 확인 후 실제 매수는 다음 날 아침에 가능하기 때문
                next_day_open = float(df.iloc[i+1]['open'])
                
                # 매수 신호
                if res['should_buy'] and shares == 0:
                    shares = (balance * (1 - self.fee)) / next_day_open
                    balance = 0
                    avg_buy_price = next_day_open
                
                # 매도 신호 (보유 중일 때만)
                elif not res['should_buy'] and shares > 0:
                    sell_val = shares * next_day_open * (1 - self.fee)
                    
                    if sell_val > (shares * avg_buy_price): 
                        win_count += 1
                    
                    balance = sell_val
                    shares = 0
                    trade_count += 1
                    
                    # MDD 갱신
                    max_balance = max(max_balance, balance)
                    dd = (max_balance - balance) / max_balance * 100
                    mdd = max(mdd, dd)

            # 최종 평가 (보유 중이면 마지막 날 종가로 평가)
            final_asset = balance if balance > 0 else shares * df.iloc[-1]['close']
            
            return {
                "win_rate": round((win_count / trade_count * 100) if trade_count > 0 else 0, 1),
                "total_return": round(((final_asset / capital) - 1) * 100, 1),
                "mdd": round(mdd, 1)
            }
        except Exception: 
            return {"win_rate": 0, "total_return": 0, "mdd": 0}

    def get_analysis(self, ticker):
        return self.results_cache.get(ticker, None)

    def get_best_opportunities(self, top_n=5):
        candidates = list(self.results_cache.values())
        # 점수가 0점 이상인 것만 필터링
        candidates = [c for c in candidates if c['score'] > 0]
        
        sorted_cands = sorted(
            candidates, 
            key=lambda x: (x['score'], x['win_rate'], x['total_yield']), 
            reverse=True
        )
        return [c['ticker'] for c in sorted_cands[:top_n]]