import sqlite3
import os

# 프로젝트 루트(backend) 기준 DB 파일 위치
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "coin_mate.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. 매매 기록 테이블 (🔥 sell_reason 추가됨!)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT,
        buy_price REAL,
        buy_amount REAL,
        buy_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sell_price REAL,
        sell_time TIMESTAMP,
        status TEXT DEFAULT 'open',
        profit_rate REAL,
        strategy_name TEXT,
        sell_reason TEXT 
    )
    ''')

    # 2. 분봉 데이터 저장 테이블
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS candles (
        ticker TEXT,
        time TEXT,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume REAL,
        UNIQUE(ticker, time)
    )
    ''')
    
    conn.commit()
    conn.close()
    print(f">>> 💾 DB 연결됨: {DB_PATH}")