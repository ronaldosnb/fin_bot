import base64
from datetime import date, datetime, timezone
from importlib import import_module
from unittest import TestCase, main
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import httpx
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.commands import process_command
from app.db import Base
from app.evolution import send_document
from app.finance import monthly_report, report_summary
from app.models import Chat, Entry, Inbox, Ledger, Outbox, Schedule
from app.pdf_report import render_monthly_pdf
from app import worker


class ReportsTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.db.add_all([Ledger(id=1, scope_jid="test@g.us", kind="group", name="Grupo de teste"),
                         Ledger(id=2, scope_jid="other@g.us", kind="group", name="Outra conta"),
                         Chat(jid="test@g.us", kind="group", enabled=True, name="Grupo de teste")])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def entry(self, **changes):
        values = dict(ledger_id=1, creator_jid="test", kind="expense", amount_cents=501,
                      description="Almoço & café <teste>", category="Alimentação",
                      occurred_on=date(2026, 9, 12), status="realized")
        values.update(changes)
        row = Entry(**values)
        self.db.add(row)
        self.db.flush()
        return row

    def payload(self, command="/fin relatorio 09/2026", jid="test@g.us"):
        return {"data": {"key": {"remoteJid": jid, "participant": "tester@s.whatsapp.net"},
                         "messageTimestamp": int(datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()),
                         "message": {"conversation": command}}}

    def test_totals_scope_deleted_and_month_boundaries(self):
        self.entry(kind="income", amount_cents=101)
        self.entry(amount_cents=501)
        self.entry(status="planned", amount_cents=1200)
        self.entry(ledger_id=2, amount_cents=99999)
        self.entry(deleted_at=datetime.now(timezone.utc), amount_cents=99999)
        self.entry(occurred_on=date(2026, 10, 1), amount_cents=99999)
        report = monthly_report(self.db, 1, 2026, 9)
        self.assertEqual(len(report.entries), 3)
        self.assertEqual(report.total("realized", "expense"), 501)
        self.assertEqual(report.categories("realized", "expense"), [("Alimentação", 501)])
        self.assertIn("-R$ 4,00", report_summary(report))
        self.assertEqual(report.total("planned", "expense"), 1200)

    def test_recurring_entries_and_installments(self):
        self.db.add(Schedule(ledger_id=1, creator_jid="test", kind="expense", amount_cents=2000,
                             description="Aluguel", category="Moradia", starts_on=date(2026, 9, 1),
                             frequency="monthly", active=True))
        self.entry(installment_number=2, installment_count=6)
        self.db.flush()
        first = monthly_report(self.db, 1, 2026, 9)
        second = monthly_report(self.db, 1, 2026, 9)
        self.assertEqual(len(first.entries), len(second.entries))
        self.assertEqual(first.total("planned", "expense"), 2000)
        self.assertEqual(first.entries[-1].installment_count, 6)

    def test_empty_pdf_and_command_without_gemini(self):
        result = process_command(self.db, self.payload())
        self.assertTrue(result.pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(result.file_name, "finbot-relatorio-2026-09.pdf")
        self.assertIn("Nenhum lançamento", result.text)
        self.assertIsNone(process_command(self.db, self.payload(jid="not-authorized@g.us")))
        help_result = process_command(self.db, self.payload("/fin ajuda"))
        self.assertIsNone(help_result.pdf_bytes)

    def test_month_default_previous_and_year_boundary(self):
        self.assertEqual(process_command(self.db, self.payload("/fin relatorio")).file_name,
                         "finbot-relatorio-2026-09.pdf")
        self.assertEqual(process_command(self.db, self.payload("/fin relatório mês passado")).file_name,
                         "finbot-relatorio-2026-08.pdf")
        self.entry(occurred_on=date(2026, 12, 31))
        self.entry(occurred_on=date(2027, 1, 1))
        self.assertEqual(len(monthly_report(self.db, 1, 2026, 12).entries), 1)

    def test_invalid_month_does_not_call_gemini(self):
        with patch("app.commands.parse_report_query") as parser:
            with self.assertRaises(ValueError):
                process_command(self.db, self.payload("/fin relatorio 13/2026"))
            parser.assert_not_called()

    def test_free_query_stays_text(self):
        query = type("Query", (), dict(start_date="2026-09-01", end_date="2026-09-30",
                                       kind="all", category=None, status="both"))()
        with patch("app.commands.parse_report_query", return_value=query):
            result = process_command(self.db, self.payload("/fin quanto gastei este mês"))
        self.assertIsNone(result.pdf_bytes)
        self.assertIn("Consulta de", result.text)

    def test_private_and_owner_group_scope(self):
        with patch("app.commands.get_settings") as settings:
            settings.return_value.owner_jid = "owner@s.whatsapp.net"
            settings.return_value.owner_self_jid = ""
            settings.return_value.timezone = "America/Sao_Paulo"
            self.db.add(Chat(jid="person@s.whatsapp.net", kind="private", enabled=True))
            self.db.add_all([Ledger(id=3, scope_jid="person@s.whatsapp.net", kind="private", name="Pessoa"),
                             Ledger(id=4, scope_jid="owner@s.whatsapp.net", kind="owner", name="Meu controle")])
            self.db.flush()
            self.entry(ledger_id=3, amount_cents=12345)
            self.entry(ledger_id=4, amount_cents=98765)
            result = process_command(self.db, self.payload(jid="person@s.whatsapp.net"))
            self.assertIn("R$ 123,45", result.text)
            self.assertNotIn("R$ 987,65", result.text)
            chat = self.db.get(Chat, "test@g.us")
            chat.owner_private = True
            self.db.flush()
            payload = self.payload()
            self.assertIsNone(process_command(self.db, payload))
            payload["data"]["key"]["participant"] = "owner@s.whatsapp.net"
            result = process_command(self.db, payload)
            self.assertIn("R$ 987,65", result.text)
            self.assertNotIn("R$ 123,45", result.text)

    def test_pdf_long_descriptions_and_escaping(self):
        for _ in range(100):
            self.entry(description="Descrição longa: café, ação, saúde & <teste> " * 5)
        content = render_monthly_pdf(monthly_report(self.db, 1, 2026, 9))
        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertGreater(len(content), 10000)

    def test_queue_retry_snapshot_cleanup_and_legacy_text(self):
        self.entry()
        self.db.add(Inbox(instance="finbot", message_id="report", payload=self.payload()))
        self.db.commit()
        with patch.object(worker, "SessionLocal", self.sessions):
            self.assertTrue(worker.process_inbox())
            self.db.expire_all()
            row = self.db.scalar(select(Outbox))
            snapshot = row.pdf_bytes
            self.assertTrue(snapshot.startswith(b"%PDF-"))
            with patch.object(worker, "send_document", side_effect=RuntimeError("offline")):
                self.assertTrue(worker.process_outbox())
            self.db.expire_all()
            self.assertEqual(row.pdf_bytes, snapshot)
            self.assertEqual(row.state, "pending")
            self.entry(amount_cents=99999)
            row.next_attempt_at = datetime.now(timezone.utc)
            self.db.commit()
            with patch.object(worker, "send_document") as sender:
                self.assertTrue(worker.process_outbox())
                self.assertEqual(sender.call_args.args[1], snapshot)
            self.db.expire_all()
            self.assertEqual(row.state, "sent")
            self.assertIsNone(row.pdf_bytes)
            self.assertIsNone(row.file_name)
            self.assertEqual(row.text, "")
            inbox = Inbox(instance="finbot", message_id="legacy", payload={}, state="done")
            self.db.add(inbox); self.db.flush()
            self.db.add(Outbox(inbox_id=inbox.id, chat_jid="test@g.us", text="Ajuda"))
            self.db.commit()
            with patch.object(worker, "send_text") as sender:
                worker.process_outbox()
                sender.assert_called_once_with("test@g.us", "Ajuda")

    def test_send_document_contract_and_http_failure(self):
        settings = type("Settings", (), dict(evolution_api_key="test-key", evolution_api_url="http://test",
                                             evolution_instance="finbot"))()
        with patch("app.evolution.get_settings", return_value=settings), patch("app.evolution.httpx.Client") as client:
            send_document("test@g.us", b"%PDF-example", "report.pdf", "Resumo")
            args = client.return_value.__enter__.return_value.post.call_args
            self.assertTrue(args.args[0].endswith("/message/sendMedia/finbot"))
            body = args.kwargs["json"]
            self.assertEqual(body["mediatype"], "document")
            self.assertEqual(body["fileName"], "report.pdf")
            self.assertEqual(base64.b64decode(body["media"]), b"%PDF-example")
            self.assertEqual(body["caption"], "Resumo")
            response = httpx.Response(503, request=httpx.Request("POST", "http://test"))
            client.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaises(httpx.HTTPStatusError):
                send_document("test@g.us", b"%PDF-example", "report.pdf", "Resumo")

    def test_exhausted_retries_keep_pdf_for_manual_retry(self):
        inbox = Inbox(instance="finbot", message_id="failed-pdf", payload={}, state="done")
        self.db.add(inbox); self.db.flush()
        row = Outbox(inbox_id=inbox.id, chat_jid="test@g.us", text="Resumo",
                     pdf_bytes=b"%PDF-example", file_name="report.pdf", attempts=4)
        self.db.add(row); self.db.commit()
        with patch.object(worker, "SessionLocal", self.sessions), patch.object(worker, "send_document", side_effect=RuntimeError("offline")):
            worker.process_outbox()
        self.db.expire_all()
        self.assertEqual(row.state, "failed")
        self.assertEqual(row.attempts, 5)
        self.assertEqual(row.pdf_bytes, b"%PDF-example")
        self.assertEqual(row.file_name, "report.pdf")

    def test_migration_new_and_existing_database(self):
        migration = import_module("migrations.versions.0002_pdf_outbox")
        for legacy in (False, True):
            with self.engine.begin() as conn:
                if legacy:
                    conn.execute(text("ALTER TABLE outbox DROP COLUMN pdf_bytes"))
                    conn.execute(text("ALTER TABLE outbox DROP COLUMN file_name"))
                    conn.execute(text("INSERT INTO inbox (instance,message_id,payload,state,attempts,next_attempt_at,created_at) VALUES ('x','x','{}','done',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                    conn.execute(text("INSERT INTO outbox (inbox_id,chat_jid,text,state,attempts,next_attempt_at) VALUES (1,'x','Resposta antiga','pending',0,CURRENT_TIMESTAMP)"))
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                columns = {c["name"] for c in inspect(conn).get_columns("outbox")}
                self.assertTrue({"pdf_bytes", "file_name"} <= columns)
                if legacy:
                    self.assertEqual(conn.execute(text("select text from outbox")).scalar(), "Resposta antiga")


if __name__ == "__main__":
    main()
