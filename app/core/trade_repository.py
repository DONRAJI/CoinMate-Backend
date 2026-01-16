import sqlite3
from datetime import datetime
from app.core.database import DB_PATH

class TradeRepository:
    def get_conn(self):
        return sqlite3.connect(DB_PATH)

    def get_open_trades(self):
        """현재 매수 중인(안 판) 거래 목록 가져오기"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            # 가져올 때 전략 이름도 같이 가져오는 게 좋습니다 (나중에 분석용)
            cursor.execute("SELECT id, ticker, buy_price, strategy_name FROM trades WHERE status='open'")
            return cursor.fetchall()

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
    def log_buy(self, ticker, price, amount, strategy_name="Unknown"):
        """매수 기록 저장"""
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO trades (ticker, buy_price, buy_amount, buy_time, status, strategy_name) 
                    VALUES (?, ?, ?, ?, 'open', ?)
                    """,
                    (ticker, price, amount, datetime.now(), strategy_name)
                )
                conn.commit()
                print(f"💾 [DB] {ticker} 매수 기록 완료 (전략: {strategy_name})")
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
                    (sell_price, datetime.now(), reason, profit_rate, trade_id)
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
                    (datetime.now(), trade_id)
                )
                conn.commit()
        except Exception as e:
            print(f"⚠️ [DB Error] 좀비 청산 실패: {e}")

    def get_open_trade(self, ticker):
        """특정 코인의 진행 중인 거래 정보 가져오기"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, buy_price, buy_amount FROM trades WHERE ticker=? AND status='open'", (ticker,))
            return cursor.fetchone()