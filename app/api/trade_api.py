from fastapi import APIRouter
from app.services.trade_manager import trade_manager
from pydantic import BaseModel

router = APIRouter()

@router.post("/start")
def start_trading():
    """실매매 시작"""
    trade_manager.start()
    return {"status": "started", "message": "Trading started"}

@router.post("/stop")
def stop_trading():
    """실매매 중지"""
    trade_manager.stop()
    return {"status": "stopped", "message": "Trading stopped"}

@router.get("/status")
def get_status():
    """현재 봇의 가동 상태 확인 (프론트엔드 폴링용)"""
    return {
        "status": "active" if trade_manager.is_active else "inactive",
        "is_active": trade_manager.is_active
    }

from pydantic import BaseModel

class ManualTradeRequest(BaseModel):
    ticker: str
    amount: float = 0

@router.post("/manual/buy")
async def manual_buy(req: ManualTradeRequest):
    return await trade_manager.place_manual_buy(req.ticker, req.amount)

@router.post("/manual/sell")
async def manual_sell(req: ManualTradeRequest):
    return await trade_manager.place_manual_sell(req.ticker)