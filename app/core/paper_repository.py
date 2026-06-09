"""
[섀도우 모드] 페이퍼(가상) 거래 저장소.
실거래 TradeRepository와 동일한 인터페이스 일부를 제공하되 paper_trades 테이블 사용.
가상 잔고는 인메모리/파일로 관리 (별도 KRW 추적).
"""
import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from app.core.database import DB_PATH

KST = timezone(timedelta(hours=9))

# 가상 잔고 상태 파일 (재시작 후에도 유지)
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PAPER_STATE_PATH = os.path.join(_BASE, "cache", "paper_state.json")

INITIAL_KRW = 1_000_000  # 가상 시작 자금 100만원


def now_kst():
    return datetime.now(KST)


class PaperRepository:
    def __init__(self):
        self.krw = INITIAL_KRW
        self._load_state()

    # ─────────────── 가상 잔고 ───────────────
    def _load_state(self):
        if os.path.exists(PAPER_STATE_PATH):
            try:
                with open(PAPER_STATE_PATH, encoding='utf-8') as f:
                    self.krw = json.load(f).get('krw', INITIAL_KRW)
            except Exception:
                self.krw = INITIAL_KRW

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(PAPER_STATE_PATH), exist_ok=True)
            with open(PAPER_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump({'krw': self.krw}, f)
        except Exception as e:
            print(f"⚠️ [Paper] 상태 저장 실패: {e}")

    def get_krw_balance(self):
        return self.krw

    def reset(self):
        """가상 잔고/거래 초기화"""
        self.krw = INITIAL_KRW
        self._save_state()
        with self._conn() as c:
            c.execute("DELETE FROM paper_trades")
            c.commit()

    # ─────────────── DB ───────────────
    def _conn(self):
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_open_count(self):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]

    def get_open_tickers(self):
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT ticker FROM paper_trades WHERE status='open'").fetchall()]

    def is_holding(self, ticker):
        with self._conn() as c:
            r = c.execute("SELECT 1 FROM paper_trades WHERE ticker=? AND status='open' LIMIT 1", (ticker,)).fetchone()
            return r is not None

    def get_open_trade(self, ticker):
        with self._conn() as c:
            return c.execute(
                "SELECT id, buy_price, buy_amount, buy_time FROM paper_trades WHERE ticker=? AND status='open'",
                (ticker,),
            ).fetchone()

    def get_open_trades(self):
        with self._conn() as c:
            return c.execute(
                "SELECT id, ticker, buy_price, buy_amount, buy_time, strategy_name FROM paper_trades WHERE status='open'"
            ).fetchall()

    # ─────────────── 가상 매수/매도 ───────────────
    def paper_buy(self, ticker, price, amount, strategy_name="Shadow", context=None):
        """가상 매수: 잔고 차감 + paper_trades INSERT"""
        context = context or {}
        if amount > self.krw:
            return False
        with self._conn() as c:
            c.execute(
                """INSERT INTO paper_trades
                (ticker, buy_price, buy_amount, buy_time, status, strategy_name,
                 buy_score, buy_ml_prob, buy_regime, buy_rsi,
                 buy_news_sentiment, buy_news_critical_count, buy_orderbook_ratio)
                VALUES (?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                (ticker, price, amount, now_kst(), strategy_name,
                 context.get('score'), context.get('ml_prob'), context.get('regime'),
                 context.get('rsi'), context.get('news_sentiment'),
                 context.get('news_critical_count'), context.get('orderbook_ratio')),
            )
            c.commit()
        self.krw -= amount
        self._save_state()
        print(f"📝 [Paper] 가상매수 {ticker} @{price:,.2f} ({amount:,.0f}원) 잔여KRW {self.krw:,.0f}")
        return True

    def paper_sell(self, trade_id, sell_price, reason="Shadow"):
        """가상 매도: 수익률 계산 + 잔고 환원(수수료 왕복 0.1% 반영)"""
        with self._conn() as c:
            row = c.execute("SELECT ticker, buy_price, buy_amount FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if not row:
                return False
            buy_price = row['buy_price']
            buy_amount = row['buy_amount']
            profit_rate = ((sell_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
            # 수수료 왕복 반영한 실현 금액
            realized = buy_amount * (1 - 0.0005) * (1 + profit_rate / 100) * (1 - 0.0005)
            c.execute(
                "UPDATE paper_trades SET status='closed', sell_price=?, sell_time=?, sell_reason=?, profit_rate=? WHERE id=?",
                (sell_price, now_kst(), reason, profit_rate, trade_id),
            )
            c.commit()
        self.krw += realized
        self._save_state()
        print(f"📝 [Paper] 가상매도 {row['ticker']} 수익률 {profit_rate:+.2f}% → 잔여KRW {self.krw:,.0f}")
        return True

    # ─────────────── 통계 ───────────────
    def get_stats(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT profit_rate, buy_amount FROM paper_trades WHERE status='closed' AND profit_rate IS NOT NULL"
            ).fetchall()
            open_rows = c.execute(
                "SELECT ticker, buy_price, buy_amount FROM paper_trades WHERE status='open'"
            ).fetchall()
        n = len(rows)
        wins = sum(1 for r in rows if r['profit_rate'] > 0)
        pnl = sum(
            (r['buy_amount'] or 0) * (1 - 0.0005) * (1 + (r['profit_rate'] or 0) / 100) * (1 - 0.0005) - (r['buy_amount'] or 0)
            for r in rows
        )
        open_value = sum((r['buy_amount'] or 0) for r in open_rows)
        return {
            "initial_krw": INITIAL_KRW,
            "current_krw": round(self.krw, 0),
            "open_positions": len(open_rows),
            "open_value": round(open_value, 0),
            "total_assets": round(self.krw + open_value, 0),
            "total_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n * 100, 1) if n else 0,
            "realized_pnl": round(pnl, 0),
            "return_pct": round((self.krw + open_value - INITIAL_KRW) / INITIAL_KRW * 100, 2),
        }

    def get_closed(self, limit=50):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM paper_trades WHERE status='closed' ORDER BY sell_time DESC LIMIT ?",
                (limit,),
            ).fetchall()


paper_repository = PaperRepository()
