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

# 체결 슬리피지 가정 (편도) — 실거래는 시장가라 신호가와 체결가가 다름.
# 매수는 신호가보다 높게, 매도는 낮게 체결된다고 가정해 실거래에 근접시킴.
# 수수료(왕복 0.1%)와 별개. 거래대금 필터(10억↑)로 유동성 종목만 매수하므로 5bp로 설정.
SLIPPAGE_RATE = 0.0005  # 0.05% 편도 (왕복 0.1% 추가 비용)


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
        # 슬리피지: 매수는 신호가보다 높게 체결된다고 가정 (실거래 시장가 근사)
        fill_price = price * (1 + SLIPPAGE_RATE)
        with self._conn() as c:
            c.execute(
                """INSERT INTO paper_trades
                (ticker, buy_price, buy_amount, buy_time, status, strategy_name,
                 buy_score, buy_ml_prob, buy_regime, buy_rsi,
                 buy_news_sentiment, buy_news_critical_count, buy_orderbook_ratio,
                 buy_mom6h, buy_vol_surge, buy_pos24h)
                VALUES (?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, fill_price, amount, now_kst(), strategy_name,
                 context.get('score'), context.get('ml_prob'), context.get('regime'),
                 context.get('rsi'), context.get('news_sentiment'),
                 context.get('news_critical_count'), context.get('orderbook_ratio'),
                 context.get('mom6h'), context.get('vol_surge'), context.get('pos24h')),
            )
            c.commit()
        self.krw -= amount
        self._save_state()
        print(f"📝 [Paper] 가상매수 {ticker} @{fill_price:,.2f}(신호 {price:,.2f}, 슬리피지 {SLIPPAGE_RATE:.2%}) "
              f"({amount:,.0f}원) 잔여KRW {self.krw:,.0f}")
        return True

    def paper_sell(self, trade_id, sell_price, reason="Shadow"):
        """가상 매도: 수익률 계산 + 잔고 환원(수수료 왕복 0.1% 반영)"""
        with self._conn() as c:
            row = c.execute("SELECT ticker, buy_price, buy_amount FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if not row:
                return False
            buy_price = row['buy_price']
            buy_amount = row['buy_amount']
            # 슬리피지: 매도는 신호가보다 낮게 체결된다고 가정
            fill_sell = sell_price * (1 - SLIPPAGE_RATE)
            profit_rate = ((fill_sell - buy_price) / buy_price) * 100 if buy_price > 0 else 0
            # 수수료 왕복 0.1% 반영한 실현 금액 (슬리피지는 fill가에 이미 반영됨)
            realized = buy_amount * (1 - 0.0005) * (1 + profit_rate / 100) * (1 - 0.0005)
            c.execute(
                "UPDATE paper_trades SET status='closed', sell_price=?, sell_time=?, sell_reason=?, profit_rate=? WHERE id=?",
                (fill_sell, now_kst(), reason, profit_rate, trade_id),
            )
            c.commit()
        self.krw += realized
        self._save_state()
        print(f"📝 [Paper] 가상매도 {row['ticker']} @{fill_sell:,.2f}(신호 {sell_price:,.2f}) "
              f"수익률 {profit_rate:+.2f}% → 잔여KRW {self.krw:,.0f}")
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

    def get_today_stats(self):
        """[세션31] 오늘(KST) closed 가상거래 집계 — 일일 요약 알림용."""
        today = now_kst().strftime("%Y-%m-%d")
        with self._conn() as c:
            rows = c.execute(
                "SELECT profit_rate, buy_amount FROM paper_trades "
                "WHERE status='closed' AND substr(sell_time,1,10)=? AND profit_rate IS NOT NULL",
                (today,),
            ).fetchall()
            open_n = c.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]
        n = len(rows)
        wins = sum(1 for r in rows if r['profit_rate'] > 0)
        pnl = sum(
            (r['buy_amount'] or 0) * (1 - 0.0005) * (1 + (r['profit_rate'] or 0) / 100) * (1 - 0.0005) - (r['buy_amount'] or 0)
            for r in rows
        )
        return {"trades": n, "wins": wins,
                "win_rate": round(wins / n * 100, 1) if n else 0,
                "pnl": int(pnl), "open": open_n}

    def get_closed(self, limit=50):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM paper_trades WHERE status='closed' ORDER BY sell_time DESC LIMIT ?",
                (limit,),
            ).fetchall()


paper_repository = PaperRepository()
