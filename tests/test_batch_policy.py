"""BatchPolicy y OutboxMessage: reglas de lotes del contrato."""
import unittest

from monitoring.domain.entities import OutboxMessage
from monitoring.domain.services import BatchPolicy, ReadingFactory
from tests.fakes import T0

factory = ReadingFactory(790.0)
TS = 1762959843


def r(seq, deferred=1, device="EMSafe-DOS-01"):
    return factory.from_raw(device, seq, TS + seq, 10.0, deferred, T0)


def sizes(groups):
    return [len(g) for g in groups]


class BatchPolicyTest(unittest.TestCase):
    def test_120_diferidas_dan_lotes_de_50_50_20(self):
        groups = BatchPolicy().group([r(i) for i in range(1, 121)])

        self.assertEqual(sizes(groups), [50, 50, 20])
        self.assertEqual([x.sequence for x in groups[0]], list(range(1, 51)))

    def test_tiempo_real_va_de_a_una(self):
        groups = BatchPolicy().group([r(i, deferred=0) for i in range(1, 4)])
        self.assertEqual(sizes(groups), [1, 1, 1])

    def test_no_mezcla_origenes_y_respeta_el_orden(self):
        # Flush del buffer y luego tiempo real, como envía el firmware.
        readings = [r(1), r(2), r(3, deferred=0), r(4, deferred=0), r(5)]

        groups = BatchPolicy().group(readings)

        self.assertEqual([[x.sequence for x in g] for g in groups], [[1, 2], [3], [4], [5]])

    def test_no_mezcla_dispositivos_pero_agrupa_cada_flujo(self):
        readings = [r(1, device="A"), r(1, device="B"), r(2, device="A"), r(2, device="B")]

        groups = BatchPolicy().group(readings)

        self.assertEqual(
            [[(x.device_id, x.sequence) for x in g] for g in groups],
            [[("A", 1), ("A", 2)], [("B", 1), ("B", 2)]],
        )

    def test_tamano_configurable(self):
        self.assertEqual(sizes(BatchPolicy(max_deferred=2).group([r(i) for i in range(5)])), [2, 2, 1])
        with self.assertRaises(ValueError):
            BatchPolicy(max_deferred=0)

    def test_sin_lecturas_no_hay_lotes(self):
        self.assertEqual(BatchPolicy().group([]), [])


class OutboxMessageTest(unittest.TestCase):
    def test_expone_dispositivo_y_origen(self):
        m = OutboxMessage("m-1", (r(1), r(2)))
        self.assertEqual(m.device_id, "EMSafe-DOS-01")
        self.assertTrue(m.origin.value == "DEFERRED")

    def test_invariantes_del_contrato(self):
        with self.assertRaises(ValueError):
            OutboxMessage("m-1", ())
        with self.assertRaises(ValueError):
            OutboxMessage("", (r(1),))
        with self.assertRaises(ValueError):
            OutboxMessage("m-1", (r(1, device="A"), r(2, device="B")))
        with self.assertRaises(ValueError):
            OutboxMessage("m-1", (r(1, deferred=1), r(2, deferred=0)))


if __name__ == "__main__":
    unittest.main()
