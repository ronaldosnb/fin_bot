from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.admin import entries_page, save_chat
from app.db import Base
from app.models import Chat, Ledger


class AccountSearchTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            db.add_all([
                Chat(jid="family@g.us", kind="group", name="Família"),
                Ledger(id=1, scope_jid="family@g.us", kind="group", name="family@g.us"),
                Ledger(id=2, scope_jid="owner@s.whatsapp.net", kind="owner", name="Meu controle"),
                Chat(jid="owner@s.whatsapp.net", kind="private", name="Dono"),
            ])
            db.commit()
        self.session_patch = patch("app.admin.SessionLocal", self.sessions)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.engine.dispose)

    def page(self, **kwargs):
        request = Request({"type": "http", "method": "GET", "path": "/admin/entries",
                           "headers": [], "query_string": b""})
        return entries_page(request, session={"csrf": "test"}, **kwargs)

    def test_search_uses_saved_name_and_ignores_case_and_accents(self):
        response = self.page(q="  FAMIL  ", ledger_id=2)
        self.assertEqual([ledger.id for ledger in response.context["ledgers"]], [1])
        self.assertEqual(response.context["selected"], 1)
        self.assertIn("Família", response.body.decode())

    def test_rename_is_visible_without_changing_ledger(self):
        save_chat(session={"csrf": "test"}, csrf="test", jid="family@g.us",
                  name="Casa", enabled=True, owner_private=False)
        response = self.page(q="casa")
        self.assertEqual(response.context["selected"], 1)
        self.assertEqual(response.context["ledger_names"][1], "Casa")
        with self.sessions() as db:
            self.assertEqual(db.get(Ledger, 1).name, "family@g.us")

    def test_owner_name_and_jid_search_remain_available(self):
        self.assertEqual(self.page(q="meu controle").context["selected"], 2)
        self.assertEqual(self.page(q="family@g.us").context["selected"], 1)

    def test_no_match_does_not_show_another_accounts_entries(self):
        response = self.page(q="inexistente", ledger_id=1)
        self.assertIsNone(response.context["selected"])
        self.assertEqual(response.context["entries"], [])
        self.assertIn("Nenhuma conta encontrada", response.body.decode())
