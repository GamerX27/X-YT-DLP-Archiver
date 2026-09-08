import asyncio
import json

import state
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.ws_clients.append(websocket)
    try:
        await websocket.send_text(
            json.dumps({"type": "init", "tasks": list(state.tasks.values())})
        )
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.ws_clients:
            state.ws_clients.remove(websocket)
