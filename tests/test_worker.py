from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Inbox, Outbox
from app.worker import purge_processed


OLD = datetime.now(timezone.utc) - timedelta(days=40)
NEW = datetime.now(timezone.utc) - timedelta(days=5)


class PurgeProcessedTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        patcher = patch("app.worker.SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def _inbox(self, id, state, created_at):
        return Inbox(id=id, instance="i", message_id=f"m{id}", payload={}, state=state, created_at=created_at)

    def _outbox(self, inbox_id, state):
        return Outbox(inbox_id=inbox_id, chat_jid="x@s.whatsapp.net", text="", state=state)

    def _ids(self, model):
        with self.sessions() as db:
            return set(db.scalars(select(model.id)))

    def test_remove_itens_antigos_processados(self):
        with self.sessions() as db:
            db.add_all([self._inbox(1, "done", OLD), self._outbox(1, "sent")])
            db.commit()
        self.assertEqual(purge_processed(), 2)
        self.assertEqual(self._ids(Inbox), set())
        self.assertEqual(self._ids(Outbox), set())

    def test_mantem_itens_recentes(self):
        with self.sessions() as db:
            db.add_all([self._inbox(1, "done", NEW), self._outbox(1, "sent")])
            db.commit()
        self.assertEqual(purge_processed(), 0)
        self.assertEqual(self._ids(Inbox), {1})
        self.assertEqual(len(self._ids(Outbox)), 1)

    def test_mantem_entrada_com_falha(self):
        with self.sessions() as db:
            db.add(self._inbox(1, "failed", OLD))
            db.commit()
        self.assertEqual(purge_processed(), 0)
        self.assertEqual(self._ids(Inbox), {1})

    def test_mantem_entrada_com_saida_pendente(self):
        with self.sessions() as db:
            db.add_all([self._inbox(1, "done", OLD), self._outbox(1, "pending")])
            db.commit()
        self.assertEqual(purge_processed(), 0)
        self.assertEqual(self._ids(Inbox), {1})
        self.assertEqual(len(self._ids(Outbox)), 1)

    def test_entrada_concluida_sem_saida_e_removida(self):
        with self.sessions() as db:
            db.add(self._inbox(1, "done", OLD))
            db.commit()
        self.assertEqual(purge_processed(), 1)
        self.assertEqual(self._ids(Inbox), set())
