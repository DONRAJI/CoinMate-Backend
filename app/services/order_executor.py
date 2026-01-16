import asyncio
from app.services.upbit_client import UpbitClient

class OrderExecutor:
    def __init__(self, repository):
        self.upbit = UpbitClient()
        self.repo = repository # 경리를 데리고 다님

    def get_krw_balance(self):
        return self.upbit.get_balance("KRW")

    def get_coin_balance(self, ticker):
        return self.upbit.get_balance(ticker)
    
    def get_all_balances(self):
        return self.upbit.get_balances()

    async def try_buy(self, ticker, price, budget, strategy_name="Ensemble"):
        print(f"🛒 [BUY Attempt] {ticker} ({budget:,.0f}원) 주문 시도...")
        buy_res = await asyncio.to_thread(self.upbit.buy_market_order, ticker, budget)
        
        if buy_res:
            print(f"✅ [BUY Success] {ticker} 체결 완료! DB에 기록합니다.")
            # 🔥 여기서 전략 이름을 같이 넘겨줍니다!
            self.repo.log_buy(ticker, price, budget, strategy_name)
            return True
        else:
            print(f"❌ [BUY Fail] {ticker} API 주문 실패")
            return False

    async def try_sell(self, trade_id, ticker, current_price, reason):
        """매도 시도 -> 성공 시 DB 정리까지"""
        vol = self.get_coin_balance(ticker)
        
        # 잔고 없으면 좀비 처리 (이미 앱에서 팔았거나 오류)
        if vol <= 0:
            print(f"👻 [Zombie] {ticker} 잔고 부족(0). DB만 정리합니다.")
            self.repo.close_zombie_trade(trade_id)
            return True

        # 1. 주문 넣기
        print(f"👋 [SELL Attempt] {ticker} ({reason})")
        sell_res = await asyncio.to_thread(self.upbit.sell_market_order, ticker, vol)
        
        if sell_res:
            # 2. 성공하면 DB 업데이트
            print(f"✅ [SELL Success] {ticker} 매도 완료! DB를 업데이트합니다.")
            self.repo.log_sell(trade_id, current_price, reason)
            return True
        else:
            print(f"❌ [SELL Fail] {ticker} API 주문 실패")
            return False