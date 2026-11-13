"""Outbox SQLite: deduplicación por índice único y fidelidad de los datos.

Usa SQLite en memoria. Peewee es una dependencia de producción, así que no
se agregan dependencias de desarrollo.
"""
import unittest
from datetime import timedelta, timezone

from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import (
    AlertLevel,
    AlertThresholds,
    CapturedAtSource,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)
from monitoring.infrastructure.models import ReadingRow, ReadingStatus
from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
from shared.database import init_db
from tests.fakes import T0

DEVICE = "EMSafe-DOS-01"
TS = 1762959843
factory = ReadingFactory(saturation_ut=790.0)


def reading(seq=1, ts=TS, value=219.5, deferred=0, received_at=T0, device=DEVICE):
    return factory.from_raw(device, seq, ts, value, deferred, received_at)


class OutboxTestCase(unittest.TestCase):
    def setUp(self):
        self.database = init_db(":memory:")
        self.outbox = SqliteReadingOutbox()

    def tearDown(self):
        self.database.close()


class AddTest(OutboxTestCase):
    def test_guarda_y_recupera_la_lectura_identica(self):
        original = reading(seq=7, ts=0, value=950.0, deferred=1,
                           received_at=T0.replace(microsecond=123456))

        self.assertTrue(self.outbox.add(original))
        loaded = self.outbox.find(original.identity)

        self.assertEqual(loaded, original)
        self.assertIs(loaded.captured_at_source, CapturedAtSource.EDGE_RECEIVED)
        self.assertIs(loaded.origin, SyncOrigin.DEFERRED)
        self.assertIs(loaded.quality, ReadingQuality.SATURATED)
        self.assertEqual(loaded.received_at.tzinfo, timezone.utc)
        self.assertEqual(loaded.received_at.microsecond, 123456)

    def test_nueva_lectura_queda_pendiente_sin_mensaje(self):
        self.outbox.add(reading())

        row = ReadingRow.get()
        self.assertEqual(row.status, ReadingStatus.PENDING)
        self.assertIsNone(row.message_id)
        self.assertEqual(self.outbox.pending_count(), 1)

    def test_find_de_una_identidad_inexistente(self):
        self.assertIsNone(self.outbox.find(ReadingIdentity(DEVICE, 99, TS)))



class LevelAndHistoryTest(OutboxTestCase):
    def test_el_nivel_se_guarda_y_se_recupera(self):
        r = factory.from_raw(DEVICE, 1, TS, 150.0, 0, T0, AlertThresholds(100, 120))
        self.outbox.add(r)
        self.assertIs(self.outbox.find(r.identity).level, AlertLevel.DANGER)

    def test_latest_devuelve_las_mas_recientes_primero(self):
        for seq in range(1, 6):
            self.outbox.add(reading(seq=seq))
        self.outbox.add(reading(seq=1, device="OTRO"))

        self.assertEqual([r.sequence for r in self.outbox.latest(limit=3)], [1, 5, 4])
        self.assertEqual([r.sequence for r in self.outbox.latest(DEVICE, 2)], [5, 4])

class DeduplicationTest(OutboxTestCase):
    def test_reenvio_del_buffer_completo_no_duplica(self):
        # El firmware reenvía el buffer desde el inicio si se corta a mitad.
        for seq in range(1, 51):
            self.assertTrue(self.outbox.add(reading(seq=seq, ts=TS + seq, deferred=1)))
        for seq in range(1, 51):
            self.assertFalse(self.outbox.add(reading(seq=seq, ts=TS + seq, deferred=1)))

        self.assertEqual(ReadingRow.select().count(), 50)

    def test_duplicada_con_ts_cero_conserva_el_primer_captured_at(self):
        # capturedAt se fija en la primera recepción.
        first = reading(seq=3, ts=0, received_at=T0)
        resent = reading(seq=3, ts=0, received_at=T0 + timedelta(minutes=5))

        self.assertTrue(self.outbox.add(first))
        self.assertFalse(self.outbox.add(resent))

        self.assertEqual(self.outbox.find(first.identity).captured_at, T0)

    def test_mismo_seq_con_otra_hora_es_otra_lectura(self):
        # seq se reinicia cuando el ESP32 se reinicia.
        self.assertTrue(self.outbox.add(reading(seq=1, ts=TS)))
        self.assertTrue(self.outbox.add(reading(seq=1, ts=TS + 3600)))

    def test_mismo_seq_y_ts_de_otro_dispositivo_es_otra_lectura(self):
        self.assertTrue(self.outbox.add(reading(device="EMSafe-DOS-01")))
        self.assertTrue(self.outbox.add(reading(device="EMSafe-DOS-02")))

    def test_un_duplicado_no_rompe_la_transaccion_siguiente(self):
        self.outbox.add(reading(seq=1))
        self.assertFalse(self.outbox.add(reading(seq=1)))
        self.assertTrue(self.outbox.add(reading(seq=2)))
        self.assertEqual(self.outbox.pending_count(), 2)


