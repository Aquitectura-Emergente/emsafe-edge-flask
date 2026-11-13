"""Publicación del outbox: marca solo tras PUBACK, reintenta el mismo mensaje y respeta el orden."""
import asyncio
import unittest

from monitoring.application.publish_outbox import PublishOutboxUseCase
from monitoring.domain.services import ReadingFactory
from tests.fakes import BATCHED, PENDING, PUBLISHED, FakeClock, FakeOutbox, FakePublisher, sequential_ids

TS = 1762959843
LOGGER = "monitoring.application.publish_outbox"
factory = ReadingFactory(790.0)


def fill(outbox, clock, n, deferred=1, device="EMSafe-DOS-01", start=1):
    for seq in range(start, start + n):
        outbox.add(factory.from_raw(device, seq, TS + seq, 10.0, deferred, clock.now()))


def build(responses=None, **kw):
    outbox, clock, publisher = FakeOutbox(), FakeClock(), FakePublisher(responses)
    use_case = PublishOutboxUseCase(outbox, publisher, clock, new_message_id=sequential_ids(), **kw)
    return use_case, outbox, clock, publisher


def run(coro):
    return asyncio.run(coro)


class PublishTest(unittest.TestCase):
    def test_publica_y_marca_tras_el_ack(self):
        use_case, outbox, clock, publisher = build()
        fill(outbox, clock, 3, deferred=0)

        report = run(use_case.run_once())

        self.assertEqual((report.batched, report.published, report.failed), (3, 3, False))
        self.assertEqual(outbox.statuses(), [PUBLISHED] * 3)
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual([s[0] for s in publisher.sent], ["msg-1", "msg-2", "msg-3"])

    def test_120_diferidas_salen_en_3_mensajes(self):
        use_case, outbox, clock, publisher = build()
        fill(outbox, clock, 120)

        run(use_case.run_once())

        self.assertEqual([s[2] for s in publisher.sent], [50, 50, 20])

    def test_sin_lecturas_no_publica_nada(self):
        use_case, _, _, publisher = build()
        report = run(use_case.run_once())
        self.assertEqual((report.batched, report.published), (0, 0))
        self.assertEqual(publisher.sent, [])

    def test_published_at_es_la_hora_del_intento(self):
        use_case, outbox, clock, publisher = build(responses=[False, True])
        fill(outbox, clock, 1, deferred=0)

        first_attempt = clock.now()
        with self.assertLogs(LOGGER, level="WARNING"):
            run(use_case.run_once())
        clock.advance(30)
        run(use_case.run_once())

        self.assertEqual(publisher.sent[0][3], first_attempt)
        self.assertEqual(publisher.sent[1][3], clock.now())


class NoAckTest(unittest.TestCase):
    def test_sin_ack_no_marca_nada(self):
        use_case, outbox, clock, _ = build(responses=[False])
        fill(outbox, clock, 1, deferred=0)

        with self.assertLogs(LOGGER, level="WARNING"):
            report = run(use_case.run_once())

        self.assertTrue(report.failed)
        self.assertEqual(outbox.statuses(), [BATCHED])
        self.assertEqual(outbox.pending_count(), 1)

    def test_excepcion_del_broker_no_pierde_la_lectura(self):
        use_case, outbox, clock, _ = build(responses=[ConnectionError("broker caído")])
        fill(outbox, clock, 1, deferred=0)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            report = run(use_case.run_once())

        self.assertTrue(report.failed)
        self.assertIn("broker caído", logs.output[0])
        self.assertEqual(outbox.pending_count(), 1)

    def test_timeout_sin_puback(self):
        use_case, outbox, clock, _ = build(responses=["hang"], ack_timeout_s=0.01)
        fill(outbox, clock, 1, deferred=0)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            report = run(use_case.run_once())

        self.assertTrue(report.failed)
        self.assertIn("Sin PUBACK", logs.output[0])
        self.assertEqual(outbox.statuses(), [BATCHED])


class RetryTest(unittest.TestCase):
    def test_el_reintento_conserva_el_message_id_y_las_lecturas(self):
        use_case, outbox, clock, publisher = build(responses=[False, True])
        fill(outbox, clock, 30)

        with self.assertLogs(LOGGER, level="WARNING"):
            run(use_case.run_once())
        # Llegan más lecturas mientras tanto: no se meten en el mensaje viejo.
        fill(outbox, clock, 5, start=31)
        report = run(use_case.run_once())

        first, retry, new = publisher.sent
        self.assertEqual(retry[0], first[0])   # mismo messageId
        self.assertEqual(retry[2], first[2])   # mismas 30 lecturas
        self.assertEqual(new[0], "msg-2")
        self.assertEqual(new[2], 5)
        self.assertEqual(report.published, 2)
        self.assertEqual(outbox.pending_count(), 0)

    def test_se_detiene_en_el_primer_fallo_para_no_desordenar(self):
        use_case, outbox, clock, publisher = build(responses=[True, False])
        fill(outbox, clock, 3, deferred=0)

        with self.assertLogs(LOGGER, level="WARNING"):
            report = run(use_case.run_once())

        self.assertEqual(report.published, 1)
        self.assertEqual(len(publisher.sent), 2)  # el tercero ni se intenta
        self.assertEqual(outbox.statuses(), [PUBLISHED, BATCHED, BATCHED])

        run(use_case.run_once())
        self.assertEqual([s[0] for s in publisher.sent[2:]], ["msg-2", "msg-3"])
        self.assertEqual(outbox.statuses(), [PUBLISHED] * 3)

    def test_limite_de_mensajes_por_ciclo(self):
        use_case, outbox, clock, publisher = build(max_messages_per_run=2)
        fill(outbox, clock, 5, deferred=0)

        self.assertEqual(run(use_case.run_once()).published, 2)
        self.assertEqual(outbox.statuses(), [PUBLISHED, PUBLISHED, BATCHED, BATCHED, BATCHED])


class SqliteIntegrationTest(unittest.TestCase):
    """El caso de uso contra el outbox SQLite real (en memoria)."""

    def setUp(self):
        from shared.database import init_db
        self.database = init_db(":memory:")

    def tearDown(self):
        self.database.close()

    def test_ciclo_completo_con_fallo_y_reintento(self):
        from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox

        outbox, clock = SqliteReadingOutbox(), FakeClock()
        publisher = FakePublisher([True, False, True, True])
        use_case = PublishOutboxUseCase(outbox, publisher, clock, new_message_id=sequential_ids())
        fill(outbox, clock, 120)            # 3 mensajes diferidos
        fill(outbox, clock, 1, deferred=0, start=500)

        with self.assertLogs(LOGGER, level="WARNING"):
            first = run(use_case.run_once())
        second = run(use_case.run_once())

        self.assertEqual((first.batched, first.published, first.failed), (4, 1, True))
        self.assertEqual((second.batched, second.published, second.failed), (0, 3, False))
        self.assertEqual([s[0] for s in publisher.sent], ["msg-1", "msg-2", "msg-2", "msg-3", "msg-4"])
        self.assertEqual([s[2] for s in publisher.sent], [50, 50, 50, 20, 1])
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.open_messages(), [])


if __name__ == "__main__":
    unittest.main()
