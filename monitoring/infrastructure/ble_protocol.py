"""Contrato BLE con el sensor. No cambiar: está grabado en el firmware.

No importa bleak, así los tests corren sin la pila BLE.

| Elemento | UUID                                   | Propiedad | Formato              |
|----------|----------------------------------------|-----------|----------------------|
| Servicio | 6e7a0001-5a1f-4c3b-9d2e-0000000000a1   | —         | —                    |
| Lectura  | 6e7a0002-5a1f-4c3b-9d2e-0000000000a1   | NOTIFY    | 13 bytes LE `<IIfB`  |
| Hora     | 6e7a0003-5a1f-4c3b-9d2e-0000000000a1   | WRITE     | `<I` epoch Unix (s)  |
| Umbral   | 6e7a0004-5a1f-4c3b-9d2e-0000000000a1   | WRITE     | `<f` umbral en µT    |
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

SERVICE_UUID = "6e7a0001-5a1f-4c3b-9d2e-0000000000a1"
READING_UUID = "6e7a0002-5a1f-4c3b-9d2e-0000000000a1"
TIME_UUID = "6e7a0003-5a1f-4c3b-9d2e-0000000000a1"
THRESHOLD_UUID = "6e7a0004-5a1f-4c3b-9d2e-0000000000a1"

# struct Reading { uint32 seq; uint32 ts; float uT; uint8 deferred; } __attribute__((packed))
_READING = struct.Struct("<IIfB")
_TIME = struct.Struct("<I")
_THRESHOLD = struct.Struct("<f")

READING_SIZE = _READING.size  # 13
_UINT32_MAX = 2**32 - 1


class BleProtocolError(ValueError):
    """Bytes que no cumplen el contrato BLE."""


@dataclass(frozen=True)
class RawReading:
    """Lectura tal como llega por BLE, sin validar: `uT` puede ser NaN o infinito."""

    seq: int
    ts: int
    uT: float
    deferred: int


def _float32_to_decimal(value: float) -> float:
    """Recorta el ruido de float32 a float64 (219.3 → 219.3000030517578 → 219.3)."""
    if math.isnan(value) or math.isinf(value):
        return value
    return float(format(value, ".7g"))


def decode_reading(data) -> RawReading:
    payload = bytes(data)
    if len(payload) != READING_SIZE:
        raise BleProtocolError(
            f"la lectura BLE debe tener {READING_SIZE} bytes, llegaron {len(payload)}"
        )
    seq, ts, ut, deferred = _READING.unpack(payload)
    return RawReading(seq=seq, ts=ts, uT=_float32_to_decimal(ut), deferred=deferred)


def encode_reading(raw: RawReading) -> bytes:
    """Inverso de `decode_reading`, para simular el firmware en los tests."""
    return _READING.pack(raw.seq, raw.ts, raw.uT, raw.deferred)


def encode_time(epoch_seconds: int) -> bytes:
    if isinstance(epoch_seconds, bool) or not isinstance(epoch_seconds, int):
        raise BleProtocolError(f"la hora debe ser un entero: {epoch_seconds!r}")
    # Para el firmware 0 significa "sin hora".
    if not 0 < epoch_seconds <= _UINT32_MAX:
        raise BleProtocolError(f"hora fuera del rango uint32 (> 0): {epoch_seconds}")
    return _TIME.pack(epoch_seconds)


def encode_threshold(threshold_ut: float) -> bytes:
    """El firmware ignora en silencio un umbral <= 0: aquí se rechaza explícitamente."""
    if isinstance(threshold_ut, bool) or not isinstance(threshold_ut, (int, float)):
        raise BleProtocolError(f"el umbral debe ser numérico: {threshold_ut!r}")
    value = float(threshold_ut)
    if math.isnan(value) or math.isinf(value) or value <= 0:
        raise BleProtocolError(f"el umbral debe ser finito y mayor que 0: {threshold_ut!r}")
    return _THRESHOLD.pack(value)


def decode_threshold(data) -> float:
    """Inverso de `encode_threshold`, para tests y diagnóstico."""
    return _float32_to_decimal(_THRESHOLD.unpack(bytes(data))[0])


def decode_time(data) -> int:
    """Inverso de `encode_time`, para tests y diagnóstico."""
    return _TIME.unpack(bytes(data))[0]
