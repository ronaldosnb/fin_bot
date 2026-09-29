import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from app.admin import router as admin_router
from app.config import get_settings, validate_runtime
from app.db import SessionLocal
from app.models import Inbox


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_runtime()
    yield


app = FastAPI(title="FinBot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/evolution")
async def evolution_webhook(request: Request) -> dict[str, bool]:
    payload = await request.json()
    event = str(payload.get("event") or "").lower().replace("_", ".")
    if event != "messages.upsert":
        return {"ok": True}
    settings = get_settings()
    instance = payload.get("instance")
    if instance != settings.evolution_instance:
        raise HTTPException(status_code=403, detail="Instância não autorizada")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return {"ok": True}
    key = data.get("key") or {}
    message_id = key.get("id")
    if not message_id or not key.get("remoteJid"):
        return {"ok": True}
    message = data.get("message") or {}
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        if wrapper in message:
            message = (message[wrapper] or {}).get("message") or {}
    command = message.get("conversation") or (message.get("extendedTextMessage") or {}).get("text") or ""
    if not isinstance(command, str) or not re.match(r"^/fin(?:\s|$)", command.strip(), re.IGNORECASE):
        return {"ok": True}
    normalized = {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "id": message_id,
                "remoteJid": key["remoteJid"],
                "fromMe": bool(key.get("fromMe")),
                "participant": key.get("participant") or data.get("participant"),
            },
            "message": {"conversation": command[:2000]},
            "messageTimestamp": data.get("messageTimestamp"),
        },
    }
    with SessionLocal() as db:
        db.add(Inbox(instance=instance, message_id=message_id, payload=normalized))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return {"ok": True}
