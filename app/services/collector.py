import time
import threading
import pyupbit

class Collector:
    def __init__(self, shared_dict):
        self.shared_dict = shared_dict
        self.thread = None
        self.running = False

    def start(self):
        """수집기 쓰레드 시작"""
        self.running = True
        # [Fix] Process -> Thread로 변경
        # 쓰레드는 내부에서 서브 프로세스(pyupbit)를 생성해도 에러가 나지 않음
        self.thread = threading.Thread(target=self._run_websocket_collector)
        self.thread.daemon = True # 메인 서버 죽으면 같이 죽도록 설정
        self.thread.start()

    def stop(self):
        """수집기 종료"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)

    def _run_websocket_collector(self):
        """
        [WebSocket] 업비트 서버와 연결 (Thread 내부 실행)
        """
        print(">>> 🔌 [Collector] WebSocket 연결 시도...")
        
        # 1. KRW 마켓 전 종목 조회
        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
        except Exception as e:
            print(f">>> ⚠️ [Collector] 티커 조회 실패: {e}")
            return

        # 2. 웹소켓 매니저 생성 (내부적으로 별도 프로세스 생성됨 - 쓰레드에선 허용)
        try:
            wm = pyupbit.WebSocketManager("ticker", tickers)
        except Exception as e:
            print(f">>> ❌ [Collector] WebSocket 생성 실패: {e}")
            return
        
        print(f">>> ⚡ [Collector] 실시간 데이터 수신 시작 ({len(tickers)}개 코인)")
        
        try:
            while self.running:
                # 3. 데이터 수신 (Blocking)
                # wm.get()은 데이터가 올 때까지 대기함
                data = wm.get()
                
                # 종료 신호가 오면 루프 탈출
                if not self.running:
                    break

                if data and 'code' in data:
                    ticker = data['code']
                    
                    # 4. 공유 메모리 업데이트
                    # Manager.dict()는 쓰레드/프로세스 안전함
                    self.shared_dict[ticker] = {
                        "current_price": float(data['trade_price']),
                        "acc_trade_price_24h": float(data['acc_trade_price_24h']),
                        "timestamp": data.get('timestamp', time.time() * 1000) 
                    }
                    
        except Exception as e:
            print(f">>> ⚠️ [Collector Error] {e}")
        finally:
            # 5. 종료 시 정리 (중요: 좀비 프로세스 방지)
            print(">>> 🔌 [Collector] WebSocket 연결 종료")
            if wm:
                wm.terminate()

# 전역 함수 (main.py에서 호출)
def start_collector_thread(shared_dict):
    collector = Collector(shared_dict)
    collector.start()
    return collector