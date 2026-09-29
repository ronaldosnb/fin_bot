import base64
import secrets
import threading
import time
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.finance import audit_change, category_names, format_money, money_to_cents, parse_date
from app.models import Category, Chat, Entry, Inbox, Ledger, Outbox, Schedule, utcnow


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["money"] = format_money
password_hasher = PasswordHasher()
login_attempts: dict[str, list[float]] = {}
login_lock = threading.Lock()


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().app_secret
    if len(secret) < 32:
        raise RuntimeError("APP_SECRET precisa ter pelo menos 32 caracteres")
    return URLSafeTimedSerializer(secret, salt="finbot-admin")


def _session(request: Request) -> dict | None:
    token = request.cookies.get("finbot_session")
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=8 * 60 * 60)
        return data if data.get("role") == "admin" else None
    except (BadSignature, SignatureExpired):
        return None


def require_admin(request: Request) -> dict:
    session = _session(request)
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return session


def _check_csrf(session: dict, token: str) -> None:
    if not secrets.compare_digest(session.get("csrf", ""), token):
        raise HTTPException(status_code=403, detail="Sessão inválida. Recarregue a página.")


def _page(request: Request, template: str, session: dict | None = None, **context):
    return templates.TemplateResponse(request, template, {"request": request, "session": session, **context})


def _redirect(path: str, notice: str = "") -> RedirectResponse:
    if notice:
        path += ("&" if "?" in path else "?") + urlencode({"notice": notice})
    return RedirectResponse(path, status_code=303)


@router.get("/")
def root() -> RedirectResponse:
    return _redirect("/admin")


@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _session(request):
        return _redirect("/admin")
    return _page(request, "login.html", error="")


@router.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host
    now = time.monotonic()
    with login_lock:
        recent = [attempt for attempt in login_attempts.get(client_ip, []) if now - attempt < 900]
        login_attempts[client_ip] = recent
        if len(recent) >= 5:
            raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
    encoded = get_settings().admin_password_hash_b64
    if not encoded:
        raise HTTPException(status_code=503, detail="Senha administrativa não configurada")
    try:
        expected = base64.b64decode(encoded).decode()
        valid = password_hasher.verify(expected, password)
    except VerifyMismatchError:
        valid = False
    except Exception:
        valid = False
    if not valid:
        with login_lock:
            login_attempts[client_ip].append(now)
        return _page(request, "login.html", error="Senha incorreta." )
    with login_lock:
        login_attempts.pop(client_ip, None)
    session = {"role": "admin", "csrf": secrets.token_urlsafe(24)}
    response = _redirect("/admin")
    response.set_cookie(
        "finbot_session",
        _serializer().dumps(session),
        max_age=8 * 60 * 60,
        httponly=True,
        secure=get_settings().session_secure,
        samesite="lax",
    )
    return response


@router.post("/admin/logout")
def logout(session: dict = Depends(require_admin), csrf: str = Form(...)):
    _check_csrf(session, csrf)
    response = _redirect("/admin/login")
    response.delete_cookie("finbot_session")
    return response


@router.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, session: dict = Depends(require_admin)):
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    month_start = date(today.year, today.month, 1)
    month_end = month_start + relativedelta(months=1)
    with SessionLocal() as db:
        chats_enabled = db.scalar(select(func.count()).select_from(Chat).where(Chat.enabled.is_(True))) or 0
        chats_pending = db.scalar(select(func.count()).select_from(Chat).where(Chat.enabled.is_(False))) or 0
        ledgers = list(db.scalars(select(Ledger).order_by(Ledger.name)))
        entries = list(db.scalars(select(Entry).where(
            Entry.occurred_on >= month_start,
            Entry.occurred_on < month_end,
            Entry.deleted_at.is_(None),
        )))
        failed_inbox = db.scalar(select(func.count()).select_from(Inbox).where(Inbox.state == "failed")) or 0
        failed_outbox = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.state == "failed")) or 0
    income = sum(e.amount_cents for e in entries if e.kind == "income" and e.status == "realized")
    expense = sum(e.amount_cents for e in entries if e.kind == "expense" and e.status == "realized")
    planned_income = sum(e.amount_cents for e in entries if e.status == "planned" and e.kind == "income")
    planned_expense = sum(e.amount_cents for e in entries if e.status == "planned" and e.kind == "expense")
    return _page(request, "dashboard.html", session, active="dashboard", month=today.strftime("%m/%Y"),
                 chats_enabled=chats_enabled, chats_pending=chats_pending, ledgers=ledgers,
                 income=income, expense=expense, planned_income=planned_income,
                 planned_expense=planned_expense, failed=failed_inbox + failed_outbox)


