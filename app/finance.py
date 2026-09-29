from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Audit, Category, Entry, Ledger, Schedule


DEFAULT_CATEGORIES = {
    "expense": ["Alimentação", "Moradia", "Transporte", "Saúde", "Educação", "Lazer", "Compras", "Serviços", "Impostos", "Outros"],
    "income": ["Salário", "Freelance", "Vendas", "Investimentos", "Presentes", "Outros"],
}


def seed_categories(db: Session) -> None:
    if db.scalar(select(Category.id).limit(1)) is not None:
        return
    for kind, names in DEFAULT_CATEGORIES.items():
        for name in names:
            db.add(Category(name=name, kind=kind))
    db.commit()


def category_names(db: Session, kind: str) -> list[str]:
    return list(db.scalars(select(Category.name).where(Category.kind == kind, Category.active.is_(True)).order_by(Category.name)))


def money_to_cents(raw: str | int | float) -> int:
    value = str(raw).strip().replace("R$", "").replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):
        value = value.replace(".", "")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Valor inválido") from exc
    cents = int((decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents <= 0 or cents > 2_000_000_000:
        raise ValueError("Informe um valor maior que zero e menor que R$ 20 milhões")
    return cents


def format_money(cents: int) -> str:
    value = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if cents < 0 else ''}R$ {value}"


def parse_date(raw: str | None, default: date) -> date:
    if not raw:
        return default
    raw = raw.strip()
    try:
        if "/" in raw:
            return datetime.strptime(raw, "%d/%m/%Y").date()
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Data inválida. Use DD/MM/AAAA") from exc


def get_or_create_ledger(db: Session, scope_jid: str, kind: str, name: str) -> Ledger:
    ledger = db.scalar(select(Ledger).where(Ledger.scope_jid == scope_jid))
    if ledger:
        return ledger
    ledger = Ledger(scope_jid=scope_jid, kind=kind, name=name)
    db.add(ledger)
    db.flush()
    return ledger


def _occurrence_date(start: date, frequency: str, index: int) -> date:
    if frequency == "daily":
        return start + timedelta(days=index)
    if frequency == "weekly":
        return start + timedelta(weeks=index)
    if frequency == "monthly":
        return start + relativedelta(months=index)
    if frequency == "yearly":
        return start + relativedelta(years=index)
    raise ValueError("Recorrência deve ser diária, semanal, mensal ou anual")


def expand_schedule(db: Session, schedule: Schedule, through: date) -> int:
    existing = set(db.scalars(select(Entry.occurrence_index).where(Entry.schedule_id == schedule.id)))
    created = 0
    index = 0
    while schedule.active and (schedule.count is None or index < schedule.count):
        current = _occurrence_date(schedule.starts_on, schedule.frequency, index)
        if current > through:
            break
        if index not in existing:
            db.add(Entry(
                ledger_id=schedule.ledger_id,
                creator_jid=schedule.creator_jid,
                kind=schedule.kind,
                amount_cents=schedule.amount_cents,
                description=schedule.description,
                category=schedule.category,
                occurred_on=current,
                status="planned",
                schedule_id=schedule.id,
                occurrence_index=index,
            ))
            created += 1
        index += 1
        if index > 5000:
            break
    return created


def create_entries(
    db: Session,
    *,
    ledger: Ledger,
    creator_jid: str,
    kind: str,
    amount_cents: int,
    description: str,
    category: str,
    occurred_on: date,
    installment_count: int,
    recurrence_frequency: str | None,
    recurrence_count: int | None,
    source_instance: str,
    source_message_id: str,
    today: date,
) -> list[Entry]:
    if kind not in {"expense", "income"}:
        raise ValueError("Tipo de lançamento inválido")
    if category not in category_names(db, kind):
        raise ValueError("Categoria inválida ou desativada")
    if not description or len(description) > 240:
        raise ValueError("Descrição deve ter até 240 caracteres")
    if installment_count < 1 or installment_count > 120:
        raise ValueError("Use entre 1 e 120 parcelas")
    if amount_cents < installment_count:
        raise ValueError("O valor total precisa ser de ao menos R$ 0,01 por parcela")
    if recurrence_frequency and installment_count > 1:
        raise ValueError("Parcelamento e recorrência devem ser lançados separadamente")
    if recurrence_count is not None and not 1 <= recurrence_count <= 5000:
        raise ValueError("Quantidade de recorrências inválida")

    if recurrence_frequency:
        schedule = Schedule(
            ledger_id=ledger.id,
            creator_jid=creator_jid,
            kind=kind,
            amount_cents=amount_cents,
            description=description,
            category=category,
            starts_on=occurred_on,
            frequency=recurrence_frequency,
            count=recurrence_count,
        )
        db.add(schedule)
        db.flush()
        through = today + relativedelta(months=12)
        expand_schedule(db, schedule, through)
        db.flush()
        entries = list(db.scalars(select(Entry).where(Entry.schedule_id == schedule.id).order_by(Entry.occurred_on)))
        first = entries[0]
        if first.occurred_on <= today:
            first.status = "realized"
        first.source_instance = source_instance
        first.source_message_id = source_message_id
        first.item_index = 0
    else:
        group_id = uuid4().hex if installment_count > 1 else None
        base = amount_cents // installment_count
        remainder = amount_cents % installment_count
        entries = []
        for index in range(installment_count):
            due = occurred_on + relativedelta(months=index)
            entry = Entry(
                ledger_id=ledger.id,
                creator_jid=creator_jid,
                kind=kind,
                amount_cents=base + (1 if index < remainder else 0),
                description=description,
                category=category,
                occurred_on=due,
                status="realized" if due <= today else "planned",
                source_instance=source_instance,
                source_message_id=source_message_id,
                item_index=index,
                installment_group=group_id,
                installment_number=index + 1 if group_id else None,
                installment_count=installment_count if group_id else None,
            )
            db.add(entry)
            entries.append(entry)
        db.flush()
    for entry in entries[:1]:
        db.add(Audit(actor_jid=creator_jid, action="create", entry_id=entry.id, detail={"count": len(entries)}))
    return entries


def report(db: Session, ledger_id: int, year: int, month: int) -> str:
    if not 1 <= month <= 12 or not 2000 <= year <= date.today().year + 2:
        raise ValueError("Mês inválido")
    start = date(year, month, 1)
    end = start + relativedelta(months=1)
    for schedule in db.scalars(select(Schedule).where(Schedule.ledger_id == ledger_id, Schedule.active.is_(True))):
        expand_schedule(db, schedule, end - timedelta(days=1))
    db.flush()
    entries = list(db.scalars(select(Entry).where(
        Entry.ledger_id == ledger_id,
        Entry.occurred_on >= start,
        Entry.occurred_on < end,
        Entry.deleted_at.is_(None),
    )))
    totals = defaultdict(int)
    categories = defaultdict(int)
    for entry in entries:
        totals[(entry.status, entry.kind)] += entry.amount_cents
        if entry.status == "realized" and entry.kind == "expense":
            categories[entry.category] += entry.amount_cents
    income = totals["realized", "income"]
    expense = totals["realized", "expense"]
    lines = [
        f"Relatório de {month:02d}/{year}",
        f"Receitas realizadas: {format_money(income)}",
        f"Despesas realizadas: {format_money(expense)}",
        f"Saldo realizado: {format_money(income - expense)}",
        f"Previsto: {format_money(totals['planned', 'income'])} em receitas e {format_money(totals['planned', 'expense'])} em despesas",
    ]
    if categories:
        lines.append("Despesas por categoria:")
        lines.extend(f"• {name}: {format_money(value)}" for name, value in sorted(categories.items(), key=lambda pair: -pair[1]))
    return "\n".join(lines)


def filtered_report(db: Session, ledger_id: int, start: date, end: date,
                    kind: str, category: str | None, status: str) -> str:
    if start > end or start < date(2000, 1, 1) or end > date.today() + relativedelta(years=2) or (end - start).days > 730:
        raise ValueError("Informe um período de até dois anos")
    if kind not in {"expense", "income", "all"} or status not in {"realized", "planned", "both"}:
        raise ValueError("Filtro de relatório inválido")
    for schedule in db.scalars(select(Schedule).where(Schedule.ledger_id == ledger_id, Schedule.active.is_(True))):
        expand_schedule(db, schedule, end)
    db.flush()
    entries = list(db.scalars(select(Entry).where(
        Entry.ledger_id == ledger_id,
        Entry.occurred_on >= start,
        Entry.occurred_on <= end,
        Entry.deleted_at.is_(None),
    )))
    entries = [entry for entry in entries if
               (kind == "all" or entry.kind == kind) and
               (status == "both" or entry.status == status) and
               (category is None or entry.category.casefold() == category.casefold())]
    totals = defaultdict(int)
    categories = defaultdict(int)
    for entry in entries:
        totals[(entry.status, entry.kind)] += entry.amount_cents
        if entry.kind == "expense":
            categories[(entry.status, entry.category)] += entry.amount_cents
    lines = [f"Consulta de {start:%d/%m/%Y} a {end:%d/%m/%Y}"]
    if category:
        lines.append(f"Categoria: {category}")
    if status == "both":
        lines.append("Valores realizados e previstos separados:")
        for item_status, label in (("realized", "Realizado"), ("planned", "Previsto")):
            income = totals[item_status, "income"]
            expense = totals[item_status, "expense"]
            lines.append(f"{label}: receitas {format_money(income)}, despesas {format_money(expense)}, saldo {format_money(income-expense)}")
    else:
        income = totals[status, "income"]
        expense = totals[status, "expense"]
        label = "Previsto" if status == "planned" else "Realizado"
        if kind in {"income", "all"}:
            lines.append(f"Receitas ({label.lower()}): {format_money(income)}")
        if kind in {"expense", "all"}:
            lines.append(f"Despesas ({label.lower()}): {format_money(expense)}")
        if kind == "all":
            lines.append(f"Saldo: {format_money(income-expense)}")
    if kind in {"expense", "all"} and not category and categories:
        lines.append("Despesas por categoria:")
        for (item_status, name), value in sorted(categories.items(), key=lambda pair: -pair[1]):
            label = " · previsto" if item_status == "planned" and status == "both" else " · realizado" if status == "both" else ""
            lines.append(f"• {name}{label}: {format_money(value)}")
    if not entries:
        lines.append("Nenhum lançamento encontrado.")
    return "\n".join(lines)


def recent_entries(db: Session, ledger_id: int, limit: int = 10) -> list[Entry]:
    return list(db.scalars(select(Entry).where(Entry.ledger_id == ledger_id, Entry.deleted_at.is_(None)).order_by(Entry.occurred_on.desc(), Entry.id.desc()).limit(limit)))


def get_entry(db: Session, ledger_id: int, entry_id: int) -> Entry:
    entry = db.get(Entry, entry_id)
    if not entry or entry.ledger_id != ledger_id or entry.deleted_at:
        raise ValueError("Lançamento não encontrado nesta conta")
    return entry


def audit_change(db: Session, actor: str, action: str, entry: Entry, detail: dict) -> None:
    db.add(Audit(actor_jid=actor, action=action, entry_id=entry.id, detail=detail))
