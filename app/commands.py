import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import parse_report_query, parse_transaction
from app.config import get_settings
from app.finance import (
    audit_change,
    category_names,
    create_entries,
    filtered_report,
    format_money,
    get_entry,
    get_or_create_ledger,
    money_to_cents,
    parse_date,
    recent_entries,
    report,
)
from app.models import Chat, Entry, Schedule, utcnow


HELP = (
    "Comandos do FinBot:\n"
    "/fin gastei 35 no almoço\n"
    "/fin recebi 2500 de salário\n"
    "/fin comprei notebook por 1200 em 6x\n"
    "/fin pago aluguel de 1000 todo mês\n"
    "/fin relatorio [MM/AAAA]\n"
    "/fin quanto gastei com alimentação no mês passado\n"
    "/fin listar\n"
    "/fin confirmar ID\n"
    "/fin editar ID valor|descricao|categoria|data NOVO_VALOR\n"
    "/fin excluir ID\n"
    "/fin cancelar recorrencia ID"
)


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _message_date(data: dict) -> date:
    timestamp = data.get("messageTimestamp")
    try:
        if isinstance(timestamp, dict):
            timestamp = timestamp.get("low")
        return datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(ZoneInfo(get_settings().timezone)).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return _today()


def _format_entry(entry: Entry) -> str:
    kind = "Receita" if entry.kind == "income" else "Despesa"
    status = "previsto" if entry.status == "planned" else "realizado"
    suffix = f" ({entry.installment_number}/{entry.installment_count})" if entry.installment_count else ""
    return f"#{entry.id} {entry.occurred_on:%d/%m/%Y} · {kind} {format_money(entry.amount_cents)} · {entry.description}{suffix} · {entry.category} · {status}"


def _parse_report_month(raw: str, today: date) -> tuple[int, int]:
    value = raw.strip().lower()
    if not value or value in {"este mes", "esse mes", "este mês", "esse mês"}:
        return today.year, today.month
    if value in {"mes passado", "mês passado"}:
        previous = today - relativedelta(months=1)
        return previous.year, previous.month
    match = re.fullmatch(r"(0?[1-9]|1[0-2])/(20\d{2})", value)
    if not match:
        raise ValueError("Use /fin relatorio MM/AAAA")
    return int(match.group(2)), int(match.group(1))


def _authorized_context(db: Session, payload: dict) -> tuple[Chat | None, str, str, str] | None:
    data = payload.get("data") or {}
    key = data.get("key") or {}
    chat_jid = key.get("remoteJid") or ""
    from_me = bool(key.get("fromMe"))
    settings = get_settings()
    owner_jid = settings.owner_jid
    owner_self_jids = {jid for jid in (owner_jid, settings.owner_self_jid) if jid}
    if not chat_jid or chat_jid.endswith("@broadcast"):
        return None
    is_group = chat_jid.endswith("@g.us")
    chat = db.get(Chat, chat_jid)
    if chat is None:
        chat = Chat(jid=chat_jid, kind="group" if is_group else "private", name=chat_jid, enabled=False)
        db.add(chat)
    chat.last_seen_at = utcnow()
    if is_group:
        sender = owner_jid if from_me else (key.get("participant") or data.get("participant") or "")
        if not sender:
            return None
    elif chat_jid in owner_self_jids:
        sender = owner_jid
    elif from_me:
        return None
    else:
        sender = chat_jid

    if chat_jid in owner_self_jids:
        return chat, sender, owner_jid, "owner"
    if not chat.enabled:
        return None
    if is_group and chat.owner_private:
        if sender != owner_jid:
            return None
        return chat, sender, owner_jid, "owner"
    return chat, sender, chat_jid, "group" if is_group else "private"


