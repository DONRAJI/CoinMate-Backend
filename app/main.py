import asyncio
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import market_api, trade_api, ws_api, news_api
from app.services.collector import start_collector_thread
from app.services.trade_manager import trade_manager
from app.services.news_collector import news_collector
from app.core.config import ALLOWED_ORIGINS

# 전역 변수
collector = None
loop_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector, loop_task

    print("\n>>> 🟢 [System] CoinMate 서버 시작 중...")

    shared_data = {}
    shared_lock = threading.Lock()
    print(f">>> 💾 [System] 고속 메모리(Fast Dict) 초기화 완료 (ID: {id(shared_data)})")

    # 1. 수집기 실행
    collector = start_collector_thread(shared_data, shared_lock)
    
    # 2. TradeManager 연결
    trade_manager.set_shared_data(shared_data, shared_lock)
    
    # 3. 백그라운드 루프 실행
    loop_task = asyncio.create_task(trade_manager.run_loop())
    print(">>> 🤖 [System] TradeManager 백그라운드 루프 시작됨")

    # 4. [P2] WebSocket broadcast 루프
    ws_task = asyncio.create_task(ws_api.broadcast_loop())
    print(">>> 📡 [System] WebSocket broadcast 루프 시작됨")

    # 5. [Phase 1A] 뉴스 수집 루프
    news_task = asyncio.create_task(news_collector.loop())
    print(">>> 🗞️ [System] News collector 루프 시작됨 (15분 주기)")

    yield

    print("\n>>> 🔴 [System] 서버 종료 절차 시작...")
    if loop_task: loop_task.cancel()
    if ws_task: ws_task.cancel()
    if news_task: news_task.cancel()
    if collector: collector.stop()
    print(">>> 👋 [System] Bye Bye!")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # 인증은 X-API-Key 헤더 기반(쿠키 미사용)이므로 credentials 불필요.
    # 특정 origin 목록일 때만 credentials 허용 (와일드카드와 동시 사용 불가)
    allow_credentials=(ALLOWED_ORIGINS != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market_api.router, prefix="/market", tags=["Market Data"])
app.include_router(trade_api.router, prefix="/trade", tags=["Trading Control"])
app.include_router(ws_api.router, tags=["WebSocket"])  # /ws/prices
app.include_router(news_api.router, prefix="/news", tags=["News"])

@app.get("/")
def read_root():
    return {"status": "ok", "message": "CoinMate Trading Server is Running"}

@app.get("/health")
def health_check():
    data_count = len(trade_manager.shared_data) if trade_manager.shared_data else 0
    open_trades = trade_manager.repo.get_trade_count()
    return {
        "status": "healthy" if data_count > 0 else "degraded",
        "bot_active": trade_manager.is_active,
        "realtime_feeds": data_count,
        "open_trades": open_trades,
        "target_coins": len(trade_manager.target_coins),
    }

if __name__ == "__main__":
    import uvicorn
    # 🔥 [수정 2] reload=False로 변경 (봇 실행 시 필수)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)