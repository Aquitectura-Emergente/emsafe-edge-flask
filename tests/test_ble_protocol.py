"""Contrato BLE: bytes exactos que entiende el firmware del dosímetro."""
import math
import os
import struct
import subprocess
import sys
import unittest

from monitoring.infrastructure import ble_protocol as p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class UuidContractTest(unittest.TestCase):
    """Guarda del contrato: estos UUID están grabados en el firmware."""

    def test_uuids_exactos(self):
        self.assertEqual(p.SERVICE_UUID, "6e7a0001-5a1f-4c3b-9d2e-0000000000a1")
        self.assertEqual(p.READING_UUID, "6e7a0002-5a1f-4c3b-9d2e-0000000000a1")
        self.assertEqual(p.TIME_UUID, "6e7a0003-5a1f-4c3b-9d2e-0000000000a1")
        self.assertEqual(p.THRESHOLD_UUID, "6e7a0004-5a1f-4c3b-9d2e-0000000000a1")

    def test_el_modulo_no_importa_bleak(self):
        # Debe poder importarse y probarse sin la pila BLE. Se comprueba en
        # un proceso limpio porque otros tests podrían haber cargado bleak.
        code = (
            "import sys, monitoring.infrastructure.ble_protocol; "
            "sys.exit(1 if any(m.split('.')[0] == 'bleak' for m in sys.modules) else 0)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
        self.assertEqual(result.returncode, 0, "ble_protocol no debe importar bleak")


class DecodeReadingTest(unittest.TestCase):
    def test_decodifica_el_struct_empaquetado_del_firmware(self):
        data = struct.pack("<IIfB", 1234, 1762959844, 219.5, 0)

        r = p.decode_reading(data)

        self.assertEqual(len(data), 13)
        self.assertEqual(r, p.RawReading(seq=1234, ts=1762959844, uT=219.5, deferred=0))

    def test_little_endian(self):
        data = bytes([0x01, 0, 0, 0, 0, 0, 0, 0]) + struct.pack("<f", 1.0) + b"\x01"

        r = p.decode_reading(data)

        self.assertEqual((r.seq, r.ts, r.uT, r.deferred), (1, 0, 1.0, 1))

    def test_acepta_bytearray_y_memoryview(self):
        data = struct.pack("<IIfB", 7, 0, 10.0, 1)
        self.assertEqual(p.decode_reading(bytearray(data)).seq, 7)
        self.assertEqual(p.decode_reading(memoryview(data)).seq, 7)

    def test_recorta_el_ruido_de_float32(self):
        # 219.3 en float32 es 219.3000030517578 en float64.
        data = struct.pack("<IIfB", 1, 0, 219.3, 0)
        self.assertEqual(p.decode_reading(data).uT, 219.3)

    def test_nan_e_inf_pasan_para_que_los_rechace_el_dominio(self):
        nan = p.decode_reading(struct.pack("<IIfB", 1, 0, float("nan"), 0))
        inf = p.decode_reading(struct.pack("<IIfB", 1, 0, float("inf"), 0))
        self.assertTrue(math.isnan(nan.uT))
        self.assertTrue(math.isinf(inf.uT))

    def test_valores_maximos_de_uint32(self):
        r = p.decode_reading(struct.pack("<IIfB", 2**32 - 1, 2**32 - 1, 0.0, 255))
        self.assertEqual((r.seq, r.ts, r.deferred), (2**32 - 1, 2**32 - 1, 255))

    def test_rechaza_largo_distinto_de_13(self):
        for size in (0, 12, 14, 20):
            with self.assertRaises(p.BleProtocolError, msg=size):
                p.decode_reading(b"\x00" * size)

    def test_ida_y_vuelta(self):
        raw = p.RawReading(seq=42, ts=1762959844, uT=512.25, deferred=1)
        self.assertEqual(p.decode_reading(p.encode_reading(raw)), raw)


class EncodeTimeTest(unittest.TestCase):
    def test_uint32_little_endian(self):
        self.assertEqual(p.encode_time(1762959844), struct.pack("<I", 1762959844))
        self.assertEqual(p.encode_time(1), b"\x01\x00\x00\x00")

    def test_ida_y_vuelta(self):
        self.assertEqual(p.decode_time(p.encode_time(1762959844)), 1762959844)

    def test_rechaza_cero_negativos_desbordes_y_no_enteros(self):
        # 0 es "sin hora" para el firmware.
        for value in (0, -1, 2**32, 1.5, "1762959844", True):
            with self.assertRaises(p.BleProtocolError, msg=repr(value)):
                p.encode_time(value)


class EncodeThresholdTest(unittest.TestCase):
    def test_float32_little_endian(self):
        self.assertEqual(p.encode_threshold(300.0), struct.pack("<f", 300.0))
        self.assertEqual(len(p.encode_threshold(300)), 4)

    def test_ida_y_vuelta(self):
        self.assertEqual(p.decode_threshold(p.encode_threshold(250.5)), 250.5)
        self.assertEqual(p.decode_threshold(p.encode_threshold(219.3)), 219.3)

    def test_rechaza_lo_que_el_firmware_ignoraria(self):
        for value in (0, -10.0, float("nan"), float("inf"), "300", None, True):
            with self.assertRaises(p.BleProtocolError, msg=repr(value)):
                p.encode_threshold(value)


if __name__ == "__main__":
    unittest.main()
