# EMSafe Edge Hogar

Nodo **Edge del segmento hogar** de EMSafe. Recibe por **BLE** las lecturas del sensor fijo de la
vivienda, las valida, deduplica y guarda, **clasifica el nivel** (SAFE / CAUTION / DANGER),
controla el **enchufe inteligente** y publica la telemetría por **MQTT** hacia la ingesta de
**Device Management**. Expone una API local de diagnóstico con Flask.

```
Sensor fijo (ESP32 #1) --BLE--> Edge hogar --HTTP/WiFi--> Actuador (ESP32 #2 + relé)
                                    |    ^
                                    |    +-- config (umbral) <-- Device Management
                                    +--MQTT--> Broker --> Ingesta Device Management
```

**Qué hace:** clasifica cada lectura con umbrales configurables, corta la energía del aparato
cuando una lectura en tiempo real llega a DANGER y la restablece cuando el campo baja (US04), con
el corte automático desactivable (US05). La orden ON/OFF del usuario desde la app gana sobre la
lógica automática.

**Qué no hace:** el nivel es local (relé, log y API); no viaja en el contrato MQTT. No calcula
dosis ni exposición acumulada. La alarma inmediata (LED y buzzer) la decide el firmware del sensor
con el umbral de DANGER que le escribe el Edge.

El sensor es el mismo ESP32 y el mismo firmware que el dosímetro del Edge industrial. El firmware
acepta **una sola conexión BLE**: se usa un Edge a la vez.

## Contenido

