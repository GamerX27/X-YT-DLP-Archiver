import asyncio
import os
from contextlib import asynccontextmanager

import state
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pipeline import worker
from routes import api_router
from schedulers import cleanup_scheduler, monitor_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.semaphore = asyncio.Semaphore(state.MAX_CONCURRENT_DOWNLOADS)
    for _ in range(state.MAX_CONCURRENT_DOWNLOADS):
        t = asyncio.create_task(worker())
        state.worker_tasks.append(t)
    state.worker_tasks.append(asyncio.create_task(monitor_scheduler()))
    state.worker_tasks.append(asyncio.create_task(cleanup_scheduler()))
    yield
    for t in state.worker_tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

app.include_router(api_router)
