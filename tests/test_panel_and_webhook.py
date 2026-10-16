import asyncio
import base64
from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import admin
from app.db import Base
from app.main import evolution_webhook
from app.models import Entry, Inbox, Ledger


class DbTestCase(TestCase):
    module = "app.admin"

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        patcher = patch(f"{self.module}.SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)


class EditEntryValidationTest(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.sessions() as db:
            db.add(Ledger(id=1, scope_jid="owner@s.whatsapp.net", kind="owner", name="Meu controle"))
            db.add(Entry(id=1, ledger_id=1, creator_jid="owner@s.whatsapp.net", kind="expense",
                         amount_cents=1000, description="Mercado", category="Outros",
                         occurred_on=date(2026, 1, 10), status="realized"))
            db.commit()

    def edit(self, **kwargs):
        form = {"amount": "20,00", "description": "Padaria", "category": "Outros",
                "occurred_on": "2026-01-11", "status": "realized"} | kwargs
        return admin.edit_entry(1, session={"csrf": "t"}, csrf="t", **form)

    def assert_unchanged(self):
        with self.sessions() as db:
            entry = db.get(Entry, 1)
            self.assertEqual((entry.amount_cents, entry.description, entry.occurred_on),
                             (1000, "Mercado", date(2026, 1, 10)))

    def test_invalid_amount_returns_400_and_keeps_entry(self):
        with self.assertRaises(HTTPException) as ctx:
            self.edit(amount="abc")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assert_unchanged()

    def test_invalid_date_returns_400_and_keeps_entry(self):
        with self.assertRaises(HTTPException) as ctx:
            self.edit(occurred_on="99/99/9999")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assert_unchanged()


class LoginRateLimitTest(TestCase):
    def setUp(self):
        admin.login_attempts.clear()
        self.addCleanup(admin.login_attempts.clear)
        encoded = base64.b64encode(admin.password_hasher.hash("certa").encode()).decode()
        patcher = patch("app.admin.get_settings", lambda: SimpleNamespace(admin_password_hash_b64=encoded))
        patcher.start()
        self.addCleanup(patcher.stop)

    def attempt(self, forwarded):
        request = Request({"type": "http", "method": "POST", "path": "/admin/login", "query_string": b"",
                           "headers": [(b"x-forwarded-for", forwarded.encode())], "client": ("10.0.0.1", 1)})
        return admin.login(request, password="errada")

    def test_rate_limit_uses_rightmost_forwarded_ip(self):
        for index in range(5):
            self.attempt(f"1.1.1.{index}, 9.9.9.9")
        with self.assertRaises(HTTPException) as ctx:
            self.attempt("2.2.2.2, 9.9.9.9")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(list(admin.login_attempts), ["9.9.9.9"])


class WebhookInvalidBodyTest(DbTestCase):
    module = "app.main"

    def post(self, body: bytes):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/webhook/evolution",
                           "headers": [], "query_string": b""}, receive)
        return asyncio.run(evolution_webhook(request))

    def inbox_count(self):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(Inbox))

    def test_list_body_is_ignored(self):
        self.assertEqual(self.post(b"[1, 2]"), {"ok": True})
        self.assertEqual(self.inbox_count(), 0)

    def test_invalid_json_is_ignored(self):
        self.assertEqual(self.post(b"{nao e json"), {"ok": True})
        self.assertEqual(self.inbox_count(), 0)

    def test_non_object_message_is_ignored(self):
        body = b'{"event": "messages.upsert", "instance": "x", "data": {"key": {"id": "1", "remoteJid": "a"}, "message": "texto"}}'
        with patch("app.main.get_settings", lambda: SimpleNamespace(evolution_instance="x")):
            self.assertEqual(self.post(body), {"ok": True})
        self.assertEqual(self.inbox_count(), 0)
