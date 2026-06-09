from fastapi import APIRouter, Depends
from app.services.trade_manager import trade_manager
from app.core.auth import verify_api_key
from pydantic import BaseModel

router = APIRouter()

@router.post("/start", dependencies=[Depends(verify_api_key)])
def start_trading():
    """실매매 시작"""
    trade_manager.start()
    return {"status": "started", "message": "Trading started"}

@router.post("/stop", dependencies=[Depends(verify_api_key)])
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

@router.post("/manual/buy", dependencies=[Depends(verify_api_key)])
async def manual_buy(req: ManualTradeRequest):
    return await trade_manager.place_manual_buy(req.ticker, req.amount)

@router.post("/manual/sell", dependencies=[Depends(verify_api_key)])
async def manual_sell(req: ManualTradeRequest):
    return await trade_manager.place_manual_sell(req.ticker)

@router.get("/history")
def get_trade_history(limit: int = 50):
    trades = trade_manager.repo.get_closed_trades(limit)
    return {"status": "success", "data": [dict(t) for t in trades]}

@router.get("/stats")
def get_trade_stats():
    stats = trade_manager.repo.get_trade_stats()
    return {"status": "success", "data": stats}

@router.get("/strategy-stats")
def get_strategy_stats():
    """전략 조합별/개별 성분별/레짐별 성과 집계"""
    return {"status": "success", "data": trade_manager.repo.get_strategy_stats()}

# === 섀도우(페이퍼) 모드 ===
@router.get("/shadow/stats")
def get_shadow_stats():
    """섀도우 모드 가상 거래 통계"""
    data = trade_manager.paper_repo.get_stats()
    data["shadow_mode"] = trade_manager.SHADOW_MODE
    return {"status": "success", "data": data}

@router.get("/shadow/history")
def get_shadow_history(limit: int = 50):
    """섀도우 가상 거래 내역"""
    rows = trade_manager.paper_repo.get_closed(limit)
    return {"status": "success", "data": [dict(r) for r in rows]}

class ShadowToggleRequest(BaseModel):
    enabled: bool

@router.post("/shadow/toggle", dependencies=[Depends(verify_api_key)])
def toggle_shadow(req: ShadowToggleRequest):
    """섀도우 모드 ON/OFF (재시작 후에도 유지되도록 영속화)"""
    trade_manager.SHADOW_MODE = req.enabled
    trade_manager.save_persisted_config()
    return {"status": "success", "shadow_mode": trade_manager.SHADOW_MODE}

@router.post("/shadow/reset", dependencies=[Depends(verify_api_key)])
def reset_shadow():
    """섀도우 가상 잔고/거래 초기화"""
    trade_manager.paper_repo.reset()
    return {"status": "success", "message": "섀도우 초기화 완료"}

# === 설정 조회/변경 API ===
@router.get("/config")
def get_config():
    """현재 봇 설정값 조회"""
    return {
        "status": "success",
        "data": {
            "stop_loss": trade_manager.STOP_LOSS,
            "profit_target": trade_manager.PROFIT_TARGET,
            "trailing_activation": trade_manager.TRAILING_ACTIVATION,
            "trailing_distance": trade_manager.TRAILING_DISTANCE,
            "max_coin_count": trade_manager.MAX_COIN_COUNT,
            "min_order_krw": trade_manager.MIN_ORDER_KRW,
            "rebuy_cooldown": trade_manager.REBUY_COOLDOWN,
            "buy_threshold": trade_manager.strategy.BUY_THRESHOLD,
        }
    }

class ConfigUpdateRequest(BaseModel):
    stop_loss: float | None = None
    profit_target: float | None = None
    trailing_activation: float | None = None
    trailing_distance: float | None = None
    max_coin_count: int | None = None
    min_order_krw: int | None = None
    rebuy_cooldown: int | None = None
    buy_threshold: float | None = None

@router.post("/config", dependencies=[Depends(verify_api_key)])
def update_config(req: ConfigUpdateRequest):
    """봇 설정값 실시간 변경 (재시작 후에도 user_config.json으로 유지됨)"""
    changes = []
    if req.stop_loss is not None:
        trade_manager.STOP_LOSS = req.stop_loss
        changes.append(f"stop_loss={req.stop_loss}")
    if req.profit_target is not None:
        trade_manager.PROFIT_TARGET = req.profit_target
        changes.append(f"profit_target={req.profit_target}")
    if req.trailing_activation is not None:
        trade_manager.TRAILING_ACTIVATION = req.trailing_activation
        changes.append(f"trailing_activation={req.trailing_activation}")
    if req.trailing_distance is not None:
        trade_manager.TRAILING_DISTANCE = req.trailing_distance
        changes.append(f"trailing_distance={req.trailing_distance}")
    if req.max_coin_count is not None:
        trade_manager.MAX_COIN_COUNT = req.max_coin_count
        changes.append(f"max_coin_count={req.max_coin_count}")
    if req.min_order_krw is not None:
        trade_manager.MIN_ORDER_KRW = req.min_order_krw
        changes.append(f"min_order_krw={req.min_order_krw}")
    if req.rebuy_cooldown is not None:
        trade_manager.REBUY_COOLDOWN = req.rebuy_cooldown
        changes.append(f"rebuy_cooldown={req.rebuy_cooldown}")
    if req.buy_threshold is not None:
        trade_manager.strategy.BUY_THRESHOLD = req.buy_threshold
        changes.append(f"buy_threshold={req.buy_threshold}")

    # 🔥 [P2] 변경 즉시 영속화 (재시작 후에도 유지)
    if changes:
        trade_manager.save_persisted_config()

    return {"status": "success", "message": f"변경됨(영속): {', '.join(changes)}"}