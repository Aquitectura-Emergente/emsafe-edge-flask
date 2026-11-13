"""Adaptador BLE: secuencia de conexión, cola, desconexión y espera creciente.

Sin hardware: `FakeBleakClient` imita la parte de bleak que se usa y deja
disparar notificaciones y desconexiones desde el test.
"""
import asyncio
import struct
import threading
import unittest

from monitoring.infrastructure import ble_protocol as proto
from monitoring.infrastructure.ble_sensor_client import (
    BleSensorClient,
    BleSensorLinks,
    ReadingReceived,
    SessionStarted,
)
from tests.fakes import T0, FakeClock

DEVICE = "EMSafe-DOS-01"
LOGGER = "monitoring.infrastructure.ble_sensor_client"


class FakeBleakClient:
    def __init__(self, device, disconnected_callback, fail_on=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.fail_on = fail_on
        self.calls = []
        self.notify_callback = None

    def _maybe_fail(self, step):
        if self.fail_on == step:
            raise OSError(f"fallo simulado en {step}")

    async def connect(self):
        self.calls.append(("connect",))
        self._maybe_fail("connect")

    async def start_notify(self, uuid, callback):
        self.calls.append(("start_notify", uuid))
        self._maybe_fail("start_notify")
        self.notify_callback = callback

    async def write_gatt_char(self, uuid, data, response=None):
        self.calls.append(("write", uuid, bytes(data), response))
        self._maybe_fail(uuid)

    async def disconnect(self):
        self.calls.append(("disconnect",))

    # --- control desde el test ---
    def notify(self, data):
        self.notify_callback(None, bytearray(data))

    def drop(self):
        self.disconnected_callback(self)


class FakeConnectivity:
    def __init__(self):
        self.events = []

    def device_connected(self, device_id):
        self.events.append(("ONLINE", device_id))

    def device_disconnected(self, device_id):
        self.events.append(("OFFLINE", device_id))


class Harness:
    """Arma un cliente con dobles. `devices` es lo que devuelve cada escaneo."""

    def __init__(self, devices=("dev",), fail_on=None, threshold=250.0, stop_after_sleeps=None):
        self.queue = asyncio.Queue()
        self.connectivity = FakeConnectivity()
        self.clients = []
        self.sleeps = []
        self.scans = list(devices)
        self.fail_on = fail_on
        self.stop_after_sleeps = stop_after_sleeps
        self.ble = BleSensorClient(
            DEVICE, self.queue, lambda _id: threshold, self.connectivity, FakeClock(),
            find_device=self.find, client_factory=self.factory, sleep=self.sleep,
        )

    async def find(self, name, timeout):
        return self.scans.pop(0) if self.scans else None

    def factory(self, device, disconnected_callback):
        client = FakeBleakClient(device, disconnected_callback, self.fail_on)
        self.clients.append(client)
        return client

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.stop_after_sleeps is not None and len(self.sleeps) >= self.stop_after_sleeps:
            self.ble._stopping = True
        await asyncio.sleep(0)


async def wait_until(condition, timeout=1.0):
    async def poll():
        while not condition():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


class ConnectionSequenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_orden_obligatorio_notify_umbral_hora(self):
        h = Harness(threshold=250.0)
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)

        calls = h.clients[0].calls
        self.assertEqual(calls[0], ("connect",))
        self.assertEqual(calls[1], ("start_notify", proto.READING_UUID))
        self.assertEqual(calls[2], ("write", proto.THRESHOLD_UUID, struct.pack("<f", 250.0), True))
        self.assertEqual(calls[3], ("write", proto.TIME_UUID, struct.pack("<I", int(T0.timestamp())), True))
        self.assertEqual(len(calls), 4)

        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_marca_de_sesion_antes_que_las_lecturas_y_aviso_online(self):
        h = Harness()
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)

        h.clients[0].notify(struct.pack("<IIfB", 1, 0, 12.5, 1))

        self.assertEqual(await h.queue.get(), SessionStarted(DEVICE))
        self.assertEqual(await h.queue.get(),
                         ReadingReceived(DEVICE, proto.RawReading(seq=1, ts=0, uT=12.5, deferred=1)))
        self.assertEqual(h.connectivity.events, [("ONLINE", DEVICE)])

        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_notificacion_desde_otro_hilo_llega_a_la_cola(self):
        h = Harness()
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)
        await h.queue.get()  # SessionStarted

        t = threading.Thread(target=h.clients[0].notify, args=(struct.pack("<IIfB", 9, 5, 1.0, 0),))
        t.start()
        t.join()

        item = await asyncio.wait_for(h.queue.get(), 1)
        self.assertEqual(item.raw.seq, 9)
        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_notificacion_mal_formada_se_descarta_con_log(self):
        h = Harness()
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)
        await h.queue.get()

        with self.assertLogs(LOGGER, level="WARNING"):
            h.clients[0].notify(b"\x00" * 5)

        self.assertTrue(h.queue.empty())
        await h.ble.stop()
        await asyncio.wait_for(task, 1)


