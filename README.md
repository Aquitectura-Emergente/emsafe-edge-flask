# EMSafe Edge Hogar

## Contrato MQTT

Versión del esquema **1.0** (revisión 1.0.1: aclaraciones sin cambio de formato). Es el contrato
compartido con el contenedor de ingesta del backend. Cualquier cambio sube la versión y se avisa
al equipo de backend.

Es el mismo contrato que usa el Edge industrial. En el hogar cambian solo `deviceType`
(`FIXED_SENSOR`) y `edgeNodeId` (`EDGE-HOGAR-01`). El nivel SAFE / CAUTION / DANGER es local:
no viaja en estos mensajes.

> El test `tests/test_contract_payload.py` lee el **primer bloque JSON de esta sección** (el
> ejemplo de telemetría) y lo compara con lo que genera el código. Si cambias el ejemplo o el
> serializador, cambia los dos.

### Tópicos

| Tópico | Sentido | QoS | Retained | Publica | Consume |
|---|---|---|---|---|---|
| `iot/{deviceId}/telemetry` | Edge → nube | 1 | No | Edge | Ingesta (`MqttTelemetrySubscriber`) |
| `iot/{deviceId}/status` *(propuesto)* | Edge → nube | 1 | Sí | Edge | Ingesta (conectividad del dispositivo) |
| `iot/{deviceId}/config` *(propuesto)* | Nube → Edge | 1 | Sí | Device Management | Edge |
| `edge/{edgeNodeId}/status` *(propuesto)* | Edge → nube | 1 | Sí (LWT) | Edge / broker | Ingesta (monitoreo de nodos) |

`deviceId` es el identificador del dispositivo registrado en Device Management (prototipo:
`EMSafe-DOS-01`, que es también su nombre BLE).

### Telemetría — `iot/{deviceId}/telemetry`

```json
{
  "schemaVersion": "1.0",
  "messageId": "6f1c2a9e-8d2b-4c7e-9a51-2f0b7c3d4e10",
  "organizationId": "org-demo",
  "edgeNodeId": "EDGE-HOGAR-01",
  "deviceId": "EMSafe-DOS-01",
  "deviceType": "FIXED_SENSOR",
  "publishedAt": "2026-11-12T15:04:05Z",
  "readings": [
    {
      "sequence": 1234,
      "capturedAt": "2026-11-12T15:04:04Z",
      "capturedAtSource": "DEVICE_CLOCK",
      "receivedAt": "2026-11-12T15:04:04Z",
      "magneticFluxDensityUT": 219.5,
      "origin": "REALTIME",
      "quality": "VALID"
    }
  ]
}
```

| Campo | Tipo | Regla |
|---|---|---|
| `schemaVersion` | string | `"1.0"`. La ingesta rechaza versiones mayores desconocidas. |
| `messageId` | UUID | Único por mensaje. Un reintento conserva el mismo `messageId`. |
| `organizationId` | string | Configurado en el Edge (`EMSAFE_ORGANIZATION_ID`). |
| `edgeNodeId` | string | Identificador del nodo Edge (`EMSAFE_EDGE_NODE_ID`). |
| `deviceId` | string | Igual al del tópico. |
| `deviceType` | enum | `PERSONAL_DOSIMETER` \| `FIXED_SENSOR`. |
| `publishedAt` | ISO-8601 UTC | Momento del intento de publicación (se actualiza en cada reintento). |
| `readings[].sequence` | int ≥ 0 | Secuencia del firmware. Se reinicia si el dispositivo se reinicia. |
| `readings[].capturedAt` | ISO-8601 UTC | Hora de captura. Si el dispositivo no tenía hora (`ts = 0`), el Edge usa `receivedAt`. |
| `readings[].capturedAtSource` | enum | `DEVICE_CLOCK` \| `EDGE_RECEIVED`. |
| `readings[].receivedAt` | ISO-8601 UTC | Hora en que el Edge recibió la lectura por BLE. |
| `readings[].magneticFluxDensityUT` | number ≥ 0 | Densidad de flujo magnético en µT. |
| `readings[].origin` | enum | `REALTIME` \| `DEFERRED` (SyncOrigin). |
| `readings[].quality` | enum | `VALID` \| `SATURATED` (≥ `EMSAFE_SATURATION_UT`, 790 µT por defecto). |

**Filtrado:** las lecturas no numéricas, negativas o NaN se descartan con un registro en el log
y no se publican. Las saturadas **sí** se publican, con `quality = SATURATED`.

**Lecturas saturadas:** `SATURATED` indica que el campo fue **de al menos**
`magneticFluxDensityUT`, posiblemente mayor, porque el sensor llegó a su tope. La ingesta y
quienes consuman la telemetría **no deben descartarlas**: deben usar el valor como mínimo
(criterio conservador) y marcar el periodo como afectado por lecturas fuera de rango. Descartarlas
subestimaría la exposición.

