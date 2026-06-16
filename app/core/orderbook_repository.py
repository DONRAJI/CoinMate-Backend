"""
[v5 데이터수집] 호가창 이력 저장소.
업비트는 과거 호가를 제공하지 않으므로 지금부터 주기 적재해야 v5 진입타이밍 피처를 만들 수 있음.
매매 로직과 독립 — 적재/정리만 담당.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from app.core.database import DB_PATH

KST = timezone(timedelta(hours=9))


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def insert_snapshots(rows):
    """rows: list of (ticker, mid_price, bid_krw, ask_krw, bid_ask_ratio).
    ts는 KST naive 문자열(pyupbit 캔들 인덱스와 동일 기준 → 추후 조인 용이)."""
    if not rows:
        return 0
    ts = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.executemany(
            "INSERT INTO orderbook_log (ticker, ts, mid_price, bid_krw, ask_krw, bid_ask_ratio) "
            "VALUES (?,?,?,?,?,?)",
            [(t, ts, m, b, a, r) for (t, m, b, a, r) in rows],
        )
        c.commit()
    return len(rows)


def cleanup_old(days: int = 30):
    """days일 이전 스냅샷 삭제 (테이블 무한 증가 방지)."""
    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        cur = c.execute("DELETE FROM orderbook_log WHERE ts < ?", (cutoff,))
        c.commit()
        return cur.rowcount


def stats():
    """모니터링용 — 총 건수 / 코인 수 / 기간."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM orderbook_log").fetchone()[0]
        nt = c.execute("SELECT COUNT(DISTINCT ticker) FROM orderbook_log").fetchone()[0]
        rng = c.execute("SELECT MIN(ts), MAX(ts) FROM orderbook_log").fetchone()
    return {"rows": n, "tickers": nt, "from": rng[0], "to": rng[1]}
