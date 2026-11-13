"""Caso de uso de ingesta: valida, filtra, deduplica y guarda en el outbox."""
import unittest

from monitoring.application.ingest_reading import IngestReadingUseCase, IngestResult
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import (
    CapturedAtSource,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)
from tests.fakes import T0, FakeClock, FakeOutbox

DEVICE = "EMSafe-DOS-01"
TS = 1762959843
LOGGER = "monitoring.application.ingest_reading"


def build():
    outbox, clock = FakeOutbox(), FakeClock()
    use_case = IngestReadingUseCase(outbox, ReadingFactory(saturation_ut=790.0), clock)
    return use_case, outbox, clock


class IngestTest(unittest.TestCase):
    def test_lectura_valida_se_guarda_con_la_hora_de_recepcion(self):
        use_case, outbox, _ = build()

        result = use_case.ingest(DEVICE, 1234, TS, 219.5, 0)

        self.assertIs(result, IngestResult.STORED)
        [stored] = outbox.stored()
        self.assertEqual(stored.identity, ReadingIdentity(DEVICE, 1234, TS))
        self.assertEqual(stored.received_at, T0)
        self.assertIs(stored.origin, SyncOrigin.REALTIME)
        self.assertIs(stored.quality, ReadingQuality.VALID)

    def test_invalidas_se_descartan_con_log_y_no_se_guardan(self):
        use_case, outbox, _ = build()

        for value in (float("nan"), float("inf"), -3.0, "abc"):
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                self.assertIs(use_case.ingest(DEVICE, 1, TS, value, 0), IngestResult.REJECTED)
            self.assertIn("descartada", logs.output[0])

        self.assertEqual(outbox.stored(), [])

    def test_saturada_se_guarda_marcada(self):
        use_case, outbox, _ = build()

        self.assertIs(use_case.ingest(DEVICE, 1, TS, 800.0, 0), IngestResult.STORED)
        self.assertIs(outbox.stored()[0].quality, ReadingQuality.SATURATED)

    def test_diferida_conserva_su_origen(self):
        use_case, outbox, _ = build()

        use_case.ingest(DEVICE, 1, TS, 10.0, 1)

        self.assertIs(outbox.stored()[0].origin, SyncOrigin.DEFERRED)


class DeduplicationTest(unittest.TestCase):
    def test_reenvio_completo_del_buffer_tras_un_corte(self):
        use_case, outbox, clock = build()
        buffer = [(seq, TS + seq) for seq in range(1, 31)]

        # Primera sesión: se corta en la lectura 20.
        use_case.session_started(DEVICE)
        for seq, ts in buffer[:20]:
            use_case.ingest(DEVICE, seq, ts, 50.0, 1)
        # Reconexión: el firmware reenvía todo desde el principio.
        clock.advance(10)
        use_case.session_started(DEVICE)
        results = [use_case.ingest(DEVICE, seq, ts, 50.0, 1) for seq, ts in buffer]

        self.assertEqual(results.count(IngestResult.DUPLICATE), 20)
        self.assertEqual(results.count(IngestResult.STORED), 10)
        self.assertEqual(len(outbox.stored()), 30)

    def test_duplicada_sin_hora_conserva_el_primer_captured_at(self):
        use_case, outbox, clock = build()

        use_case.session_started(DEVICE)
        use_case.ingest(DEVICE, 5, 0, 12.0, 1)
        clock.advance(120)
        use_case.session_started(DEVICE)
        result = use_case.ingest(DEVICE, 5, 0, 12.0, 1)

        self.assertIs(result, IngestResult.DUPLICATE)
        stored = outbox.find(ReadingIdentity(DEVICE, 5, 0))
        self.assertEqual(stored.captured_at, T0)
        self.assertIs(stored.captured_at_source, CapturedAtSource.EDGE_RECEIVED)


class BootRegressionWarningTest(unittest.TestCase):
    """Limitación conocida: varios arranques sin hora."""

    def test_advierte_si_el_seq_retrocede_entre_lecturas_sin_hora_en_la_misma_sesion(self):
        use_case, _, _ = build()
        use_case.session_started(DEVICE)
        for seq in (1, 2, 3):
            use_case.ingest(DEVICE, seq, 0, 10.0, 1)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            use_case.ingest(DEVICE, 1, 0, 10.0, 1)  # segundo arranque en el mismo buffer

        self.assertIn("retrocedió", logs.output[0])
        self.assertIn("bootId", logs.output[0])

    def test_no_advierte_cuando_una_sesion_nueva_reenvia_desde_el_inicio(self):
        use_case, _, _ = build()
        use_case.session_started(DEVICE)
        for seq in (1, 2, 3):
            use_case.ingest(DEVICE, seq, 0, 10.0, 1)
        use_case.session_started(DEVICE)

        with self.assertNoLogs(LOGGER, level="WARNING"):
            use_case.ingest(DEVICE, 1, 0, 10.0, 1)

    def test_no_advierte_con_lecturas_con_hora(self):
        # Con ts > 0 la identidad distingue arranques: no hay limitación.
        use_case, _, _ = build()
        use_case.session_started(DEVICE)
        use_case.ingest(DEVICE, 10, TS, 10.0, 0)

        with self.assertNoLogs(LOGGER, level="WARNING"):
            use_case.ingest(DEVICE, 1, TS + 100, 10.0, 0)

    def test_el_seguimiento_es_por_dispositivo(self):
        use_case, _, _ = build()
        use_case.ingest("EMSafe-DOS-01", 5, 0, 10.0, 1)

        with self.assertNoLogs(LOGGER, level="WARNING"):
            use_case.ingest("EMSafe-DOS-02", 1, 0, 10.0, 1)


class SqliteIntegrationTest(unittest.TestCase):
    """El caso de uso con el outbox real en memoria (sin BLE ni red)."""

    def test_ingesta_contra_sqlite(self):
        from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
        from shared.database import init_db

        database = init_db(":memory:")
        try:
            outbox = SqliteReadingOutbox()
            use_case = IngestReadingUseCase(outbox, ReadingFactory(790.0), FakeClock())

            self.assertIs(use_case.ingest(DEVICE, 1, 0, 20.0, 1), IngestResult.STORED)
            self.assertIs(use_case.ingest(DEVICE, 1, 0, 20.0, 1), IngestResult.DUPLICATE)
            self.assertIs(use_case.ingest(DEVICE, 2, 0, -1.0, 1), IngestResult.REJECTED)
            self.assertEqual(outbox.pending_count(), 1)
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
