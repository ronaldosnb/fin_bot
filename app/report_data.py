from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ReportEntry:
    id: int
    occurred_on: date
    description: str
    category: str
    amount_cents: int
    kind: str
    status: str
    installment_number: int | None
    installment_count: int | None


@dataclass(frozen=True)
class MonthlyReport:
    ledger_name: str
    year: int
    month: int
    entries: tuple[ReportEntry, ...]

    def total(self, status: str, kind: str) -> int:
        return sum(e.amount_cents for e in self.entries if e.status == status and e.kind == kind)

    def categories(self, status: str, kind: str) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for entry in self.entries:
            if entry.status == status and entry.kind == kind:
                totals[entry.category] = totals.get(entry.category, 0) + entry.amount_cents
        return sorted(totals.items(), key=lambda item: (-item[1], item[0]))