@router.get("/admin/chats", response_class=HTMLResponse)
def chats_page(request: Request, session: dict = Depends(require_admin)):
    with SessionLocal() as db:
        chats = list(db.scalars(select(Chat).order_by(Chat.enabled, Chat.last_seen_at.desc())))
    return _page(request, "chats.html", session, active="chats", chats=chats, notice=request.query_params.get("notice", ""))


@router.post("/admin/chats")
def save_chat(session: dict = Depends(require_admin), csrf: str = Form(...), jid: str = Form(...),
              name: str = Form(""), enabled: bool = Form(False), owner_private: bool = Form(False)):
    _check_csrf(session, csrf)
    jid = jid.strip()
    if not jid.endswith(("@s.whatsapp.net", "@lid", "@g.us")) or len(jid) > 160:
        raise HTTPException(status_code=400, detail="JID inválido")
    if owner_private and not jid.endswith("@g.us"):
        raise HTTPException(status_code=400, detail="Somente grupos podem ser privados do proprietário")
    with SessionLocal() as db:
        chat = db.get(Chat, jid)
        if not chat:
            chat = Chat(jid=jid, kind="group" if jid.endswith("@g.us") else "private")
            db.add(chat)
        chat.name = name.strip()[:160] or jid
        chat.enabled = enabled
        chat.owner_private = owner_private and enabled
        db.commit()
    return _redirect("/admin/chats", "Conversa salva")


@router.get("/admin/entries", response_class=HTMLResponse)
def entries_page(request: Request, ledger_id: int | None = None, page: int = 1,
                 session: dict = Depends(require_admin)):
    page = max(1, page)
    with SessionLocal() as db:
        ledgers = list(db.scalars(select(Ledger).order_by(Ledger.name)))
        selected = ledger_id or (ledgers[0].id if ledgers else None)
        total = (db.scalar(select(func.count()).select_from(Entry).where(
            Entry.ledger_id == selected, Entry.deleted_at.is_(None))) or 0) if selected else 0
        entries = list(db.scalars(select(Entry).where(Entry.ledger_id == selected, Entry.deleted_at.is_(None))
                                  .order_by(Entry.occurred_on.desc(), Entry.id.desc()).offset((page - 1) * 100).limit(100))) if selected else []
        categories = {kind: category_names(db, kind) for kind in ("expense", "income")}
    return _page(request, "entries.html", session, active="entries", ledgers=ledgers, selected=selected,
                 entries=entries, categories=categories, page=page, has_next=page * 100 < total,
                 notice=request.query_params.get("notice", ""))


@router.post("/admin/entries/{entry_id}")
def edit_entry(entry_id: int, session: dict = Depends(require_admin), csrf: str = Form(...),
               amount: str = Form(...), description: str = Form(...), category: str = Form(...),
               occurred_on: str = Form(...), status: str = Form(...)):
    _check_csrf(session, csrf)
    with SessionLocal() as db:
        entry = db.get(Entry, entry_id)
        if not entry or entry.deleted_at:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado")
        if category != entry.category and category not in category_names(db, entry.kind):
            raise HTTPException(status_code=400, detail="Categoria inválida")
        if status not in {"realized", "planned"}:
            raise HTTPException(status_code=400, detail="Status inválido")
        if not 1 <= len(description.strip()) <= 240:
            raise HTTPException(status_code=400, detail="Descrição inválida")
        previous = {"amount_cents": entry.amount_cents, "description": entry.description,
                    "category": entry.category, "occurred_on": entry.occurred_on.isoformat(), "status": entry.status}
        entry.amount_cents = money_to_cents(amount)
        entry.description = description.strip()
        entry.category = category
        entry.occurred_on = parse_date(occurred_on, entry.occurred_on)
        entry.status = status
        current = {"amount_cents": entry.amount_cents, "description": entry.description,
                   "category": entry.category, "occurred_on": entry.occurred_on.isoformat(), "status": entry.status}
        audit_change(db, "admin", "edit", entry, {"source": "panel", "before": previous, "after": current})
        ledger_id = entry.ledger_id
        db.commit()
    return _redirect(f"/admin/entries?ledger_id={ledger_id}", "Lançamento atualizado")


