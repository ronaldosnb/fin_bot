from datetime import date
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.finance import (create_entries, expand_schedule, format_money, money_to_cents,
                         parse_date, seed_categories)
from app.models import Entry, Ledger, Schedule


class MoneyTest(TestCase):
    def test_money_to_cents_valid_formats(self):
        for raw, expected in [("35", 3500), ("42,50", 4250), ("1.200,00", 120000),
                              ("1.200", 120000), ("R$ 10", 1000), (10, 1000), (1.5, 150)]:
            with self.subTest(raw=raw):
                self.assertEqual(money_to_cents(raw), expected)

    def test_money_to_cents_rejects_invalid_values(self):
        for raw in ["abc", "", "R$", "0", "0,00", "-5", "-1,50", "20000000,01", "30000000"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                money_to_cents(raw)

    def test_money_to_cents_limit_is_inclusive(self):
        self.assertEqual(money_to_cents("20000000"), 2_000_000_000)

    def test_format_money(self):
        self.assertEqual(format_money(0), "R$ 0,00")
        self.assertEqual(format_money(3550), "R$ 35,50")
        self.assertEqual(format_money(123456789), "R$ 1.234.567,89")
        self.assertEqual(format_money(-120000), "-R$ 1.200,00")


class ParseDateTest(TestCase):
    default = date(2026, 1, 2)

    def test_formats(self):
        self.assertEqual(parse_date("05/03/2026", self.default), date(2026, 3, 5))
        self.assertEqual(parse_date("2026-03-05", self.default), date(2026, 3, 5))

    def test_empty_returns_default(self):
        self.assertEqual(parse_date("", self.default), self.default)
        self.assertEqual(parse_date(None, self.default), self.default)

    def test_invalid(self):
        for raw in ["31/02/2026", "ontem", "2026-13-01"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_date(raw, self.default)


class DbCase(TestCase):
    today = date(2026, 6, 15)

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        seed_categories(self.db)
        self.ledger = Ledger(scope_jid="a@s.whatsapp.net", kind="private", name="a")
        self.db.add(self.ledger)
        self.db.flush()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def create(self, **overrides):
        args = dict(ledger=self.ledger, creator_jid="a@s.whatsapp.net", kind="expense",
                    amount_cents=3500, description="Almoço", category="Alimentação",
                    occurred_on=self.today, installment_count=1, recurrence_frequency=None,
                    recurrence_count=None, source_instance="finbot", source_message_id="m1",
                    today=self.today)
        args.update(overrides)
        return create_entries(self.db, **args)


class EntriesTest(DbCase):
    def test_simple_entry(self):
        entries = self.create()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].amount_cents, 3500)
        self.assertEqual(entries[0].status, "realized")
        self.assertIsNone(entries[0].installment_group)

    def test_installments_split_remainder_to_first(self):
        entries = self.create(amount_cents=10000, installment_count=3)
        self.assertEqual([e.amount_cents for e in entries], [3334, 3333, 3333])
        self.assertEqual(sum(e.amount_cents for e in entries), 10000)
        self.assertEqual([e.installment_number for e in entries], [1, 2, 3])
        self.assertEqual(len({e.installment_group for e in entries}), 1)
        self.assertEqual([e.occurred_on for e in entries],
                         [date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)])

    def test_installment_status_by_due_date(self):
        entries = self.create(amount_cents=30000, installment_count=3,
                              occurred_on=date(2026, 5, 15))
        self.assertEqual([e.status for e in entries], ["realized", "realized", "planned"])

    def test_monthly_recurrence(self):
        entries = self.create(recurrence_frequency="monthly", recurrence_count=3,
                              occurred_on=date(2026, 5, 15))
        schedules = list(self.db.scalars(select(Schedule)))
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].frequency, "monthly")
        all_entries = list(self.db.scalars(select(Entry).order_by(Entry.occurred_on)))
        self.assertEqual(len(all_entries), 3)
        self.assertEqual([e.status for e in all_entries], ["realized", "planned", "planned"])
        self.assertEqual(entries[0].id, all_entries[0].id)
        self.assertEqual(all_entries[0].source_message_id, "m1")

    def test_recurrence_first_in_future_stays_planned(self):
        self.create(recurrence_frequency="monthly", recurrence_count=2,
                    occurred_on=date(2026, 7, 1))
        statuses = {e.status for e in self.db.scalars(select(Entry))}
        self.assertEqual(statuses, {"planned"})

    def test_validation_errors(self):
        cases = [dict(installment_count=0), dict(installment_count=121),
                 dict(installment_count=2, recurrence_frequency="monthly"),
                 dict(category="Inexistente"), dict(kind="transfer"),
                 dict(description=""), dict(amount_cents=2, installment_count=3),
                 dict(recurrence_frequency="monthly", recurrence_count=0)]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create(**overrides)
        self.assertEqual(list(self.db.scalars(select(Entry))), [])

    def test_installments_up_to_limit(self):
        self.assertEqual(len(self.create(amount_cents=12000, installment_count=120)), 120)


class ExpandScheduleTest(DbCase):
    def make_schedule(self, count):
        schedule = Schedule(ledger_id=self.ledger.id, creator_jid="a", kind="expense",
                            amount_cents=1000, description="Aluguel", category="Moradia",
                            starts_on=date(2026, 1, 1), frequency="monthly", count=count)
        self.db.add(schedule)
        self.db.flush()
        return schedule

    def test_idempotent(self):
        schedule = self.make_schedule(None)
        first = expand_schedule(self.db, schedule, date(2026, 6, 30))
        self.db.flush()
        self.assertEqual(first, 6)
        self.assertEqual(expand_schedule(self.db, schedule, date(2026, 6, 30)), 0)
        self.db.flush()
        self.assertEqual(len(list(self.db.scalars(select(Entry)))), 6)

    def test_respects_count(self):
        schedule = self.make_schedule(3)
        self.assertEqual(expand_schedule(self.db, schedule, date(2030, 1, 1)), 3)

    def test_inactive_creates_nothing(self):
        schedule = self.make_schedule(3)
        schedule.active = False
        self.assertEqual(expand_schedule(self.db, schedule, date(2030, 1, 1)), 0)