def process_command(db: Session, payload: dict) -> tuple[str, str] | None:
    data = payload.get("data") or {}
    key = data.get("key") or {}
    message = data.get("message") or {}
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        if wrapper in message:
            message = (message[wrapper] or {}).get("message") or {}
    text = message.get("conversation") or (message.get("extendedTextMessage") or {}).get("text") or ""
    if not isinstance(text, str) or not re.match(r"^/fin(?:\s|$)", text.strip(), re.IGNORECASE):
        return None

    context = _authorized_context(db, payload)
    if not context:
        return None
    _, sender, scope_jid, ledger_kind = context
    chat_jid = key["remoteJid"]
    ledger_name = "Meu controle" if ledger_kind == "owner" else chat_jid
    ledger = get_or_create_ledger(db, scope_jid, ledger_kind, ledger_name)
    body = re.sub(r"^/fin\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    today = _message_date(data)
    lower = body.lower()
    if not body or lower in {"ajuda", "help"}:
        return chat_jid, HELP
    if lower.startswith("relatorio") or lower.startswith("relatório"):
        period = body.split(maxsplit=1)[1] if " " in body else ""
        try:
            year, month = _parse_report_month(period, today)
            return chat_jid, report(db, ledger.id, year, month)
        except ValueError:
            pass
    if lower.startswith(("quanto ", "quais ", "saldo ", "resumo ", "despesas ", "receitas ")) or lower in {"saldo", "resumo", "despesas", "receitas"} or lower.startswith(("relatorio ", "relatório ")):
        categories = {kind: category_names(db, kind) for kind in ("expense", "income")}
        query = parse_report_query(body, today, categories)
        start = parse_date(query.start_date, today)
        end = parse_date(query.end_date, today)
        chosen_category = None
        if query.category:
            chosen_category = next((name for name in categories["expense"] + categories["income"]
                                    if name.casefold() == query.category.casefold()), None)
            if not chosen_category:
                raise ValueError("Categoria não encontrada. Consulte as categorias no painel.")
        return chat_jid, filtered_report(db, ledger.id, start, end, query.kind, chosen_category, query.status)
    if lower == "listar":
        entries = recent_entries(db, ledger.id)
        return chat_jid, "Últimos lançamentos:\n" + "\n".join(_format_entry(e) for e in entries) if entries else "Nenhum lançamento nesta conta."
    match = re.fullmatch(r"confirmar\s+(\d+)", lower)
    if match:
        entry = get_entry(db, ledger.id, int(match.group(1)))
        before = entry.status
        entry.status = "realized"
        audit_change(db, sender, "confirm", entry, {"before": before, "after": entry.status})
        return chat_jid, "Confirmado: " + _format_entry(entry)
    match = re.fullmatch(r"excluir\s+(\d+)", lower)
    if match:
        entry = get_entry(db, ledger.id, int(match.group(1)))
        entry.deleted_at = utcnow()
        audit_change(db, sender, "delete", entry, {})
        return chat_jid, f"Lançamento #{entry.id} excluído."
    match = re.fullmatch(r"cancelar\s+recorr[eê]ncia\s+(\d+)", lower)
    if match:
        entry = get_entry(db, ledger.id, int(match.group(1)))
        if not entry.schedule_id:
            raise ValueError("Esse lançamento não é recorrente")
        schedule = db.get(Schedule, entry.schedule_id)
        schedule.active = False
        for future in db.scalars(select(Entry).where(Entry.schedule_id == schedule.id, Entry.status == "planned", Entry.deleted_at.is_(None))):
            future.deleted_at = utcnow()
        audit_change(db, sender, "cancel_recurrence", entry, {"schedule_id": schedule.id})
        return chat_jid, f"Recorrência #{schedule.id} cancelada. Lançamentos realizados foram mantidos."
    match = re.fullmatch(r"editar\s+(\d+)\s+(valor|descricao|descrição|categoria|data)\s+(.+)", body, re.IGNORECASE)
    if match:
        entry = get_entry(db, ledger.id, int(match.group(1)))
        previous = {"amount_cents": entry.amount_cents, "description": entry.description,
                    "category": entry.category, "occurred_on": entry.occurred_on.isoformat()}
        field = match.group(2).lower()
        value = match.group(3).strip()
        if field == "valor":
            entry.amount_cents = money_to_cents(value)
        elif field in {"descricao", "descrição"}:
            if not 1 <= len(value) <= 240:
                raise ValueError("Descrição deve ter até 240 caracteres")
            entry.description = value
        elif field == "categoria":
            names = category_names(db, entry.kind)
            chosen = next((name for name in names if name.casefold() == value.casefold()), None)
            if not chosen:
                raise ValueError("Categoria disponível: " + ", ".join(names))
            entry.category = chosen
        else:
            entry.occurred_on = parse_date(value, today)
        current = {"amount_cents": entry.amount_cents, "description": entry.description,
                   "category": entry.category, "occurred_on": entry.occurred_on.isoformat()}
        audit_change(db, sender, "edit", entry, {"field": field, "before": previous, "after": current})
        return chat_jid, "Atualizado: " + _format_entry(entry)

    categories = {kind: category_names(db, kind) for kind in ("expense", "income")}
    draft = parse_transaction(body, today, categories)
    if not draft.amount or not draft.description:
        raise ValueError("Informe valor e descrição. Exemplo: /fin gastei 35 no almoço")
    if draft.kind not in {"expense", "income"}:
        raise ValueError("Não identifiquei se é receita ou despesa. Escreva, por exemplo, 'gastei' ou 'recebi'.")
    kind = draft.kind
    category = next((name for name in categories[kind] if name.casefold() == (draft.category or "").casefold()), "Outros")
    entries = create_entries(
        db,
        ledger=ledger,
        creator_jid=sender,
        kind=kind,
        amount_cents=money_to_cents(draft.amount),
        description=draft.description.strip(),
        category=category,
        occurred_on=parse_date(draft.date, today),
        installment_count=draft.installments,
        recurrence_frequency=draft.recurrence_frequency,
        recurrence_count=draft.recurrence_count,
        source_instance=payload.get("instance") or get_settings().evolution_instance,
        source_message_id=key["id"],
        today=today,
    )
    first = entries[0]
    if draft.recurrence_frequency:
        note = f" Recorrência {draft.recurrence_frequency} criada; próximas ocorrências ficam previstas."
    elif len(entries) > 1:
        note = f" Compra dividida em {len(entries)} parcelas mensais; primeira em {first.occurred_on:%d/%m/%Y}."
    else:
        note = ""
    return chat_jid, "Registrado: " + _format_entry(first) + note
