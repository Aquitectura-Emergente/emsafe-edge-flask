# EMSafe Edge Hogar

Nodo **Edge del segmento hogar** de EMSafe. Recibe por **BLE** las lecturas del sensor fijo de la
vivienda, las valida, deduplica y guarda, **clasifica el nivel** (SAFE / CAUTION / DANGER),
controla el **enchufe inteligente** y publica la telemetría por **MQTT** hacia la ingesta de
**Device Management**. Expone una API local de diagnóstico con Flask.

```
Sensor fijo (ESP32 #1) --BLE--> Edge hogar --HTTP/WiFi--> Actuador (ESP32 #2 + relé)
                                    |    ^
                                    |    +-- orden del usuario (iot/{plugId}/command)
                                    +--MQTT--> Broker --> Ingesta Device Management
                                         telemetría, estados, iot/{plugId}/state
```

**Qué hace:** clasifica cada lectura con umbrales fijos configurables, aplica la orden ON/OFF
que el usuario envía desde la app por MQTT y publica el estado del enchufe. Como opción de
seguridad (US05), puede cortar la energía cuando una lectura en tiempo real llega a DANGER y
restablecerla cuando el campo baja (US04); viene desactivada.

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
  domain/          Reading, OutboxMessage, PlugCommand, value objects (ReadingIdentity,
                   MagneticFluxDensity, SyncOrigin, ReadingQuality, CapturedAtSource,
                   AlertLevel, AlertThresholds, PlugState…), ReadingFactory, BatchPolicy
                   y los puertos
  application/     ingest_reading, control_plug, publish_outbox, track_connectivity,
                   purge_outbox
  infrastructure/  ble_protocol (bytes <-> datos, sin bleak), ble_sensor_client (bleak),
                   mqtt_payloads (esquema 1.0), mqtt_client (paho), models, sqlite_outbox,
                   http_actuator
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
- **Enchufe:** las órdenes y la regla automática corren en un hilo, así un actuador lento o
  apagado nunca frena la ingesta. Cada cambio se publica en `iot/{plugId}/state`.
- **Outbox y MQTT:** igual que el Edge industrial. Una lectura se marca publicada **solo tras el
  PUBACK**; sin broker se acumula y se reenvía en orden al reconectar. Last Will del nodo
  registrado antes de conectar.
- **Apagado** (Ctrl+C): se detiene Flask, luego el BLE (sensor `OFFLINE`), las tareas, y por
  último MQTT con el `OFFLINE` del Edge. Lo no publicado queda en el outbox.

## Reglas del hogar

| Regla | Detalle |
|---|---|
| Clasificación | SAFE por debajo de `EMSAFE_CAUTION_UT`, CAUTION desde ahí y DANGER desde `EMSAFE_DANGER_UT` (inclusivos por abajo). Una lectura `SATURATED` es DANGER. |
| Umbral del sensor | Al conectar se escribe el de DANGER (`EMSAFE_DANGER_UT`), así el LED y el buzzer coinciden con el corte. Los umbrales del hogar son fijos: solo vienen de `EMSAFE_CAUTION_UT` y `EMSAFE_DANGER_UT`. |
| Orden del usuario | Llega por `iot/{plugId}/command` (QoS 1). El Edge acciona el actuador y publica `iot/{plugId}/state` con `source` `USER`. Es idempotente por `commandId`, y se ignora si `requestedAt` difiere más de 60 s de la hora del Edge. Si el actuador falla, se publica el estado sin cambios y se registra en el log. |
| Corte automático (US05) | Opción de seguridad, desactivada por defecto (`EMSAFE_AUTO_CUTOFF_ENABLED`). Activada: lectura **en tiempo real** en DANGER → `GET /off` (US04), y bajo `EMSAFE_RESTORE_UT` (80 % de DANGER por defecto) → `GET /on`, solo si el corte lo hizo la regla. Publica el estado con `source` `AUTO`. |
| Prioridad | La orden del usuario gana: un OFF no lo deshace la regla automática, y un ON durante DANGER no se vuelve a cortar hasta que el campo baje del umbral de restablecimiento. |
| Diferidas | Solo se registran y se publican: describen el pasado y nunca accionan el relé. |
| Arranque | El Edge no sabe en qué estado está el relé: publica `UNKNOWN` con `source` `STARTUP` y republica el último estado cada vez que se conecta al broker. |

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

Cliente gráfico para ver lo que publica el Edge y publicar mensajes sin pelear con las comillas.

