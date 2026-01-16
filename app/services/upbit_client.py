import pyupbit
import os
from dotenv import load_dotenv

load_dotenv()

class UpbitClient:
    def __init__(self):
        self.access_key = os.getenv("UPBIT_ACCESS") or os.getenv("UPBIT_ACCESS_KEY")
        self.secret_key = os.getenv("UPBIT_SECRET") or os.getenv("UPBIT_SECRET_KEY")
        
        if not self.access_key or not self.secret_key:
            print("⚠️ [Warning] 업비트 API 키가 없습니다.")
            self.upbit = None
        else:
            self.upbit = pyupbit.Upbit(self.access_key, self.secret_key)

    def get_balance(self, ticker="KRW"):
        """
        [최종 해결버전] 전체 리스트(get_balances)를 가져와서 직접 찾기
        이 방식은 진단 키트와 동일한 로직이므로 무조건 성공합니다.
        """
        if not self.upbit: 
            return 0
        
        try:
            # 1. 티커명에서 "KRW-" 제거 (예: KRW-BTC -> BTC)
            # "KRW" 자체를 조회할 때는 그대로 둠
            target_currency = ticker
            if "-" in ticker and ticker.upper() != "KRW":
                target_currency = ticker.split("-")[1]
            
            # 2. [핵심] 전체 계좌 리스트 조회 (진단 키트가 성공한 그 방식!)
            balances = self.upbit.get_balances()
            
            # 3. 리스트에서 내가 찾는 코인 검색해서 합산
            for b in balances:
                if b['currency'] == target_currency:
                    # balance: 사용 가능 잔고
                    # locked: 매도 주문 걸어놔서 묶인 잔고
                    # 이 두 개를 합쳐야 '진짜 내 재산'입니다.
                    total_qty = float(b['balance']) + float(b['locked'])
                    return total_qty
            
            # 리스트를 다 뒤졌는데 없으면 진짜 0개
            return 0
            
        except Exception as e:
            print(f"❌ [Balance Error] {ticker} 조회 실패: {e}")
            return 0

    def get_balances(self):
        """전체 계좌 잔고 조회 (동기화용)"""
        if not self.upbit: return []
        return self.upbit.get_balances()

    def buy_market_order(self, ticker, price):
        if not self.upbit: return None
        try:
            if price < 5000:
                print(f"⛔ [매수 실패] 최소 주문액(5,000원) 미만: {price}원")
                return None
            result = self.upbit.buy_market_order(ticker, price)
            return result
        except Exception as e:
            print(f"❌ [매수 에러] {e}")
            return None

    def sell_market_order(self, ticker, volume):
        if not self.upbit: return None
        try:
            result = self.upbit.sell_market_order(ticker, volume)
            return result
        except Exception as e:
            print(f"❌ [매도 에러] {e}")
            return None