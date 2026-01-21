import asyncio
from contextlib import asynccontextmanager
# from multiprocessing import Manager  <-- 이거 이제 필요 없음 (삭제)
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
    """
    [서버 생명주기 관리]
    """
    global collector, loop_task

    print("\n>>> 🟢 [System] CoinMate 서버 시작 중...")

    # 🔥 [핵심 수정] Manager().dict() 대신 그냥 일반 딕셔너리 사용!
    # 같은 프로세스 안에서는 이걸로도 충분히 공유되며, 훨씬 빠르고 락(Lock)이 안 걸림.
    shared_data = {} 
    print(">>> 💾 [System] 고속 메모리(Fast Dict) 초기화 완료")

    # 1. 수집기 실행 (이제 일반 dict에 데이터를 꽂아줌)
    collector = start_collector_thread(shared_data)
    
    # 2. TradeManager 연결 (같은 dict를 읽음)
    trade_manager.set_shared_data(shared_data)
    
    # 3. 백그라운드 루프 실행
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

    # (Manager 종료 코드는 필요 없음)

    print(">>> 👋 [System] Bye Bye! (Clean Exit)")

app = FastAPI(
    title="CoinMate AI Trading System",
    description="Upbit Automatic Trading Bot with React Dashboard",
    version="2.0.0",
    lifespan=lifespan 
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록 (사용자님 코드 유지)
app.include_router(market_api.router, prefix="/market", tags=["Market Data"])
app.include_router(trade_api.router, prefix="/trade", tags=["Trading Control"])

@app.get("/")
def read_root():
    return {"status": "ok", "message": "CoinMate Trading Server is Running 🚀"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)