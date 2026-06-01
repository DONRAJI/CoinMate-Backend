import sqlite3
from datetime import datetime, timezone, timedelta
from app.core.database import DB_PATH

KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

class TradeRepository:
    def get_conn(self):
        # 🔥 [P0] timeout=5초: 다른 연결이 쓰기 중이면 lock 에러 대신 최대 5초 대기
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row  # 🔥 [핵심] 컬럼명으로 접근 가능하게 변경
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_open_trades(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, ticker, buy_price, buy_amount, buy_time, strategy_name FROM trades WHERE status='open'")
            return cursor.fetchall() # 이제 Row 객체 리스트 반환

    def get_trade_count(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
            res = cursor.fetchone()
            return res[0] if res else 0

    def get_all_open_tickers(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ticker FROM trades WHERE status='open'")
            return [r[0] for r in cursor.fetchall()]

    # 🔥 [수정 1] strategy_name 파라미터 추가
    # 🔥 [P1] context: 매수 시점 score/ml_prob/regime/rsi 저장 (사후 분석용)
    # 🔥 [Phase 1B] context: news_sentiment/news_critical_count 추가
    def log_buy(self, ticker, price, amount, strategy_name="Unknown", context=None):
        """매수 기록 저장"""
        context = context or {}
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO trades (
                        ticker, buy_price, buy_amount, buy_time, status, strategy_name,
                        buy_score, buy_ml_prob, buy_regime, buy_rsi,
                        buy_news_sentiment, buy_news_critical_count
                    )
                    VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ticker, price, amount, now_kst(), strategy_name,
                     context.get('score'), context.get('ml_prob'),
                     context.get('regime'), context.get('rsi'),
                     context.get('news_sentiment'),
                     context.get('news_critical_count'))
                )
                conn.commit()
                news_part = ""
                if context.get('news_sentiment') is not None:
                    news_part = f", 뉴스: {context.get('news_sentiment'):+.2f}/crit{context.get('news_critical_count',0)}"
                print(f"💾 [DB] {ticker} 매수 기록 완료 (전략: {strategy_name}, 점수: {context.get('score')}, 레짐: {context.get('regime')}{news_part})")
        except Exception as e:
            print(f"⚠️ [DB Error] 매수 기록 실패: {e}")

    # 🔥 [수정 2] profit_rate 계산 로직 추가
    def log_sell(self, trade_id, sell_price, reason="익절/손절"):
        """매도 기록 저장 (상태 변경 및 수익률 기록)"""
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                
                # 1. 원래 매수가 가져오기 (수익률 계산용)
                cursor.execute("SELECT buy_price FROM trades WHERE id=?", (trade_id,))
                row = cursor.fetchone()
                
                profit_rate = 0.0
                if row:
                    buy_price = row[0]
                    if buy_price and buy_price > 0:
                        # 수익률 공식: ((매도가 - 매수가) / 매수가) * 100
                        profit_rate = ((sell_price - buy_price) / buy_price) * 100
                
                # 2. 업데이트 실행
                cursor.execute(
                    """
                    UPDATE trades 
                    SET status='closed', sell_price=?, sell_time=?, sell_reason=?, profit_rate=? 
                    WHERE id=?
                    """,
                    (sell_price, now_kst(), reason, profit_rate, trade_id)
                )
                conn.commit()
                print(f"💾 [DB] 거래ID {trade_id} 매도 완료 (수익률: {profit_rate:.2f}%)")
        except Exception as e:
            print(f"⚠️ [DB Error] 매도 기록 실패: {e}")
            
    def close_zombie_trade(self, trade_id):
        """지갑엔 없는데 DB에만 있는 좀비 데이터 강제 청산"""
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE trades SET status='closed', sell_price=0, sell_time=? WHERE id=?",
                    (now_kst(), trade_id)
                )
                conn.commit()
        except Exception as e:
            print(f"⚠️ [DB Error] 좀비 청산 실패: {e}")

    def get_open_trade(self, ticker):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, buy_price, buy_amount FROM trades WHERE ticker=? AND status='open'", (ticker,))
            return cursor.fetchone()

    def get_closed_trades(self, limit=50):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY sell_time DESC LIMIT ?",
                (limit,)
            )
            return cursor.fetchall()

    def get_trade_stats(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN profit_rate > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN profit_rate <= 0 THEN 1 ELSE 0 END) as losses,
                    ROUND(AVG(profit_rate), 2) as avg_profit,
                    ROUND(MAX(profit_rate), 2) as best_trade,
                    ROUND(MIN(profit_rate), 2) as worst_trade,
                    ROUND(SUM(
                        buy_amount * (1 - 0.0005) * (1 + profit_rate / 100.0) * (1 - 0.0005) - buy_amount
                    ), 0) as total_pnl
                FROM trades WHERE status='closed' AND profit_rate IS NOT NULL
            """)
            row = cursor.fetchone()
            if not row or row['total'] == 0:
                return {"total": 0, "wins": 0, "losses": 0, "avg_profit": 0, "win_rate": 0, "total_pnl": 0}
            return {
                "total": row['total'],
                "wins": row['wins'] or 0,
                "losses": row['losses'] or 0,
                "avg_profit": row['avg_profit'] or 0,
                "best_trade": row['best_trade'] or 0,
                "worst_trade": row['worst_trade'] or 0,
                "win_rate": round((row['wins'] or 0) / row['total'] * 100, 1),
                "total_pnl": int(row['total_pnl'] or 0),
            }

    # 🔥 [P1] 전략 조합별 / 개별 성분별 / 레짐별 성과 집계
    def get_strategy_stats(self):
        with self.get_conn() as conn:
            rows = conn.execute("""
                SELECT strategy_name, profit_rate, buy_amount, buy_regime
                FROM trades WHERE status='closed' AND profit_rate IS NOT NULL
            """).fetchall()

        # 수수료 왕복(0.05% x2) 반영 손익(원)
        def calc_pnl(amount, rate):
            if not amount:
                return 0.0
            return amount * (1 - 0.0005) * (1 + rate / 100.0) * (1 - 0.0005) - amount

        COMPONENTS = ['trend', 'adx', 'macd', 'volume', 'vwap', 'bollinger', 'rsi', 'mfi']
        comp = {c: {'total': 0, 'wins': 0, 'pnl': 0.0, 'sum': 0.0} for c in COMPONENTS}
        combo = {}
        regime = {}

        for r in rows:
            name = r['strategy_name'] or 'Unknown'
            rate = r['profit_rate'] or 0
            amount = r['buy_amount'] or 0
            reg = r['buy_regime'] or '미기록'
            win = 1 if rate > 0 else 0
            p = calc_pnl(amount, rate)

            cs = combo.setdefault(name, {'total': 0, 'wins': 0, 'pnl': 0.0, 'sum': 0.0})
            cs['total'] += 1; cs['wins'] += win; cs['pnl'] += p; cs['sum'] += rate

            for c in COMPONENTS:
                if c in name:
                    d = comp[c]
                    d['total'] += 1; d['wins'] += win; d['pnl'] += p; d['sum'] += rate

            rs = regime.setdefault(reg, {'total': 0, 'wins': 0, 'pnl': 0.0, 'sum': 0.0})
            rs['total'] += 1; rs['wins'] += win; rs['pnl'] += p; rs['sum'] += rate

        def finalize(d):
            out = []
            for k, v in d.items():
                if v['total'] == 0:
                    continue
                out.append({
                    'name': k,
                    'total': v['total'],
                    'win_rate': round(v['wins'] / v['total'] * 100, 1),
                    'avg_profit': round(v['sum'] / v['total'], 2),
                    'total_pnl': int(v['pnl']),
                })
            return sorted(out, key=lambda x: x['total_pnl'], reverse=True)

        return {
            'by_component': finalize(comp),
            'by_combo': finalize(combo),
            'by_regime': finalize(regime),
        }