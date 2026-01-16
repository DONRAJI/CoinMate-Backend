import pyupbit
import os
from dotenv import load_dotenv

load_dotenv()

# .env에서 키 가져오기
access = os.getenv("UPBIT_ACCESS") or os.getenv("UPBIT_ACCESS_KEY")
secret = os.getenv("UPBIT_SECRET") or os.getenv("UPBIT_SECRET_KEY")

print("="*60)
print("🕵️‍♂️ [업비트 계좌 정밀 검문] API가 보는 내 지갑 상태")
print("="*60)

if not access or not secret:
    print("❌ API 키가 없습니다. .env 파일을 확인해주세요.")
    exit()

try:
    upbit = pyupbit.Upbit(access, secret)
    
    # 1. 내 계좌의 모든 자산(현금+코인)을 있는 그대로 가져옵니다.
    balances = upbit.get_balances()
    
    # 2. 결과 분석
    if isinstance(balances, dict) and 'error' in balances:
        print("❌ [접속 실패] API 키 문제 또는 IP 차단입니다.")
        print(f"   에러 메시지: {balances.get('error')}")
    else:
        print(f"✅ 접속 성공! 발견된 자산: 총 {len(balances)}개\n")
        
        has_coin = False
        for b in balances:
            currency = b.get('currency')
            balance = float(b.get('balance', 0))      # 주문 가능
            locked = float(b.get('locked', 0))        # 주문 대기(묶임)
            avg_price = float(b.get('avg_buy_price', 0))
            
            total = balance + locked
            
            if currency == "KRW":
                print(f"💰 [현금] 보유: {total:,.0f} 원")
            else:
                est_value = total * avg_price
                print(f"🪙 [코인] {currency:5s} | 총 보유: {total:,.4f}개 (대기: {locked}) | 평가금(약): {est_value:,.0f}원")
                has_coin = True
        
        if not has_coin:
            print("\n⚠️ [진단 결과] API는 '보유 코인이 하나도 없다'고 합니다.")
            print("   -> 어플에는 보이는데 여기 안 뜬다면, '다른 계정의 API 키'입니다.")
            print("   -> 키를 새로 발급받으시되, 반드시 코인이 있는 계정으로 로그인했는지 확인하세요.")

except Exception as e:
    print(f"❌ [오류] {e}")

print("="*60)