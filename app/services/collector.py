import threading
import asyncio
import json
import time
import pyupbit
import websockets # pip install websockets 필요

class Collector:
    def __init__(self, shared_dict):
        self.shared_dict = shared_dict
        self.thread = None
        self.running = False

    def start(self):
        """수집기 쓰레드 시작"""
        self.running = True
        self.thread = threading.Thread(target=self._run_async_loop)
        self.thread.daemon = True 
        self.thread.start()

    def stop(self):
        """수집기 종료"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)

    def _run_async_loop(self):
        """
        [핵심] Thread 안에서 asyncio 루프를 돌려서 웹소켓을 직접 관리
        pyupbit의 멀티프로세싱 이슈를 원천 차단함.
        """
        try:
            asyncio.run(self._websocket_worker())
        except Exception as e:
            print(f">>> ❌ [Collector Fatal Error] {e}")

    async def _websocket_worker(self):
        print(">>> 🔌 [Collector] WebSocket 직접 연결 준비 중...")
        
        # 1. 티커 조회
        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
        except:
            # 실패 시 비상용 하드코딩 (최소한의 코인으로라도 돌리기 위해)
            tickers = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
        
        uri = "wss://api.upbit.com/websocket/v1"
        
        # 업비트 구독 포맷
        subscribe_fmt = [
            {"ticket": "CoinMate-Bot"},
            {"type": "ticker", "codes": tickers, "isOnlyRealtime": True}
        ]

        # 2. 무한 재연결 루프 (끊기면 다시 붙음)
        while self.running:
            try:
                async with websockets.connect(uri, ping_interval=60) as websocket:
                    await websocket.send(json.dumps(subscribe_fmt))
                    print(f">>> ⚡ [Collector] 데이터 수신 시작 (Direct Mode, {len(tickers)}개)")
                    
                    first_msg = True
                    
                    while self.running:
                        data_str = await websocket.recv()
                        data = json.loads(data_str)
                        
                        if first_msg:
                            print(f">>> 🎉 [Collector] 첫 데이터 수신 성공! {data.get('code', 'Unknown')}")
                            first_msg = False

                        if 'code' in data:
                            ticker = data['code']
                            price = float(data['trade_price'])
                            
                            self.shared_dict[ticker] = {
                                "current_price": price,
                                "acc_trade_price_24h": float(data['acc_trade_price_24h']),
                                "timestamp": time.time()
                            }
                            
            except Exception as e:
                print(f">>> ⚠️ [Collector] 연결 끊김 ({e}). 3초 후 재연결...")
                await asyncio.sleep(3)

# 전역 함수 (main.py에서 호출)
def start_collector_thread(shared_dict):
    collector = Collector(shared_dict)
    collector.start()
    return collector