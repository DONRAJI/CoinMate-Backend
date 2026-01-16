import asyncio
from contextlib import asynccontextmanager
from multiprocessing import Manager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 🔥 [Fix] 경로 수정: app.api.endpoints -> app.api
from app.api import market_api, trade_api
from app.services.collector import start_collector_thread
from app.services.trade_manager import trade_manager

# 전역 변수
shared_manager = None
collector = None
loop_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    [서버 생명주기 관리]
    """
    global shared_manager, collector, loop_task

    print("\n>>> 🟢 [System] CoinMate 서버 시작 중...")

    # 1. 공유 메모리 생성
    shared_manager = Manager()
    shared_data = shared_manager.dict()
    print(">>> 💾 [System] 공유 메모리(Shared Memory) 초기화 완료")

    # 2. 수집기 실행
    collector = start_collector_thread(shared_data)
    
    # 3. TradeManager 연결
    trade_manager.set_shared_data(shared_data)
    
    # 4. 백그라운드 루프 실행
    loop_task = asyncio.create_task(trade_manager.run_loop())
    print(">>> 🤖 [System] TradeManager 백그라운드 루프 시작됨")

    yield  # 서버 가동 중...

    # ==========================================
    # 종료 절차
    # ==========================================
    print("\n>>> 🔴 [System] 서버 종료 절차 시작...")

    # 1. 트레이딩 루프부터 강제 종료
    if loop_task:
        print(">>> 🛑 [System] 백그라운드 루프 종료 중...")
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
    
    # 2. 수집기 종료
    if collector:
        collector.stop()
        print(">>> 🔌 [System] 데이터 수집기 종료 완료")

    # 3. 공유 메모리 해제
    if shared_manager:
        shared_manager.shutdown()
        print(">>> 💾 [System] 공유 메모리 해제 완료")

    print(">>> 👋 [System] Bye Bye! (Clean Exit)")

app = FastAPI(
    title="CoinMate AI Trading System",
    description="Upbit Automatic Trading Bot with React Dashboard",
    version="2.0.0",
    lifespan=lifespan 
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(market_api.router, prefix="/market", tags=["Market Data"])
app.include_router(trade_api.router, prefix="/trade", tags=["Trading Control"])

@app.get("/")
def read_root():
    return {"status": "ok", "message": "CoinMate Trading Server is Running 🚀"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)