1. Descárgalo desde [mqtt-explorer.com](https://mqtt-explorer.com/) o desde Microsoft Store.
2. Crea una conexión con host `localhost`, puerto `1883`, sin usuario ni contraseña y con
   *Encryption (tls)* desactivado. Conéctate.
3. **Ver la telemetría:** en el árbol abre `iot → EMSafe-DOS-01 → telemetry`.


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
| `EMSAFE_DEVICE_NAME` | Nombre BLE con el que se busca el sensor | `EMSafe-DOS-01` |
| `EMSAFE_DEVICE_ID` | `deviceId` publicado por MQTT (telemetría y estado) | vacío = el nombre BLE |
| `EMSAFE_CAUTION_UT` | Desde qué valor una lectura es CAUTION | `100` |
| `EMSAFE_DANGER_UT` | Desde qué valor es DANGER; se escribe en el sensor | `200` |
| `EMSAFE_RESTORE_UT` | Restablecer el enchufe por debajo de este valor | 80 % de DANGER |
| `EMSAFE_SATURATION_UT` | Desde qué valor una lectura se marca `SATURATED` | `790` |
| `EMSAFE_AUTO_CUTOFF_ENABLED` | Corte automático por DANGER (US05) | `false` |
| `EMSAFE_ACTUATOR_URL` | URL base del actuador, p. ej. `http://172.20.10.5` | vacío = sin enchufe |
| `EMSAFE_PLUG_ID` | Identificador del enchufe en los tópicos `command` y `state` | `EMSafe-PLUG-01` |
| `EMSAFE_MQTT_HOST` / `EMSAFE_MQTT_PORT` | Broker MQTT | `localhost` / `1883` |
| `EMSAFE_MQTT_TLS` | Activa TLS (`true`/`false`) | `false` |
| `EMSAFE_MQTT_USERNAME` / `EMSAFE_MQTT_PASSWORD` | Credenciales del nodo | vacío |
| `EMSAFE_DB_PATH` | Archivo SQLite del outbox | `emsafe_edge.db` |
| `EMSAFE_OUTBOX_RETENTION_DAYS` | Días que se conservan las lecturas ya publicadas (mínimo 1) | `7` |
| `EMSAFE_HTTP_HOST` | Interfaz donde escucha la API local. `0.0.0.0` la abre a la red | `127.0.0.1` |
| `EMSAFE_LOG_LEVEL` | Nivel de log (`DEBUG`, `INFO`, `WARNING`…) | `INFO` |
| `EMSAFE_EDGE_DEBUG` | Debug de Flask. Solo para depurar: expone la consola de Werkzeug a la red | `false` |

Se exige `0 < CAUTION ≤ DANGER < SATURATION` y `0 < RESTORE < DANGER`.

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

- `GET /health`: estado del Edge, del BLE, de MQTT, del enchufe (estado, `source`, último
  `commandId`, último error), lecturas pendientes y umbrales.
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
- En la terminal 1 también aparece `iot/EMSafe-PLUG-01/state` con `"state":"UNKNOWN"` y
  `"source":"STARTUP"`.
- `http://localhost:5000/health` muestra `ble[0].connected: true` y `mqtt.connected: true`.

### Orden del usuario

La app publica la orden en `iot/EMSafe-PLUG-01/command`. La forma más fácil de simularla es el
script `tools/send_plug_command.py`: genera un `commandId` nuevo y `requestedAt` con la hora UTC
actual, publica con QoS 1 y espera la confirmación del broker. Desde la raíz del repo, en otra
terminal:

```powershell
.\.venv\Scripts\python.exe tools\send_plug_command.py OFF
.\.venv\Scripts\python.exe tools\send_plug_command.py ON
```

Usa el mismo broker y el mismo `plugId` que el Edge (`EMSAFE_MQTT_HOST`, `EMSAFE_PLUG_ID`…), o
se pueden indicar con `--host`, `--port` y `--plug-id`. Imprime el tópico y el JSON publicado.

1. Envía `OFF`: la lámpara se apaga, el log muestra `Enchufe OFF: orden del usuario <commandId>`
   y en la terminal 1 aparece `iot/EMSafe-PLUG-01/state` con `"state":"OFF"`, ese `commandId` y
   `"source":"USER"`.
2. Envía `ON`: la lámpara vuelve.
3. **Actuador caído:** desenchúfalo y envía una orden. El log registra `Actuador sin respuesta` y
   `Orden ... sin efecto`, y se publica el estado anterior con el `commandId` de la orden.

**Alternativa: MQTT Explorer.** Sirve para probar los casos que el script no genera (una orden
repetida o vieja). En *Publish* escribe el tópico `iot/EMSafe-PLUG-01/command` **sin espacios**
(un espacio al inicio o al final crea otro tópico y el Edge no recibe nada), elige el formato
`json`, QoS 1, **sin** retain, y pega:

```text
{"schemaVersion":"1.0","commandId":"cmd-001","plugId":"EMSafe-PLUG-01","action":"OFF","requestedBy":"USER","requestedAt":"2026-11-13T15:10:00Z"}
```

`requestedAt` debe ser la hora **UTC** (no la local) y estar dentro de los **60 segundos**
anteriores al envío: cámbiala justo antes de publicar. Fuera de esa ventana la orden se ignora con
una advertencia en el log.

- Publica **el mismo** mensaje dos veces: la segunda vez el log dice `Orden cmd-001 repetida` y no
  pasa nada.
- Publica una orden con un `requestedAt` de hace unos minutos: se ignora.

### Corte automático (opcional)

Reinicia el Edge con `$env:EMSAFE_AUTO_CUTOFF_ENABLED = "true"`. Usa **un solo imán**, con el polo
hacia el sensor, y acércalo **en línea recta**: con varios imanes, o con uno que gira, los campos
se pueden restar y la lectura puede *bajar* al acercarlo.

1. Acerca el imán hasta pasar 200 µT: suena la alarma local, el log muestra
   `Enchufe OFF: DANGER (...)`, la lámpara se apaga y el estado sale con `"source":"AUTO"`.
2. Aléjalo: entre 160 y 200 µT el enchufe sigue apagado. Bajo 160 µT vuelve, con `source` `AUTO`.
3. Con el imán cerca, publica una orden `ON`: la lámpara vuelve y no se corta de nuevo hasta que
   el campo baje de 160 µT y vuelva a pasar 200 µT.
4. Muy cerca, el sensor satura (800 µT o más): la lectura sale con `quality` `SATURATED` y cuenta
   como DANGER.
5. **Diferidas:** apaga el Edge, acerca el imán unos segundos y aléjalo, y vuelve a arrancar. Las
   lecturas en DANGER llegan como `DEFERRED` y no accionan el relé.

### Corte de nube y de BLE

- Detén *Mosquitto Broker* (`net stop mosquitto` en un cmd de administrador): el Edge sigue
  guardando las lecturas (y, si está activado, aplicando el corte automático). Al iniciarlo de
  nuevo publica las acumuladas en orden y republica el estado del enchufe.
- Apaga el Bluetooth unos 60 s: se publica `OFFLINE` en `iot/EMSafe-DOS-01/status`; al volver,
  `ONLINE`, las diferidas sin duplicados y luego tiempo real.

### Apagado

- Ctrl+C en la terminal del Edge publica `OFFLINE` del sensor y del Edge.
- Si el proceso muere sin despedirse, el broker publica el Last Will `{"status":"OFFLINE"}` en
  `edge/EDGE-HOGAR-01/status` en unos 45 s.

## Limitaciones conocidas

- **Estado inicial del enchufe desconocido.** Al arrancar, el Edge no sabe si el relé está
  encendido y publica `UNKNOWN`. El firmware del actuador arranca con el aparato encendido.
- **Órdenes y reloj.** La ventana de 60 s compara `requestedAt` con el reloj del Edge: si la app o
  la laptop tienen la hora desfasada, las órdenes se ignoran. La idempotencia por `commandId` se
  guarda en memoria; tras reiniciar el Edge la cubre la misma ventana de 60 s.
- **Sin sesión persistente.** El Edge se conecta con `clean_start`: una orden publicada mientras
  estaba desconectado del broker no le llega.
- **Nivel con los umbrales del momento.** El nivel se guarda al recibir la lectura; si se
  cambian los umbrales y se reinicia el Edge, las lecturas anteriores conservan su nivel.
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
.\.venv\Scripts\python.exe -m compileall -q app.py monitoring shared tests tools
```

Los tests usan dobles y SQLite en memoria, sin BLE, broker ni red. Cubren el dominio, la
clasificación, las órdenes y el estado del enchufe, el corte automático (incluidas las diferidas), el outbox,
los adaptadores y la API. `tests/test_contract_payload.py` compara el JSON que genera el código con
los ejemplos de [Contrato MQTT](#contrato-mqtt). El CI corre lo mismo en Python 3.11 en cada push y
pull request.

---

## Contrato MQTT

Versión del esquema **1.0** (revisión 1.0.1: aclaraciones sin cambio de formato). Es el contrato
compartido con el contenedor de ingesta del backend. Cualquier cambio sube la versión y se avisa
al equipo de backend.

Es el mismo contrato que usa el Edge industrial. En el hogar cambian `deviceType`
(`FIXED_SENSOR`) y `edgeNodeId` (`EDGE-HOGAR-01`), y se suman los tópicos del enchufe inteligente
(ver [Extensión del hogar](#extensión-del-hogar-enchufe-inteligente)). El nivel SAFE / CAUTION /
DANGER es local: no viaja en estos mensajes.

> El test `tests/test_contract_payload.py` lee el **primer bloque JSON de esta sección** (el
> ejemplo de telemetría) y lo compara con lo que genera el código. Si cambias el ejemplo o el
> serializador, cambia los dos.

### Tópicos

| Tópico | Sentido | QoS | Retained | Publica | Consume |
|---|---|---|---|---|---|
| `iot/{deviceId}/telemetry` | Edge → nube | 1 | No | Edge | Ingesta (`MqttTelemetrySubscriber`) |
| `iot/{deviceId}/status` *(propuesto)* | Edge → nube | 1 | Sí | Edge | Ingesta (conectividad del dispositivo) |
| `edge/{edgeNodeId}/status` *(propuesto)* | Edge → nube | 1 | Sí (LWT) | Edge / broker | Ingesta (monitoreo de nodos) |
| `iot/{plugId}/command` *(hogar)* | Nube → Edge | 1 | No | Backend (orden de la app) | Edge hogar |
| `iot/{plugId}/state` *(hogar)* | Edge → nube | 1 | Sí | Edge hogar | Backend |

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

### Extensión del hogar: enchufe inteligente

Solo los usa el Edge hogar. `plugId` identifica el enchufe (`EMSAFE_PLUG_ID`, prototipo
`EMSafe-PLUG-01`).

**Orden — `iot/{plugId}/command`** (QoS 1, sin retain):

```json
{
  "schemaVersion": "1.0",
  "commandId": "3b8e1f7a-2c4d-4e9b-8a1f-6d5c4b3a2e10",
  "plugId": "EMSafe-PLUG-01",
  "action": "OFF",
  "requestedBy": "USER",
  "requestedAt": "2026-11-12T15:10:00Z"
}
```

| Campo | Tipo | Regla |
|---|---|---|
| `commandId` | string | Único por orden. Una orden con un `commandId` ya procesado se ignora. |
| `plugId` | string | Igual al del tópico. |
| `action` | enum | `ON` \| `OFF`. |
| `requestedBy` | enum | `USER`. |
| `requestedAt` | ISO-8601 UTC | Se ignora si difiere más de 60 s de la hora del Edge. |

**Estado — `iot/{plugId}/state`** (QoS 1, retained):

```json
{
  "schemaVersion": "1.0",
  "plugId": "EMSafe-PLUG-01",
  "state": "OFF",
  "commandId": "3b8e1f7a-2c4d-4e9b-8a1f-6d5c4b3a2e10",
  "source": "USER",
  "changedAt": "2026-11-12T15:10:01Z"
}
```

| Campo | Tipo | Regla |
|---|---|---|
| `state` | enum | `ON` \| `OFF` \| `UNKNOWN` (el Edge todavía no accionó el relé). |
| `commandId` | string \| null | La orden que originó el mensaje; `null` si no lo originó una orden. |
| `source` | enum | `USER` (orden), `AUTO` (corte automático) \| `STARTUP` (arranque del Edge). |
| `changedAt` | ISO-8601 UTC | Momento del último cambio de estado. |

- Se publica tras cada orden y cada acción automática, al arrancar (`UNKNOWN`, `STARTUP`) y cada
  vez que el Edge se conecta al broker.
- Si el actuador no responde a una orden, se publica el estado **sin cambios** con el `commandId`
  de esa orden y `source` `USER`: la app ve que la orden no surtió efecto.

### Seguridad

- **Producción:** MQTT v5 sobre TLS (puerto 8883), credenciales por nodo Edge y ACL por
  organización.
- **Prototipo:** Mosquitto local en el puerto 1883 sin TLS. Es una desviación declarada; TLS y
  credenciales se activan por variables de entorno (`EMSAFE_MQTT_TLS`, `EMSAFE_MQTT_USERNAME`,
  `EMSAFE_MQTT_PASSWORD`).