class DisconnectAndBackoffTest(unittest.IsolatedAsyncioTestCase):
    async def test_desconexion_avisa_offline_y_reconecta(self):
        h = Harness(devices=("dev", "dev"))
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)

        h.clients[0].drop()
        await wait_until(lambda: len(h.clients) == 2 and h.ble.is_connected)

        self.assertEqual(h.connectivity.events,
                         [("ONLINE", DEVICE), ("OFFLINE", DEVICE), ("ONLINE", DEVICE)])
        self.assertEqual(h.sleeps, [1.0])  # tras una sesión se reintenta pronto
        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_desconexion_desde_otro_hilo(self):
        h = Harness()
        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)

        threading.Thread(target=h.clients[0].drop).start()
        await wait_until(lambda: ("OFFLINE", DEVICE) in h.connectivity.events)

        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_espera_creciente_si_no_lo_encuentra(self):
        h = Harness(devices=(), stop_after_sleeps=7)

        await asyncio.wait_for(h.ble.run(), 1)

        self.assertEqual(h.sleeps, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])

    async def test_fallo_en_la_secuencia_desconecta_y_reintenta(self):
        h = Harness(devices=("dev",), fail_on=proto.TIME_UUID, stop_after_sleeps=1)

        with self.assertLogs(LOGGER, level="WARNING"):
            await asyncio.wait_for(h.ble.run(), 1)

        self.assertEqual(h.clients[0].calls[-1], ("disconnect",))
        self.assertFalse(h.ble.is_connected)
        self.assertEqual(h.connectivity.events, [])  # nunca estuvo ONLINE
        self.assertEqual(h.sleeps, [1.0])

    async def test_error_de_escaneo_no_rompe_el_bucle(self):
        h = Harness(stop_after_sleeps=1)

        async def broken_find(name, timeout):
            raise OSError("adaptador Bluetooth apagado")

        h.ble._find_device = broken_find
        with self.assertLogs(LOGGER, level="WARNING"):
            await asyncio.wait_for(h.ble.run(), 1)
        self.assertEqual(h.sleeps, [1.0])


class StopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_interrumpe_un_escaneo_en_curso(self):
        h = Harness()
        scan_cancelled = asyncio.Event()

        async def slow_find(name, timeout):
            try:
                await asyncio.sleep(60)  # escaneo largo
            except asyncio.CancelledError:
                scan_cancelled.set()
                raise

        h.ble._find_device = slow_find
        task = asyncio.create_task(h.ble.run())
        await asyncio.sleep(0.01)

        await h.ble.stop()
        await asyncio.wait_for(task, 0.5)  # no espera los 60 s

        self.assertTrue(scan_cancelled.is_set())
        self.assertEqual(h.clients, [])

    async def test_stop_interrumpe_la_espera_entre_reintentos(self):
        h = Harness(devices=())

        async def long_sleep(seconds):
            await asyncio.sleep(60)

        h.ble._sleep = long_sleep
        task = asyncio.create_task(h.ble.run())
        await asyncio.sleep(0.01)

        await h.ble.stop()
        await asyncio.wait_for(task, 0.5)

    async def test_stop_antes_de_arrancar(self):
        h = Harness()
        await h.ble.stop()
        await asyncio.wait_for(h.ble.run(), 0.5)
        self.assertEqual(h.clients, [])


class LinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_escribe_el_umbral_en_caliente_solo_si_esta_conectado(self):
        h = Harness()
        links = BleSensorLinks()
        links.register(h.ble)
        self.assertFalse(links.is_connected(DEVICE))
        with self.assertRaises(ConnectionError):
            await links.write_threshold(DEVICE, 200.0)

        task = asyncio.create_task(h.ble.run())
        await wait_until(lambda: h.ble.is_connected)
        await links.write_threshold(DEVICE, 200.0)

        self.assertTrue(links.is_connected(DEVICE))
        self.assertEqual(links.connected_devices(), [DEVICE])
        self.assertEqual(h.clients[0].calls[-1],
                         ("write", proto.THRESHOLD_UUID, struct.pack("<f", 200.0), True))
        await h.ble.stop()
        await asyncio.wait_for(task, 1)

    async def test_dispositivo_no_configurado(self):
        self.assertFalse(BleSensorLinks().is_connected("X"))
        with self.assertRaises(KeyError):
            await BleSensorLinks().write_threshold("X", 200.0)


if __name__ == "__main__":
    unittest.main()