@router.post("/admin/entries/{entry_id}/delete")
def delete_entry(entry_id: int, session: dict = Depends(require_admin), csrf: str = Form(...)):
    _check_csrf(session, csrf)
    with SessionLocal() as db:
        entry = db.get(Entry, entry_id)
        if not entry or entry.deleted_at:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado")
        entry.deleted_at = utcnow()
        audit_change(db, "admin", "delete", entry, {"source": "panel"})
        ledger_id = entry.ledger_id
        db.commit()
    return _redirect(f"/admin/entries?ledger_id={ledger_id}", "Lançamento excluído")


@router.get("/admin/categories", response_class=HTMLResponse)
def categories_page(request: Request, session: dict = Depends(require_admin)):
    with SessionLocal() as db:
        categories = list(db.scalars(select(Category).order_by(Category.kind, Category.name)))
    return _page(request, "categories.html", session, active="categories", categories=categories,
                 notice=request.query_params.get("notice", ""))


@router.post("/admin/categories")
def add_category(session: dict = Depends(require_admin), csrf: str = Form(...),
                 name: str = Form(...), kind: str = Form(...)):
    _check_csrf(session, csrf)
    name = name.strip()
    if kind not in {"expense", "income"} or not 1 <= len(name) <= 80:
        raise HTTPException(status_code=400, detail="Categoria inválida")
    with SessionLocal() as db:
        existing = db.scalar(select(Category).where(Category.name == name, Category.kind == kind))
        if existing:
            existing.active = True
        else:
            db.add(Category(name=name, kind=kind))
        db.commit()
    return _redirect("/admin/categories", "Categoria salva")


@router.post("/admin/categories/{category_id}/toggle")
def toggle_category(category_id: int, session: dict = Depends(require_admin), csrf: str = Form(...)):
    _check_csrf(session, csrf)
    with SessionLocal() as db:
        category = db.get(Category, category_id)
        if not category or category.name == "Outros":
            raise HTTPException(status_code=400, detail="Categoria não pode ser alterada")
        category.active = not category.active
        db.commit()
    return _redirect("/admin/categories", "Categoria atualizada")


@router.get("/admin/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, session: dict = Depends(require_admin)):
    with SessionLocal() as db:
        schedules = list(db.scalars(select(Schedule).order_by(Schedule.active.desc(), Schedule.id.desc()).limit(100)))
        ledger_names = {ledger.id: ledger.name for ledger in db.scalars(select(Ledger))}
    return _page(request, "schedules.html", session, active="schedules", schedules=schedules,
                 ledger_names=ledger_names, notice=request.query_params.get("notice", ""))


@router.post("/admin/schedules/{schedule_id}/cancel")
def cancel_schedule(schedule_id: int, session: dict = Depends(require_admin), csrf: str = Form(...)):
    _check_csrf(session, csrf)
    with SessionLocal() as db:
        schedule = db.get(Schedule, schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail="Recorrência não encontrada")
        schedule.active = False
        for entry in db.scalars(select(Entry).where(Entry.schedule_id == schedule.id, Entry.status == "planned", Entry.deleted_at.is_(None))):
            entry.deleted_at = utcnow()
        db.commit()
    return _redirect("/admin/schedules", "Recorrência cancelada")


@router.get("/admin/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: dict = Depends(require_admin)):
    with SessionLocal() as db:
        inbox = list(db.scalars(select(Inbox).where(Inbox.state == "failed").order_by(Inbox.id.desc()).limit(50)))
        outbox = list(db.scalars(select(Outbox).where(Outbox.state == "failed").order_by(Outbox.id.desc()).limit(50)))
    return _page(request, "jobs.html", session, active="jobs", inbox=inbox, outbox=outbox,
                 notice=request.query_params.get("notice", ""))


@router.post("/admin/jobs/{kind}/{job_id}/retry")
def retry_job(kind: str, job_id: int, session: dict = Depends(require_admin), csrf: str = Form(...)):
    _check_csrf(session, csrf)
    model = {"inbox": Inbox, "outbox": Outbox}.get(kind)
    if model is None:
        raise HTTPException(status_code=404, detail="Fila não encontrada")
    with SessionLocal() as db:
        job = db.get(model, job_id)
        if not job or job.state != "failed":
            raise HTTPException(status_code=404, detail="Falha não encontrada")
        job.state = "pending"
        job.attempts = 0
        job.error = None
        job.next_attempt_at = utcnow()
        db.commit()
    return _redirect("/admin/jobs", "Tentativa agendada")