- [Arquitectura](#arquitectura)
- [Reglas del hogar](#reglas-del-hogar)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Ejecución](#ejecución)
- [Prueba manual con el sensor y el actuador](#prueba-manual-con-el-sensor-y-el-actuador)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Tests y CI](#tests-y-ci)
- [Contrato MQTT](#contrato-mqtt)

---

## Arquitectura

Un proceso Python. Flask atiende la API en el hilo principal y el Edge (BLE, MQTT, enchufe)
corre en un hilo aparte con su propio loop `asyncio`. DDD con un bounded context,
**`monitoring`**. Los puertos son `Protocol` en `domain/ports.py` y se inyectan por constructor,
así los tests reemplazan BLE, broker, actuador y SQLite por dobles.

```text
monitoring/
  domain/          Reading, OutboxMessage, DeviceConfig, value objects (ReadingIdentity,
                   MagneticFluxDensity, SyncOrigin, ReadingQuality, CapturedAtSource,
                   AlertLevel, AlertThresholds), ReadingFactory, BatchPolicy y los puertos
  application/     ingest_reading, control_plug, publish_outbox, apply_device_config,
                   track_connectivity, purge_outbox
  infrastructure/  ble_protocol (bytes <-> datos, sin bleak), ble_sensor_client (bleak),
                   mqtt_payloads (esquema 1.0), mqtt_client (paho), models, sqlite_outbox,
                   sqlite_config_store, http_actuator, backend_desired_plug
  interfaces/      runner (arma y orquesta el Edge), edge_thread (loop asyncio en un hilo),
                   api (Flask)
shared/            config (variables de entorno), database (SQLite en modo WAL), clock
firmware/          sketches del sensor y del actuador, y el prototipo Wokwi anterior
tests/             unittest con dobles
app.py             arranque de Flask y del Edge, apagado ordenado
```

### Flujo de una lectura

```text
notificación BLE ──> cola asyncio ──> IngestReading ──> outbox SQLite (PENDING)
 (solo decodifica)                    valida, marca       │            │
                                      saturada, nivel,    │            └──> reglas del enchufe
                                      dedupe              ▼                 (solo tiempo real)
                                                     PublishOutbox: lotes con messageId,
                                                     QoS 1, espera el PUBACK
                                                          ▼
                                                     PUBLISHED (se conserva 7 días para deduplicar)
```

- **BLE:** escanea por nombre (y por UUID de servicio si viene en el anuncio) y al conectar
  ejecuta la secuencia *notify → umbral → hora*. El callback solo decodifica y encola, porque el
  sensor vacía su buffer a una lectura cada 15 ms. Reintenta a 1, 2, 4… hasta 30 s.
- **Ingesta:** descarta valores no numéricos, NaN, infinitos o negativos, marca `SATURATED`
  desde `EMSAFE_SATURATION_UT`, calcula el nivel con los umbrales vigentes y deduplica por
  `deviceId + sequence + ts crudo`.
- **Enchufe:** las reglas corren en su propia tarea y el HTTP al actuador en un hilo, así un
  actuador lento o apagado nunca frena la ingesta. Un fallo se registra y se reintenta con la
  siguiente lectura.
- **Outbox y MQTT:** igual que el Edge industrial. Una lectura se marca publicada **solo tras el
  PUBACK**; sin broker se acumula y se reenvía en orden al reconectar. Last Will del nodo
  registrado antes de conectar.
- **Apagado** (Ctrl+C): se detiene Flask, luego el BLE (sensor `OFFLINE`), las tareas, y por
  último MQTT con el `OFFLINE` del Edge. Lo no publicado queda en el outbox.

## Reglas del hogar

| Regla | Detalle |
|---|---|
| Clasificación | SAFE por debajo de `EMSAFE_CAUTION_UT`, CAUTION desde ahí y DANGER desde `EMSAFE_DANGER_UT` (inclusivos por abajo). Una lectura `SATURATED` es DANGER. |
| Umbral del sensor | Al conectar se escribe el de DANGER, así el LED y el buzzer coinciden con el corte. Si llega `iot/{deviceId}/config`, su `alertThresholdUT` reemplaza a DANGER y se escribe en caliente. Si queda por debajo de CAUTION, CAUTION baja con él. |
| Corte (US04) | Lectura **en tiempo real** en DANGER → `GET /off`. |
| Restablecimiento | Cuando el campo baja de `EMSAFE_RESTORE_UT` (80 % de DANGER por defecto) → `GET /on`, solo si el corte lo hizo la lógica automática. |
| Diferidas | Solo se registran y se publican: describen el pasado y nunca accionan el relé. |
| US05 | `EMSAFE_AUTO_CUTOFF_ENABLED=false` desactiva el corte y el restablecimiento automáticos. |
| Orden del usuario | Si `EMSAFE_BACKEND_URL` está definido, se consulta `GET /api/v1/devices/{id}/plug` cada 5 s como máximo. Una orden ON u OFF gana sobre la lógica automática. |

## Requisitos

Preparación desde cero en Windows.

### Python y Bluetooth

- Python 3.10 o superior ([python.org](https://www.python.org/downloads/)). Al instalarlo, marca
  *Add python.exe to PATH*.
- Laptop con Bluetooth LE, activado en *Configuración → Bluetooth y dispositivos*.
- La laptop y el actuador en la misma red WiFi (el hotspot del celular, ver más abajo).

### Mosquitto

1. Descarga el instalador de Windows desde
   [mosquitto.org/download](https://mosquitto.org/download/) e instálalo con las opciones por
   defecto. Queda como servicio *Mosquitto Broker* en el puerto 1883.
2. Comprueba que el servicio esté en ejecución con `sc query mosquitto` en cmd. Si no dice
   `RUNNING`, inícialo desde `services.msc` o con `net start mosquitto` en un cmd de administrador.
3. Agrega `C:\Program Files\mosquitto` al `PATH`: busca *Editar las variables de entorno del
   sistema* → *Variables de entorno* → `Path` → *Nuevo*.
4. Abre **dos ventanas nuevas de cmd** (las que ya estaban abiertas no ven el `PATH` nuevo). En la
   primera `mosquitto_sub -t prueba -v` y en la segunda `mosquitto_pub -t prueba -m hola`. La
   primera debe mostrar `prueba hola`.

### MQTT Explorer (opcional, recomendado)

Cliente gráfico para ver lo que publica el Edge y publicar la config sin pelear con las comillas.

1. Descárgalo desde [mqtt-explorer.com](https://mqtt-explorer.com/) o desde Microsoft Store.
2. Crea una conexión con host `localhost`, puerto `1883`, sin usuario ni contraseña y con
   *Encryption (tls)* desactivado. Conéctate.
3. **Ver la telemetría:** en el árbol abre `iot → EMSafe-DOS-01 → telemetry`.
4. **Publicar la config:** en *Publish* escribe el tópico `iot/EMSafe-DOS-01/config`, elige el
   formato `json`, pega el payload (ver [Umbral desde la nube](#umbral-desde-la-nube)), marca
   **retain**, elige QoS 1 y pulsa *Publish*.

### Arduino IDE y placas

1. Instala el driver USB de las placas. El prototipo usa **CP2102**: driver *CP210x VCP* de
   Silicon Labs.
2. Instala [Arduino IDE](https://www.arduino.cc/en/software). En *Boards Manager* instala el
   paquete **esp32** de Espressif Systems. Los dos sketches usan solo librerías incluidas en ese
   paquete.
3. En *Tools* configura, para cada placa:
   - *Board*: **ESP32 Dev Module**;
   - *Partition Scheme*: **Huge APP (3MB No OTA/1MB SPIFFS)** (la pila BLE no entra en la
     partición por defecto);
   - *Port*: el COM que aparece al conectar la placa.

### Sensor fijo (ESP32 #1)

Abre `firmware/emsafe_dosimetro/emsafe_dosimetro.ino` y pulsa *Upload*. En el *Serial Monitor*
(115200 baudios) se ven sus mensajes. Se anuncia por BLE como `EMSafe-DOS-01`.

### Actuador (ESP32 #2 + relé)

1. Activa el **hotspot del celular en 2.4 GHz**: el ESP32 no se conecta a redes de 5 GHz. En
   iPhone, activa *Maximizar compatibilidad* en *Compartir Internet*.
2. Crea `firmware/actuador_emsafe/secrets.h` copiando `secrets.example.h` en la misma carpeta, y
   reemplaza `TU_RED` y `TU_CLAVE` por el nombre y la clave de tu hotspot. En cmd:

   ```bat
   copy firmwarectuador_emsafe\secrets.example.h firmwarectuador_emsafe\secrets.h
   ```

   `secrets.h` está en `.gitignore` y no se versiona. Sin él, el sketch no compila. El relé va en
   el GPIO 26, alimentado a 3V3 (módulo activo en LOW).
3. Pulsa *Upload* y abre el *Serial Monitor* (115200). Al conectarse muestra
   `IP del actuador: 172.20.10.x`. Esa es la URL base: `http://172.20.10.x`.
4. Conecta la laptop al mismo hotspot y prueba en el navegador `http://172.20.10.x/off` y
   `.../on`: el relé debe conmutar.

## Instalación

```powershell
cd emsafe-edge-flask
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

En Linux o macOS: `python3 -m venv .venv` y `.venv/bin/pip install -r requirements.txt`.

Dependencias: `flask`, `bleak`, `paho-mqtt` 2.x, `peewee` y `requests`. Los tests usan
`unittest` de la librería estándar.

## Configuración

Todo se configura por variables de entorno, incluidas las credenciales. Un valor inválido detiene
el arranque con un mensaje que nombra la variable.

| Variable | Uso | Default |
|---|---|---|
| `EMSAFE_EDGE_NODE_ID` | Identificador del nodo Edge | `EDGE-HOGAR-01` |
| `EMSAFE_ORGANIZATION_ID` | Organización del nodo | `org-demo` |
| `EMSAFE_DEVICE_NAME` | Nombre BLE del sensor (es también su `deviceId`) | `EMSafe-DOS-01` |
| `EMSAFE_CAUTION_UT` | Desde qué valor una lectura es CAUTION | `100` |
| `EMSAFE_DANGER_UT` | Desde qué valor es DANGER; se escribe en el sensor | `200` |
| `EMSAFE_RESTORE_UT` | Restablecer el enchufe por debajo de este valor | 80 % de DANGER |
| `EMSAFE_SATURATION_UT` | Desde qué valor una lectura se marca `SATURATED` | `790` |
| `EMSAFE_AUTO_CUTOFF_ENABLED` | Corte automático (US05) | `true` |
| `EMSAFE_ACTUATOR_URL` | URL base del actuador, p. ej. `http://172.20.10.5` | vacío = sin enchufe |
| `EMSAFE_BACKEND_URL` | Backend para consultar la orden del usuario | vacío = sin orden |
| `EMSAFE_MQTT_HOST` / `EMSAFE_MQTT_PORT` | Broker MQTT | `localhost` / `1883` |
| `EMSAFE_MQTT_TLS` | Activa TLS (`true`/`false`) | `false` |
| `EMSAFE_MQTT_USERNAME` / `EMSAFE_MQTT_PASSWORD` | Credenciales del nodo | vacío |
| `EMSAFE_DB_PATH` | Archivo SQLite del outbox y la config | `emsafe_edge.db` |
| `EMSAFE_OUTBOX_RETENTION_DAYS` | Días que se conservan las lecturas ya publicadas (mínimo 1) | `7` |
| `EMSAFE_HTTP_HOST` | Interfaz donde escucha la API local. `0.0.0.0` la abre a la red | `127.0.0.1` |
| `EMSAFE_LOG_LEVEL` | Nivel de log (`DEBUG`, `INFO`, `WARNING`…) | `INFO` |
| `EMSAFE_EDGE_DEBUG` | Debug de Flask. Solo para depurar: expone la consola de Werkzeug a la red | `false` |

Se exige `0 < CAUTION ≤ DANGER < SATURATION` y `0 < RESTORE < DANGER`. Si una config de la nube
baja DANGER por debajo de `EMSAFE_RESTORE_UT`, se usa el 80 % del nuevo DANGER.

## Ejecución

```powershell
cd emsafe-edge-flask
$env:EMSAFE_ACTUATOR_URL = "http://172.20.10.5"
.\.venv\Scripts\python.exe app.py
```

O con el script, que crea el `.venv` la primera vez:

```powershell
.\run-local.ps1 -ActuatorUrl http://172.20.10.5
```

La API queda en `http://localhost:5000`. Es **solo de diagnóstico** y **no tiene
autenticación**: por eso escucha por defecto únicamente en `127.0.0.1`. Las aplicaciones (web y
móvil) no la consumen: leen los datos del backend, que los recibe por MQTT. Solo cambia
`EMSAFE_HTTP_HOST` (por ejemplo a `0.0.0.0`) en una red de confianza y mientras haga falta.


- `GET /health`: estado del Edge, del BLE, de MQTT, del actuador (último estado del enchufe,
  orden del usuario, último error), lecturas pendientes y umbrales vigentes.
- `GET /api/v1/emf-monitoring/readings?limit=50&deviceId=EMSafe-DOS-01`: últimas lecturas con su
  nivel, `quality` y `origin`.

`emsafe_edge.db` (y sus `-wal`/`-shm`) se crea en la carpeta desde la que se ejecuta y está en
`.gitignore`.

## Prueba manual con el sensor y el actuador

### Preparación

1. Comprueba que el servicio *Mosquitto Broker* esté en ejecución (`sc query mosquitto`).
2. Enciende el sensor, el actuador y el Bluetooth de la laptop. Laptop y actuador en el mismo
   hotspot; anota la IP del actuador del *Serial Monitor*.
3. Cierra el Edge industrial u otra app conectada al sensor: el firmware acepta **una sola
   conexión BLE**.
4. Conecta al relé un aparato de prueba (una lámpara, por ejemplo).

### Paso a paso

**Terminal 1 (cmd) — ver lo que publica el Edge:**

```bat
mosquitto_sub -t "iot/#" -t "edge/#" -v
```

**Terminal 2 (PowerShell) — Edge:**

```powershell
cd emsafe-edge-flask
$env:EMSAFE_ACTUATOR_URL = "http://172.20.10.5"
.\.venv\Scripts\python.exe app.py
```

**Qué se espera:**

- En el log: `Conectado al broker MQTT` y `Conectado a EMSafe-DOS-01 (umbral 200.0 µT, ...)`.
  Luego una línea por segundo con el valor, el nivel y `REALTIME`.
- En la terminal 1: `edge/EDGE-HOGAR-01/status` con `ONLINE`, `iot/EMSafe-DOS-01/status` con
  `"connectivity":"ONLINE"` y un `iot/EMSafe-DOS-01/telemetry` por segundo con
  `"deviceType":"FIXED_SENSOR"`. Si el sensor tenía lecturas guardadas, primero llegan lotes
  `DEFERRED` de hasta 50.
- `http://localhost:5000/health` muestra `ble[0].connected: true` y `mqtt.connected: true`.

### Corte y restablecimiento del enchufe

Usa **un solo imán**, con el polo hacia el sensor, y acércalo **en línea recta**. Con varios
imanes, o con uno que gira, los campos se pueden restar y la lectura puede *bajar* al acercarlo.

1. Acerca el imán hasta pasar 200 µT: el sensor enciende la alarma local, el log muestra
   `Enchufe OFF: DANGER (...)` y la lámpara se apaga. `/health` muestra `actuator.plug: OFF`.
2. Aléjalo: mientras el campo esté entre 160 y 200 µT (CAUTION) el enchufe sigue apagado. Bajo
   160 µT el log muestra `Enchufe ON: campo bajo el umbral de restablecimiento` y la lámpara
   vuelve.
3. Muy cerca, el sensor satura (800 µT o más): la lectura se publica con `quality` `SATURATED` y
   cuenta como DANGER.
4. **Actuador caído:** desenchúfalo y repite el paso 1. El log registra `Actuador sin respuesta`,
   la telemetría sigue saliendo y `/health` muestra el error en `actuator.lastError`.
5. **US05:** reinicia el Edge con `$env:EMSAFE_AUTO_CUTOFF_ENABLED = "false"`. Las lecturas en
   DANGER ya no apagan la lámpara.
6. **Diferidas:** apaga el Edge, acerca el imán unos segundos y aléjalo, y vuelve a arrancar. Las
   lecturas en DANGER llegan como `DEFERRED` y no accionan el relé.

### Umbral desde la nube

Publica la config con **MQTT Explorer** o con **cmd**. No uses PowerShell: altera las comillas del
JSON al pasarlo a `mosquitto_pub` y el Edge recibe un JSON roto.

En **cmd**:

```bat
mosquitto_pub -r -q 1 -t iot/EMSafe-DOS-01/config -m "{\"schemaVersion\":\"1.0\",\"deviceId\":\"EMSafe-DOS-01\",\"configVersion\":1,\"alertThresholdUT\":300.0,\"definedBy\":\"SSO_RESPONSIBLE\",\"updatedAt\":\"2026-11-12T14:00:00Z\"}"
```

El log muestra `Config de EMSafe-DOS-01 aplicada` y `Umbral 300.0 µT escrito en EMSafe-DOS-01`.
Desde ahí DANGER (alarma local y corte) es 300 µT y el restablecimiento 240 µT; `/health` muestra
los umbrales nuevos. Reenviar el mismo `configVersion` se ignora; `alertThresholdUT` 800 se rechaza.
Para volver a `EMSAFE_DANGER_UT` borra el retained (`mosquitto_pub -r -t iot/EMSafe-DOS-01/config -n`)
**y** la fila guardada en `device_configs`, o usa otro `EMSAFE_DB_PATH`.

### Corte de nube y de BLE

- Detén *Mosquitto Broker* (`net stop mosquitto` en un cmd de administrador): el Edge sigue
  guardando y controlando el enchufe. Al iniciarlo de nuevo publica las acumuladas en orden.
- Apaga el Bluetooth unos 60 s: se publica `OFFLINE` en `iot/EMSafe-DOS-01/status`; al volver,
  `ONLINE`, las diferidas sin duplicados y luego tiempo real.

### Apagado

- Ctrl+C en la terminal del Edge publica `OFFLINE` del sensor y del Edge.
- Si el proceso muere sin despedirse, el broker publica el Last Will `{"status":"OFFLINE"}` en
  `edge/EDGE-HOGAR-01/status` en unos 45 s.

## Limitaciones conocidas

- **Estado inicial del enchufe desconocido.** Al arrancar, el Edge no sabe si el relé está
  encendido; el primer DANGER manda `/off` igual. El firmware del actuador arranca con el aparato
  encendido.
- **Orden del usuario.** Se consulta como máximo cada 5 s y solo al llegar lecturas en tiempo real:
  sin sensor conectado no se aplica.
- **Nivel con los umbrales del momento.** El nivel se guarda al recibir la lectura; si después
  cambia el umbral, las lecturas anteriores conservan el nivel con el que llegaron.
- **Sensor del prototipo.** El magnetómetro (QMC5883L) mide **campo estático**, no la componente
  ELF, y satura en unos 800 µT. Una lectura `SATURATED` significa que el campo fue **de al menos
  ese valor**. Un imán muy cerca puede magnetizar el sensor y dejar un desplazamiento; se corrige
  reiniciando el sensor lejos de imanes.
- **Reinicios del sensor sin hora.** Las lecturas con `ts = 0` salen con
  `capturedAtSource = EDGE_RECEIVED` y la hora de recepción. Si el sensor se reinicia varias veces
  sin conectarse, su buffer puede repetir `sequence`: solo se conserva la primera (falta un
  `bootId` en el firmware).
- **Un solo sensor por Edge** y **resolución de 1 s** en `capturedAt`.
- El actuador se controla por HTTP sin autenticación, en la red local.
- `firmwareVersion` y `batteryLevel` se publican en `null`.

## Tests y CI

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -b
.\.venv\Scripts\python.exe -m compileall -q app.py monitoring shared tests
```

Los tests usan dobles y SQLite en memoria, sin BLE, broker ni red. Cubren el dominio, la
clasificación, las reglas del enchufe (incluidas las diferidas y la orden del usuario), el outbox,
los adaptadores y la API. `tests/test_contract_payload.py` compara el JSON que genera el código con
los ejemplos de [Contrato MQTT](#contrato-mqtt). El CI corre lo mismo en Python 3.11 en cada push y
pull request.

---

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
