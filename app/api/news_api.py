"""
[Phase 1A] 뉴스 REST API (읽기 전용, 무인증)
- GET /news/recent           - 최신 N건
- GET /news/ticker/{ticker}  - 코인별 N시간 내 뉴스
- GET /news/sentiment/{ticker} - 코인별 집계 센티멘트
- GET /news/stats            - 수집기 상태
"""
import sqlite3
from fastapi import APIRouter, Query

from app.core.database import DB_PATH

router = APIRouter()


def _row_to_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def _normalize_symbol(ticker: str) -> str:
    """'KRW-BTC' / 'krw-btc' / 'BTC' 등에서 'BTC' 추출"""
    s = (ticker or "").upper().replace("KRW-", "")
    return s.strip()


@router.get("/recent")
def get_recent_news(limit: int = Query(20, ge=1, le=100)):
    """최신 뉴스 N건"""
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM news ORDER BY datetime(published_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"status": "success", "data": [_row_to_dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/ticker/{ticker}")
def get_ticker_news(
    ticker: str,
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    """특정 코인 관련 뉴스 (기본 최근 48h)"""
    symbol = _normalize_symbol(ticker)
    if not symbol:
        return {"status": "error", "message": "invalid ticker"}
    pattern = f"%,{symbol},%"
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM news
                WHERE (',' || COALESCE(tickers, '') || ',') LIKE ?
                  AND datetime(published_at) >= datetime('now', '-' || ? || ' hours')
                ORDER BY datetime(published_at) DESC LIMIT ?
                """,
                (pattern, hours, limit),
            ).fetchall()
        return {"status": "success", "data": [_row_to_dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/sentiment/{ticker}")
def get_ticker_sentiment(ticker: str, hours: int = Query(24, ge=1, le=720)):
    """특정 코인의 N시간 내 평균 센티멘트 + 치명적건수"""
    symbol = _normalize_symbol(ticker)
    if not symbol:
        return {"status": "error", "message": "invalid ticker"}
    pattern = f"%,{symbol},%"
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    ROUND(AVG(sentiment), 3) AS avg_sentiment,
                    SUM(is_critical) AS critical_count,
                    MIN(sentiment) AS min_sentiment,
                    MAX(sentiment) AS max_sentiment
                FROM news
                WHERE (',' || COALESCE(tickers, '') || ',') LIKE ?
                  AND datetime(published_at) >= datetime('now', '-' || ? || ' hours')
                """,
                (pattern, hours),
            ).fetchone()
        data = _row_to_dict(row) if row else {}
        data["ticker"] = symbol
        data["hours"] = hours
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/stats")
def get_collector_stats():
    """뉴스 수집기 상태 (모니터링용)"""
    from app.services.news_collector import news_collector
    return {"status": "success", "data": news_collector.get_stats()}
