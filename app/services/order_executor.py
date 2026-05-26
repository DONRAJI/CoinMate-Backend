import asyncio
import time
from app.services.upbit_client import UpbitClient

class OrderExecutor:
    def __init__(self, repository):
        self.upbit = UpbitClient()
        self.repo = repository

    def get_krw_balance(self):
        return self.upbit.get_balance("KRW")

    def get_coin_balance(self, ticker):
        return self.upbit.get_balance(ticker)

    def get_all_balances(self):
        return self.upbit.get_balances()

    def _get_avg_buy_price(self, ticker):
        """체결 후 실제 평균 매수가 조회"""
        try:
            currency = ticker.split("-")[1] if "-" in ticker else ticker
            balances = self.upbit.get_balances()
            for b in balances:
                if b['currency'] == currency:
                    return float(b['avg_buy_price'])
        except Exception as e:
            print(f"⚠️ [Avg Price] 조회 실패: {e}")
        return 0

    async def try_buy(self, ticker, price, budget, strategy_name="Ensemble"):
        print(f"🛒 [BUY Attempt] {ticker} ({budget:,.0f}원) 주문 시도...")
        buy_res = await asyncio.to_thread(self.upbit.buy_market_order, ticker, budget)

        if buy_res:
            # 체결 후 실제 평균 매수가 조회 (슬리피지 반영)
            await asyncio.sleep(0.3)  # 체결 대기
            real_price = await asyncio.to_thread(self._get_avg_buy_price, ticker)
            if real_price <= 0:
                real_price = price  # fallback

            print(f"✅ [BUY Success] {ticker} 체결! 시그널:{price:,.0f} → 실제:{real_price:,.0f}")
            self.repo.log_buy(ticker, real_price, budget, strategy_name)
            return True
        else:
            print(f"❌ [BUY Fail] {ticker} API 주문 실패")
            return False

    async def try_sell(self, trade_id, ticker, current_price, reason):
        """매도 시도 -> 성공 시 DB 정리까지"""
        vol = self.get_coin_balance(ticker)

        if vol <= 0:
            print(f"👻 [Zombie] {ticker} 잔고 부족(0). DB만 정리합니다.")
            self.repo.close_zombie_trade(trade_id)
            return True

        print(f"👋 [SELL Attempt] {ticker} ({reason})")
        sell_res = await asyncio.to_thread(self.upbit.sell_market_order, ticker, vol)

        if sell_res:
            # 체결 후 실제 매도 가격 계산
            await asyncio.sleep(0.3)  # 체결 대기
            real_sell_price = current_price  # 기본값

            try:
                # 주문 UUID로 체결 내역 조회
                if isinstance(sell_res, dict) and 'uuid' in sell_res:
                    uuid = sell_res['uuid']
                    await asyncio.sleep(0.5)  # 체결 완료 대기
                    order_info = await asyncio.to_thread(
                        self.upbit.upbit.get_order, uuid
                    )
                    if order_info and 'trades' in order_info:
                        trades = order_info['trades']
                        if trades:
                            total_funds = sum(float(t['funds']) for t in trades)
                            total_vol = sum(float(t['volume']) for t in trades)
                            if total_vol > 0:
                                real_sell_price = total_funds / total_vol
                                print(f"💰 [SELL Real] {ticker} 시그널:{current_price:,.0f} → 실제:{real_sell_price:,.0f}")
            except Exception as e:
                print(f"⚠️ [SELL Price] 체결가 조회 실패, 시그널가 사용: {e}")

            self.repo.log_sell(trade_id, real_sell_price, reason)
            return True
        else:
            print(f"❌ [SELL Fail] {ticker} API 주문 실패")
            return False
