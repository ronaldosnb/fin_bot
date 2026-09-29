import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
import httpx
from sqlalchemy import or_, select

from app.commands import process_command
from app.config import get_settings, validate_runtime
from app.db import SessionLocal
from app.evolution import send_text
from app.finance import expand_schedule, seed_categories
from app.models import Inbox, Outbox, Schedule


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _claim(model):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.scalar(
            select(model)
            .where(
                or_(model.state == "pending", model.state == "processing"),
                model.next_attempt_at <= now,
            )
            .order_by(model.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not row:
            return None
        row.state = "processing"
        row.attempts += 1
        row.next_attempt_at = now + timedelta(minutes=5)
        row_id = row.id
        db.commit()
        return row_id


def _retry(row, exc: Exception) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        row.error = f"HTTP {exc.response.status_code} da Evolution"
    else:
        row.error = type(exc).__name__
    if row.attempts >= 5:
        row.state = "failed"
    else:
        row.state = "pending"
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** row.attempts * 5))


def process_inbox() -> bool:
    row_id = _claim(Inbox)
    if row_id is None:
        return False
    with SessionLocal() as db:
        row = db.get(Inbox, row_id)
        try:
            seed_categories(db)
            result = process_command(db, row.payload)
            if result:
                chat_jid, text = result
                db.add(Outbox(inbox_id=row.id, chat_jid=chat_jid, text=text))
            row.state = "done"
            row.payload = {}
            row.error = None
            db.commit()
        except ValueError as exc:
            db.rollback()
            row = db.get(Inbox, row_id)
            chat_jid = ((row.payload.get("data") or {}).get("key") or {}).get("remoteJid")
            if chat_jid:
                db.add(Outbox(inbox_id=row.id, chat_jid=chat_jid, text=f"Não registrei: {exc}"))
            row.state = "done"
            row.payload = {}
            db.commit()
        except Exception as exc:
            db.rollback()
            row = db.get(Inbox, row_id)
            _retry(row, exc)
            db.commit()
            log.warning("Falha no processamento da mensagem %s: %s", row_id, type(exc).__name__)
    return True


def process_outbox() -> bool:
    row_id = _claim(Outbox)
    if row_id is None:
        return False
    with SessionLocal() as db:
        row = db.get(Outbox, row_id)
        try:
            send_text(row.chat_jid, row.text)
            row.state = "sent"
            row.text = ""
            row.error = None
            db.commit()
        except Exception as exc:
            _retry(row, exc)
            db.commit()
            log.warning("Falha ao enviar resposta %s: %s", row_id, type(exc).__name__)
    return True


def expand_schedules() -> None:
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    through = today + relativedelta(months=12)
    with SessionLocal() as db:
        for schedule in db.scalars(select(Schedule).where(Schedule.active.is_(True))):
            expand_schedule(db, schedule, through)
        db.commit()


def main() -> None:
    validate_runtime()
    last_expansion = None
    with SessionLocal() as db:
        seed_categories(db)
    while True:
        today = datetime.now(ZoneInfo(get_settings().timezone)).date()
        if today != last_expansion:
            try:
                expand_schedules()
                last_expansion = today
            except Exception:
                log.exception("Falha ao expandir recorrências")
        worked = process_inbox()
        worked = process_outbox() or worked
        if not worked:
            time.sleep(2)


if __name__ == "__main__":
    main()
