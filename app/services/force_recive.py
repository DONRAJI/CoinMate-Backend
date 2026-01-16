import sqlite3
import os

# DB 파일 경로
DB_PATH = r"F:\CoinMate\backend\coin_mate.db"

def force_revive_coins():
    print(f"📂 DB 경로: {DB_PATH}")
    
    if not os.path.exists(DB_PATH):
        print("❌ 파일이 없습니다.")
        return

    # 되살릴 코인 목록 (보유 중인 4개)
    target_tickers = ['KRW-XTZ', 'KRW-STORJ', 'KRW-TRX', 'KRW-BOUNTY']
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        print(f"🚑 다음 4개 코인의 마지막 기록을 강제로 되살립니다: {target_tickers}")
        
        revived_count = 0
        
        for ticker in target_tickers:
            # 1. 해당 코인의 가장 최근 기록(ID) 찾기
            cursor.execute("SELECT id FROM trades WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,))
            row = cursor.fetchone()
            
            if row:
                trade_id = row[0]
                # 2. 해당 기록을 'open' 상태로 초기화 (매도 정보 삭제)
                cursor.execute("""
                    UPDATE trades 
                    SET status = 'open', 
                        sell_price = NULL, 
                        sell_time = NULL, 
                        profit_rate = NULL 
                    WHERE id = ?
                """, (trade_id,))
                
                print(f"  ✅ [ID: {trade_id}] {ticker} -> 부활 성공! (open 상태로 복구됨)")
                revived_count += 1
            else:
                print(f"  ⚠️ {ticker} -> DB에서 기록을 찾을 수 없습니다.")

        conn.commit()
        print(f"\n🎉 총 {revived_count}개의 코인을 심폐소생했습니다.")

        # 3. 결과 확인
        print("\n📊 [현재 보유 중(open)인 코인 목록]")
        cursor.execute("SELECT ticker, status, buy_price FROM trades WHERE status='open'")
        rows = cursor.fetchall()
        if not rows:
            print("  (목록이 비어있습니다)")
        else:
            for r in rows:
                print(f"  - {r[0]}: {r[1]} (매수가: {r[2]:,.0f}원)")
            
        conn.close()
        
    except Exception as e:
        print(f"❌ 오류 발생: {e}")

if __name__ == "__main__":
    force_revive_coins()