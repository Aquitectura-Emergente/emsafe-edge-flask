"""Conectividad del sensor: publica cambios y los republica al reconectar el broker."""
import unittest

from monitoring.application.track_connectivity import TrackConnectivityUseCase
from tests.fakes import T0, FakeClock, FakeStatusPublisher

DEVICE = "EMSafe-DOS-01"
LOGGER = "monitoring.application.track_connectivity"


class TrackConnectivityTest(unittest.TestCase):
    def test_publica_online_y_offline_con_la_hora_del_cambio(self):
        publisher, clock = FakeStatusPublisher(), FakeClock()
        uc = TrackConnectivityUseCase(publisher, clock)

        uc.device_connected(DEVICE)
        clock.advance(60)
        uc.device_disconnected(DEVICE)

        self.assertEqual(publisher.published, [
            (DEVICE, "ONLINE", T0),
            (DEVICE, "OFFLINE", clock.now()),
        ])
        self.assertEqual(uc.current(DEVICE), "OFFLINE")

    def test_sin_broker_guarda_el_estado_y_lo_republica_al_reconectar(self):
        publisher, clock = FakeStatusPublisher(connected=False), FakeClock()
        uc = TrackConnectivityUseCase(publisher, clock)

        with self.assertLogs(LOGGER, level="INFO"):
            uc.device_connected(DEVICE)
        self.assertEqual(publisher.published, [])

        publisher.connected = True
        self.assertEqual(uc.republish_all(), 1)
        # Se publica con la hora real del cambio, no la de la reconexión.
        self.assertEqual(publisher.published, [(DEVICE, "ONLINE", T0)])

    def test_republica_solo_el_ultimo_estado_de_cada_dispositivo(self):
        publisher = FakeStatusPublisher(connected=False)
        uc = TrackConnectivityUseCase(publisher, FakeClock())
        with self.assertLogs(LOGGER, level="INFO"):
            uc.device_connected("A")
            uc.device_disconnected("A")
            uc.device_connected("B")

        publisher.connected = True
        uc.republish_all()

        self.assertEqual(sorted((d, c) for d, c, _ in publisher.published),
                         [("A", "OFFLINE"), ("B", "ONLINE")])

    def test_sin_estados_no_publica_nada(self):
        self.assertEqual(TrackConnectivityUseCase(FakeStatusPublisher(), FakeClock()).republish_all(), 0)


if __name__ == "__main__":
    unittest.main()
