import pyupbit
import time
import sqlite3
import pandas as pd
from app.core.database import DB_PATH, init_db

def fetch_and_save_all_coins(days=200):
    """
    [속도 개선판] 업비트 전 종목 데이터를 긁어와서 DB에 저장합니다.
    - 기존: 1행마다 DB 연결 (느림)
    - 개선: 코인 1개당 1번 연결 & 대량 삽입 (빠름)
    """
    tickers = pyupbit.get_tickers(fiat="KRW")
    total = len(tickers)
    print(f">>> 📥 데이터 적재 시작: 총 {total}개 코인 ({days}일치)")
    
    # DB 초기화 및 연결 (한 번만 연결)
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    cursor = conn.cursor()
    
    try:
        for i, ticker in enumerate(tickers, 1):
            try:
                print(f"[{i}/{total}] {ticker} 다운로드 중...", end="\r")
                
                # 1. API로 데이터 가져오기
                df = pyupbit.get_ohlcv(ticker, interval="day", count=days)
                
                if df is None or df.empty:
                    continue
                
                # 2. 대량 삽입을 위한 데이터 가공 (List of Tuples)
                # DataFrame의 인덱스(날짜)를 컬럼으로 뺌
                df = df.reset_index() 
                
                # 저장할 데이터 리스트 생성
                data_list = []
                for _, row in df.iterrows():
                    data_list.append((
                        ticker,
                        str(row['index']), # timestamp (날짜)
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        float(row['volume'])
                    ))
                
                # 3. 한 방에 집어넣기 (executemany) - 여기가 핵심!
                cursor.executemany('''
                    INSERT OR IGNORE INTO candles (ticker, time, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', data_list)
                
                conn.commit() # 코인 1개 다 넣고 커밋
                
                # API 제한 방지 (아주 짧게 휴식)
                time.sleep(0.05)
                
            except Exception as e:
                print(f"\n[Error] {ticker}: {e}")
                
    finally:
        conn.close() # 작업 다 끝나면 문 닫기

    print(f"\n>>> ✅ 데이터 적재 완료! 이제 백테스팅을 돌려보세요.")

if __name__ == "__main__":
    fetch_and_save_all_coins()