class PendingCountTest(OutboxTestCase):
    def test_no_cuenta_las_publicadas(self):
        self.outbox.add(reading(seq=1))
        self.outbox.add(reading(seq=2))
        ReadingRow.update(status=ReadingStatus.PUBLISHED).where(ReadingRow.sequence == 1).execute()

        self.assertEqual(self.outbox.pending_count(), 1)


class MessagesTest(OutboxTestCase):
    def setUp(self):
        super().setUp()
        from monitoring.domain.services import BatchPolicy
        from tests.fakes import sequential_ids
        self.policy = BatchPolicy()
        self.ids = sequential_ids()

    def test_arma_lotes_persistentes_con_message_id(self):
        for seq in range(1, 121):
            self.outbox.add(reading(seq=seq, ts=TS + seq, deferred=1))

        self.assertEqual(self.outbox.batch_pending(self.policy, self.ids), 3)
        messages = self.outbox.open_messages()

        self.assertEqual([m.message_id for m in messages], ["msg-1", "msg-2", "msg-3"])
        self.assertEqual([len(m.readings) for m in messages], [50, 50, 20])
        self.assertEqual([r.sequence for r in messages[0].readings], list(range(1, 51)))
        self.assertEqual(ReadingRow.select().where(ReadingRow.status == ReadingStatus.BATCHED).count(), 120)

    def test_un_lote_armado_no_se_vuelve_a_armar(self):
        self.outbox.add(reading(seq=1, deferred=0))
        self.outbox.batch_pending(self.policy, self.ids)

        self.assertEqual(self.outbox.batch_pending(self.policy, self.ids), 0)
        self.assertEqual([m.message_id for m in self.outbox.open_messages()], ["msg-1"])

    def test_los_mensajes_abiertos_sobreviven_a_un_reinicio(self):
        # El messageId se guarda: tras reiniciar el Edge, el reintento es el mismo mensaje.
        self.outbox.add(reading(seq=1, deferred=0))
        self.outbox.batch_pending(self.policy, self.ids)
        before = self.outbox.open_messages()

        self.assertEqual(SqliteReadingOutbox().open_messages(), before)

    def test_mark_published_cierra_solo_ese_mensaje(self):
        for seq in (1, 2):
            self.outbox.add(reading(seq=seq, deferred=0))
        self.outbox.batch_pending(self.policy, self.ids)

        self.assertEqual(self.outbox.mark_published("msg-1", T0), 1)
        self.assertEqual(self.outbox.mark_published("msg-1", T0), 0)  # idempotente

        self.assertEqual([m.message_id for m in self.outbox.open_messages()], ["msg-2"])
        self.assertEqual(self.outbox.pending_count(), 1)

    def test_publicadas_siguen_deduplicando(self):
        # No se borran: un reenvío del buffer tras publicar sigue siendo duplicado.
        self.outbox.add(reading(seq=1, deferred=1))
        self.outbox.batch_pending(self.policy, self.ids)
        self.outbox.mark_published("msg-1", T0)

        self.assertFalse(self.outbox.add(reading(seq=1, deferred=1)))

    def test_limite_de_mensajes_abiertos(self):
        for seq in range(1, 6):
            self.outbox.add(reading(seq=seq, deferred=0))
        self.outbox.batch_pending(self.policy, self.ids)

        self.assertEqual([m.message_id for m in self.outbox.open_messages(limit=2)], ["msg-1", "msg-2"])


class InitDbTest(unittest.TestCase):
    def test_init_db_es_idempotente(self):
        database = init_db(":memory:")
        try:
            database.create_tables([ReadingRow], safe=True)
            SqliteReadingOutbox().add(reading())
            self.assertEqual(ReadingRow.select().count(), 1)
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
