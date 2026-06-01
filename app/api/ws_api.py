"""
[P2] WebSocket 실시간 업데이트
- /ws/prices: 연결 시 즉시 현재 캐시 전송, 이후 ~2초마다 broadcast
- 폴링(/market/prices, 5초 간격) 대체. 실패 시 프론트가 폴링으로 폴백.
- 읽기 전용이므로 인증 불필요 (HTTP 폴링 엔드포인트와 동일 정책)
"""
import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.trade_manager import trade_manager

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, payload: dict):
        if not self.active:
            return
        msg = json.dumps(payload, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@router.websocket("/ws/prices")
async def ws_prices(ws: WebSocket):
    await manager.connect(ws)
    try:
        # 연결 즉시 현재 캐시 전송 (있다면)
        cache = trade_manager.frontend_cache
        if cache:
            await ws.send_text(json.dumps(
                {"type": "prices", "data": cache},
                ensure_ascii=False, default=str
            ))
        # keep-alive — 클라이언트 ping/메시지 수신만 (응답은 broadcast로)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        print(f"⚠️ [WS] {e}")
        manager.disconnect(ws)


async def broadcast_loop():
    """2초마다 frontend_cache를 전체 연결에 push (변경 여부 무관, 단순)."""
    while True:
        try:
            cache = trade_manager.frontend_cache
            if cache and manager.active:
                await manager.broadcast({"type": "prices", "data": cache})
        except Exception as e:
            print(f"⚠️ [WS broadcast] {e}")
        await asyncio.sleep(2)
