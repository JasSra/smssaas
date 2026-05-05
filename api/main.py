from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone

from db import init_db, db
from worker_router  import router as worker_router
from tenant_router  import router as tenant_router
from admin_router   import router as admin_router
from audio_ws       import router as ws_router
from billing_router import router as billing_router
from voice_router   import router as voice_router
import billing


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    billing.ensure_stripe_products()
    task = asyncio.create_task(_offline_monitor())
    yield
    task.cancel()


app = FastAPI(title="SmsSaaS", version="1.0.0", lifespan=lifespan)

app.include_router(worker_router)
app.include_router(tenant_router)
app.include_router(admin_router)
app.include_router(ws_router)
app.include_router(billing_router)
app.include_router(voice_router)


@app.get("/health")
def health():
    return {"ok": True}


async def _offline_monitor():
    """Mark phones offline if no heartbeat for 5 minutes."""
    while True:
        await asyncio.sleep(60)
        try:
            with db() as conn:
                conn.execute(
                    """
                    UPDATE phones SET status='offline'
                    WHERE status='online'
                      AND (last_seen IS NULL
                           OR datetime(last_seen) < datetime('now', '-5 minutes'))
                    """
                )
        except Exception:
            pass