### ReadingIdentity (deduplicación)

- **En el Edge:** `deviceId + sequence + ts crudo` (el `ts` tal como llega por BLE, incluido 0).
  El firmware reenvía el buffer completo desde el inicio si la conexión se corta a mitad. Con
  `ts = 0`, `capturedAt` sale de `receivedAt`, así que usarlo como clave haría que cada reenvío
  pareciera nuevo.
- **`capturedAt` se fija en la primera recepción** y queda guardado. Un reenvío BLE se descarta
  antes de guardarse, y un reintento MQTT publica el mismo `capturedAt`.
- **En la ingesta:** `deviceId + sequence + capturedAt`, equivalente a la del Edge gracias a la regla
  anterior.
- **Limitación conocida:** si el sensor se reinicia más de una vez sin conectarse a un Edge,
  su buffer puede tener lecturas de varios arranques con el mismo `sequence` y `ts = 0`. Solo se
  conserva la primera, y el Edge registra una advertencia cuando `sequence` retrocede con `ts = 0`.
  Se resolverá con un `bootId` en el firmware (cambio futuro del contrato BLE).
- `capturedAt` tiene resolución de 1 s (el `ts` del firmware está en segundos).

### Lotes y reintentos

- Un mensaje lleva lecturas de **un solo dispositivo y un solo `origin`**. Las de tiempo real van de
  a 1 por mensaje; las diferidas, en lotes de hasta 50. Los mensajes se publican en orden de llegada.
- **Outbox local (SQLite):** toda lectura válida se guarda primero. Una lectura se marca publicada
  **solo tras el PUBACK** (QoS 1). Si el broker cae, se acumula y se reenvía en orden al reconectar.
- El reintento reenvía **el mismo mensaje**: mismo `messageId` y mismas lecturas, con `publishedAt`
  actualizado. La ingesta es idempotente por ReadingIdentity.

### Estado del dispositivo — `iot/{deviceId}/status` (retained)

Lo publica el Edge al conectar o perder el BLE con el sensor.

```json
{
  "schemaVersion": "1.0",
  "deviceId": "EMSafe-DOS-01",
  "edgeNodeId": "EDGE-HOGAR-01",
  "connectivity": "ONLINE",
  "changedAt": "2026-11-12T15:00:00Z",
  "firmwareVersion": null,
  "batteryLevel": null
}
```

`connectivity`: `ONLINE` \| `OFFLINE`. `firmwareVersion` y `batteryLevel` van en `null` hasta que
el firmware los exponga por BLE.

### Configuración del dispositivo — `iot/{deviceId}/config` (retained)

La publica Device Management al registrar el dispositivo o cuando cambia el umbral. En el hogar,
`alertThresholdUT` reemplaza al umbral de DANGER del espacio, que también decide el corte del
enchufe. El Edge la guarda y la escribe en el sensor por BLE al conectar y cuando cambia.

```json
{
  "schemaVersion": "1.0",
  "deviceId": "EMSafe-DOS-01",
  "configVersion": 3,
  "alertThresholdUT": 300.0,
  "definedBy": "SSO_RESPONSIBLE",
  "updatedAt": "2026-11-12T14:00:00Z"
}
```

- Se ignora una configuración con `configVersion` menor o igual a la vigente.
- También se rechaza, con un registro en el log, si `alertThresholdUT` no es numérico, es ≤ 0 o es
  ≥ `EMSAFE_SATURATION_UT`. En ese caso se conserva la última válida.
- Sin configuración, el Edge usa `EMSAFE_DANGER_UT` (200 µT por defecto).

### Estado del nodo Edge — `edge/{edgeNodeId}/status` (retained, Last Will)

Al conectar al broker, el Edge publica:

```json
{"status": "ONLINE", "changedAt": "2026-11-12T15:00:00Z"}
```

Antes de conectar, deja registrado un **Last Will** que el broker publica si el Edge cae sin
despedirse:

```json
{"status": "OFFLINE"}
```

El Last Will **no lleva `changedAt`**: se registra al conectar, antes de saber cuándo caerá el Edge.

### Seguridad

- **Producción:** MQTT v5 sobre TLS (puerto 8883), credenciales por nodo Edge y ACL por
  organización.
- **Prototipo:** Mosquitto local en el puerto 1883 sin TLS. Es una desviación declarada; TLS y
  credenciales se activan por variables de entorno (`EMSAFE_MQTT_TLS`, `EMSAFE_MQTT_USERNAME`,
  `EMSAFE_MQTT_PASSWORD`).
