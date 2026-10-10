from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request  # noqa: F401  (mantém o mesmo estilo dos outros testes)

from app.admin import cancel_schedule
from app.commands import process_command
from app.db import Base
from app.finance import create_entries, seed_categories
from app.models import Audit, Chat, Entry, Ledger, Schedule

OWNER = "owner@s.whatsapp.net"
FRIEND = "5511999990000@s.whatsapp.net"
GROUP = "family@g.us"
SETTINGS = SimpleNamespace(owner_jid=OWNER, owner_self_jid="", timezone="America/Sao_Paulo",
                           evolution_instance="finbot")


def payload(chat, text="/fin ajuda", from_me=False, participant=None, message_id="m1"):
    key = {"remoteJid": chat, "fromMe": from_me, "id": message_id}
    if participant:
        key["participant"] = participant
    return {"instance": "finbot",
            "data": {"key": key, "message": {"conversation": text}, "messageTimestamp": 1781500000}}


class CommandsBase(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.sessions()
        seed_categories(self.db)
        for patch_target in ("app.commands.get_settings", "app.admin.get_settings"):
            p = patch(patch_target, return_value=SETTINGS)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def enable(self, jid, kind="private", owner_private=False):
        self.db.add(Chat(jid=jid, kind=kind, name=jid, enabled=True, owner_private=owner_private))
        self.db.commit()

    def ledger(self, scope):
        return self.db.scalar(select(Ledger).where(Ledger.scope_jid == scope))


class AuthorizationTest(CommandsBase):
    def test_unknown_private_chat_is_ignored_and_registered_pending(self):
        self.assertIsNone(process_command(self.db, payload(FRIEND)))
        self.db.commit()
        chat = self.db.get(Chat, FRIEND)
        self.assertIsNotNone(chat)
        self.assertFalse(chat.enabled)
        self.assertEqual(chat.kind, "private")

    def test_authorized_private_chat_gets_reply(self):
        self.enable(FRIEND)
        response = process_command(self.db, payload(FRIEND))
        self.assertEqual(response.chat_jid, FRIEND)
        self.assertIn("Comandos do FinBot", response.text)

    def test_from_me_in_third_party_private_chat_is_ignored(self):
        self.enable(FRIEND)
        self.assertIsNone(process_command(self.db, payload(FRIEND, from_me=True)))

    def test_owner_own_chat_needs_no_authorization(self):
        response = process_command(self.db, payload(OWNER, from_me=True))
        self.assertIn("Comandos do FinBot", response.text)
        self.assertEqual(self.ledger(OWNER).kind, "owner")

    def test_text_without_fin_is_ignored(self):
        self.enable(FRIEND)
        self.assertIsNone(process_command(self.db, payload(FRIEND, text="oi, tudo bem?")))
        self.assertIsNone(process_command(self.db, payload(FRIEND, text="/finalizar")))

    def test_authorized_group_any_participant_uses_group_ledger(self):
        self.enable(GROUP, kind="group")
        for index, participant in enumerate(("a@s.whatsapp.net", "b@s.whatsapp.net")):
            response = process_command(self.db, payload(
                GROUP, text="/fin listar", participant=participant, message_id=f"g{index}"))
            self.assertEqual(response.chat_jid, GROUP)
        self.assertEqual(self.ledger(GROUP).kind, "group")

    def test_unauthorized_group_is_ignored(self):
        self.assertIsNone(process_command(self.db, payload(GROUP, participant=FRIEND)))

    def test_owner_private_group_only_serves_owner(self):
        self.enable(GROUP, kind="group", owner_private=True)
        self.assertIsNone(process_command(self.db, payload(GROUP, participant=FRIEND)))
        self.assertIsNone(self.ledger(GROUP))
        response = process_command(self.db, payload(GROUP, participant=OWNER))
        self.assertEqual(response.chat_jid, GROUP)
        self.assertEqual(self.ledger(OWNER).kind, "owner")
        self.assertIsNone(self.ledger(GROUP))

    def test_owner_private_group_entry_goes_to_owner_ledger(self):
        self.enable(GROUP, kind="group", owner_private=True)
        draft = SimpleNamespace(amount="35", description="almoço", kind="expense",
                                category="Alimentação", date=None, installments=1,
                                recurrence_frequency=None, recurrence_count=None)
        with patch("app.commands.parse_transaction", return_value=draft):
            process_command(self.db, payload(GROUP, text="/fin gastei 35", participant=OWNER))
        entry = self.db.scalar(select(Entry))
        self.assertEqual(entry.ledger_id, self.ledger(OWNER).id)


class FlowTest(CommandsBase):
    def setUp(self):
        super().setUp()
        self.enable(FRIEND)

    def test_mocked_transaction_creates_entry_in_right_ledger(self):
        draft = SimpleNamespace(amount="35,50", description="almoço", kind="expense",
                                category="Alimentação", date=None, installments=1,
                                recurrence_frequency=None, recurrence_count=None)
        with patch("app.commands.parse_transaction", return_value=draft) as mocked:
            response = process_command(self.db, payload(FRIEND, text="/fin gastei 35,50 no almoço"))
        mocked.assert_called_once()
        self.assertIn("Registrado", response.text)
        entry = self.db.scalar(select(Entry))
        self.assertEqual(entry.amount_cents, 3550)
        self.assertEqual(entry.category, "Alimentação")
        self.assertEqual(entry.creator_jid, FRIEND)
        self.assertEqual(entry.ledger_id, self.ledger(FRIEND).id)

    def test_list_shows_entry(self):
        draft = SimpleNamespace(amount="10", description="café", kind="expense",
                                category="Alimentação", date=None, installments=1,
                                recurrence_frequency=None, recurrence_count=None)
        with patch("app.commands.parse_transaction", return_value=draft):
            process_command(self.db, payload(FRIEND, text="/fin gastei 10 café"))
        response = process_command(self.db, payload(FRIEND, text="/fin listar", message_id="m2"))
        self.assertIn("café", response.text)

    def other_account_entry(self):
        other = Ledger(scope_jid="other@s.whatsapp.net", kind="private", name="other")
        self.db.add(other)
        self.db.flush()
        return create_entries(
            self.db, ledger=other, creator_jid="other@s.whatsapp.net", kind="expense",
            amount_cents=1000, description="alheio", category="Outros",
            occurred_on=date(2026, 6, 1), installment_count=1, recurrence_frequency=None,
            recurrence_count=None, source_instance="finbot", source_message_id="x",
            today=date(2026, 6, 15))[0]

    def test_edit_and_delete_of_other_account_entry_raise(self):
        entry = self.other_account_entry()
        for text in (f"/fin editar {entry.id} valor 50", f"/fin excluir {entry.id}",
                     f"/fin confirmar {entry.id}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                process_command(self.db, payload(FRIEND, text=text))
        self.assertIsNone(entry.deleted_at)
        self.assertEqual(entry.amount_cents, 1000)


class PanelCancelScheduleTest(CommandsBase):
    def test_cancel_schedule_records_audit(self):
        ledger = Ledger(scope_jid=FRIEND, kind="private", name="x")
        self.db.add(ledger)
        self.db.flush()
        create_entries(
            self.db, ledger=ledger, creator_jid=FRIEND, kind="expense", amount_cents=1000,
            description="aluguel", category="Moradia", occurred_on=date(2026, 6, 1),
            installment_count=1, recurrence_frequency="monthly", recurrence_count=3,
            source_instance="finbot", source_message_id="r", today=date(2026, 6, 15))
        self.db.commit()
        schedule_id = self.db.scalar(select(Schedule.id))
        with patch("app.admin.SessionLocal", self.sessions):
            cancel_schedule(schedule_id, session={"csrf": "t"}, csrf="t")
        self.db.expire_all()
        self.assertFalse(self.db.get(Schedule, schedule_id).active)
        planned = list(self.db.scalars(select(Entry).where(Entry.status == "planned")))
        self.assertTrue(planned and all(e.deleted_at for e in planned))
        audit = [a for a in self.db.scalars(select(Audit)) if a.action == "cancel_recurrence"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].actor_jid, "admin")
        self.assertIsNone(audit[0].entry_id)
        self.assertEqual(audit[0].detail, {"source": "panel", "schedule_id": schedule_id})
