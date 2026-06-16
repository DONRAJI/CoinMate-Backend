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
        sell_reason TEXT,
        buy_score REAL,
        buy_ml_prob REAL,
        buy_regime TEXT,
        buy_rsi REAL
    )
    ''')

    # 🔥 [P1/Phase 1B] 마이그레이션: 매수 시점 컨텍스트 컬럼 추가 (idempotent)
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
    for col, coltype in [
        ("buy_score", "REAL"), ("buy_ml_prob", "REAL"),
        ("buy_regime", "TEXT"), ("buy_rsi", "REAL"),
        # Phase 1B: 매수 시점 뉴스 컨텍스트 (참고/분석용, 매매에는 영향 없음)
        ("buy_news_sentiment", "REAL"),
        ("buy_news_critical_count", "INTEGER"),
    ]:
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {coltype}")
            print(f">>> [Migration] trades.{col} 컬럼 추가")

    # 🔥 [섀도우 모드] 페이퍼(가상) 거래 테이블 — 실거래와 동일 스키마, 별도 추적
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS paper_trades (
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
        sell_reason TEXT,
        buy_score REAL,
        buy_ml_prob REAL,
        buy_regime TEXT,
        buy_rsi REAL,
        buy_news_sentiment REAL,
        buy_news_critical_count INTEGER,
        buy_orderbook_ratio REAL
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status)')

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

    # 🔥 [Phase 1A] 뉴스 테이블 (센티멘트 분석용)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        external_id TEXT UNIQUE,
        url TEXT,
        title TEXT,
        description TEXT,
        source TEXT,
        published_at TIMESTAMP,
        tickers TEXT,
        sentiment REAL,
        is_critical INTEGER DEFAULT 0,
        raw_tags TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_critical ON news(is_critical, published_at DESC)')

    # 🔥 [v5 데이터수집] 호가창 이력 — 진입타이밍 피처 학습용 (업비트는 과거 호가 미제공 → 지금부터 적재)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS orderbook_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT,
        ts TIMESTAMP,
        mid_price REAL,
        bid_krw REAL,        -- 상위5호가 매수 총액(KRW)
        ask_krw REAL,        -- 상위5호가 매도 총액(KRW)
        bid_ask_ratio REAL   -- bid_krw/ask_krw (1↑ 매수우세)
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_oblog_ticker_ts ON orderbook_log(ticker, ts)')

    conn.commit()
    conn.close()
    print(f">>> DB connected: {DB_PATH}")