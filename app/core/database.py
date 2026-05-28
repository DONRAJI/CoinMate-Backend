import sqlite3
import os

# 프로젝트 루트(backend) 기준 DB 파일 위치
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "coin_mate.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 🔥 [P0] WAL 모드 활성화 — 매매루프 쓰기 + API 읽기 동시 접근 시 lock 방지
    # WAL은 DB 파일에 한 번 설정하면 영속됨 (재설정해도 무해)
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")  # WAL에서 권장 (성능/안전 균형)

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
    print(f">>> DB connected: {DB_PATH}")