"""Limpieza del outbox: solo lecturas publicadas hace más de N días.

Invariante del store-and-forward: una lectura pendiente o
sin confirmar NUNCA se borra por antigüedad, aunque la nube lleve caída
mucho más que la retención.
"""
import unittest
from datetime import timedelta

from monitoring.application.purge_outbox import PurgeOutboxUseCase
from monitoring.domain.services import BatchPolicy, ReadingFactory
from tests.fakes import T0, FakeClock, FakeOutbox, sequential_ids

DEVICE = "EMSafe-DOS-01"
factory = ReadingFactory(790.0)


def reading(seq, received_at, deferred=1):
    return factory.from_raw(DEVICE, seq, 1762959843 + seq, 10.0, deferred, received_at)


class PurgeContract:
    """Mismos casos contra el doble y contra SQLite (ver subclases)."""

    def make_outbox(self):
        raise NotImplementedError

    def setUp(self):
        self.outbox = self.make_outbox()
        self.ids = sequential_ids()

    def publish_all(self, at):
        self.outbox.batch_pending(BatchPolicy(), self.ids)
        for m in self.outbox.open_messages():
            self.outbox.mark_published(m.message_id, at)

    def test_borra_solo_las_publicadas_hace_mas_de_la_retencion(self):
        now = T0 + timedelta(days=30)
        self.outbox.add(reading(1, T0))
        self.publish_all(now - timedelta(days=8))   # se borra
        self.outbox.add(reading(2, T0))
        self.publish_all(now - timedelta(days=6))   # se conserva

        purged = PurgeOutboxUseCase(self.outbox, FakeClock(now), retention_days=7).run_once()

        self.assertEqual(purged, 1)
        self.assertIsNone(self.outbox.find(reading(1, T0).identity))
        self.assertIsNotNone(self.outbox.find(reading(2, T0).identity))

    def test_pendientes_y_sin_confirmar_nunca_se_borran_aunque_la_nube_lleve_un_ano_caida(self):
        # Lecturas recibidas hace un año que nunca llegaron a la nube: unas
        # sin lote (PENDING) y otras con lote esperando PUBACK (BATCHED).
        a_year_ago = T0
        now = T0 + timedelta(days=365)
        for seq in range(1, 61):
            self.outbox.add(reading(seq, a_year_ago))
        self.outbox.batch_pending(BatchPolicy(), self.ids)   # 50 + 10 BATCHED, sin PUBACK
        for seq in range(61, 71):
            self.outbox.add(reading(seq, a_year_ago))       # 10 PENDING

        purged = PurgeOutboxUseCase(self.outbox, FakeClock(now), retention_days=1).run_once()

        self.assertEqual(purged, 0)
        self.assertEqual(self.outbox.pending_count(), 70)
        for seq in range(1, 71):
            self.assertIsNotNone(self.outbox.find(reading(seq, a_year_ago).identity), seq)

    def test_justo_en_el_borde_no_se_borra(self):
        now = T0 + timedelta(days=30)
        self.outbox.add(reading(1, T0))
        self.publish_all(now - timedelta(days=7))

        self.assertEqual(PurgeOutboxUseCase(self.outbox, FakeClock(now), 7).run_once(), 0)

    def test_tras_borrar_un_reenvio_viejo_vuelve_a_entrar(self):
        # Consecuencia aceptada: pasada la retención, la identidad se olvida.
        # El buffer del sensor dura ~30 min, así que en la práctica no ocurre.
        now = T0 + timedelta(days=30)
        self.outbox.add(reading(1, T0))
        self.publish_all(now - timedelta(days=10))
        PurgeOutboxUseCase(self.outbox, FakeClock(now), 7).run_once()

        self.assertTrue(self.outbox.add(reading(1, now)))


class FakeOutboxPurgeTest(PurgeContract, unittest.TestCase):
    def make_outbox(self):
        return FakeOutbox()


class SqliteOutboxPurgeTest(PurgeContract, unittest.TestCase):
    def make_outbox(self):
        from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
        from shared.database import init_db
        self.database = init_db(":memory:")
        self.addCleanup(self.database.close)
        return SqliteReadingOutbox()


class ValidationTest(unittest.TestCase):
    def test_retencion_minima_de_un_dia(self):
        with self.assertRaises(ValueError):
            PurgeOutboxUseCase(FakeOutbox(), FakeClock(), retention_days=0)


if __name__ == "__main__":
    unittest.main()
