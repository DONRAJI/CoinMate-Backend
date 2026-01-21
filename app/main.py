import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import market_api, trade_api
from app.services.collector import start_collector_thread
from app.services.trade_manager import trade_manager

# 전역 변수
collector = None
loop_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector, loop_task

    print("\n>>> 🟢 [System] CoinMate 서버 시작 중...")

    # 🔥 [수정 1] Manager 삭제 -> 일반 딕셔너리 사용 (속도 향상 & 락 방지)
    shared_data = {} 
    print(f">>> 💾 [System] 고속 메모리(Fast Dict) 초기화 완료 (ID: {id(shared_data)})")

    # 1. 수집기 실행
    collector = start_collector_thread(shared_data)
    
    # 2. TradeManager 연결
    trade_manager.set_shared_data(shared_data)
    
    # 3. 백그라운드 루프 실행
    loop_task = asyncio.create_task(trade_manager.run_loop())
    print(">>> 🤖 [System] TradeManager 백그라운드 루프 시작됨")

    yield

    print("\n>>> 🔴 [System] 서버 종료 절차 시작...")
    if loop_task: loop_task.cancel()
    if collector: collector.stop()
    print(">>> 👋 [System] Bye Bye!")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market_api.router, prefix="/market", tags=["Market Data"])
app.include_router(trade_api.router, prefix="/trade", tags=["Trading Control"])

@app.get("/")
def read_root():
    return {"status": "ok", "message": "CoinMate Trading Server is Running 🚀"}

if __name__ == "__main__":
    import uvicorn
    # 🔥 [수정 2] reload=False로 변경 (봇 실행 시 필수)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)