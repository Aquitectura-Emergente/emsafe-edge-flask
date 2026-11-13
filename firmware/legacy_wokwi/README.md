# Firmware ESP32 — EMSafe (equipo GAUSS)

Firmware del dispositivo IoT de EMSafe, desarrollado y probado en **Wokwi** (y cargado
al prototipo físico). Versionado aquí como respaldo — la fuente de verdad de diseño
es este directorio, no el proyecto Wokwi.

## Archivos

| Archivo | Qué es |
|---|---|
| `sketch.ino` | Programa del ESP32 (Arduino C++) |
| `diagram.json` | Cableado del circuito en Wokwi |
| `a3144-hall.chip.c` | Custom chip Wokwi: simula el sensor EMF (DAC proporcional al slider) |
| `a3144-hall.chip.json` | Definición del custom chip (pines + slider "Campo EMF (uT)" 0–1000) |
| `libraries.txt` | Librerías Wokwi (`ArduinoJson`) |

## Hardware / pines

- **Sensor EMF (A3144 custom chip):** OUT → **GPIO34** (ADC). Escala: 0–3.3 V ⇒ 0–1000 µT.
- **LED RGB semáforo** (cátodo común, resistencias 220Ω): R → **GPIO27**, G → **GPIO25**, B → **GPIO26**.
- **Relé (enchufe inteligente):** **GPIO32** (HIGH = corriente conectada).
  ⚠️ En el `diagram.json` NO hay módulo de relé conectado al GPIO32 — en el simulador
  el relé es "virtual" (solo se ve por serial); el prototipo físico sí lo tiene.

## Lógica

- Lee el campo cada **1 s**. Clasifica (igual que edge/backend/web/mobile):
  `SAFE < 100 µT` (verde) · `CAUTION 100–200` (amarillo) · `DANGER ≥ 200` (rojo).
- **Relé automático:** corta en DANGER; reconecta recién por debajo de **150 µT** (histéresis).
- **Anti-saturación:** solo hace POST cuando cambia el nivel o el estado del enchufe
  (mínimo 3 s entre envíos). Payload:
  ```json
  { "deviceId": "EMSAFE-6766-01", "field_uT": 250.4, "level": "DANGER",
    "plug": "OFF", "time": "19/06/2026 18:40:12" }
  ```
- Hora real vía NTP (UTC-5 Perú). Lee la respuesta del POST y muestra `message` por serial.

## ⚠️ Ajustes pendientes (decididos con el equipo, aún NO aplicados)

1. **Apuntar al edge real** — hoy `ENDPOINT_URL` apunta a Beeceptor (mock). Cambiar a:
   `https://<tunel-ngrok>/api/v1/emf-monitoring/data-records` (Wokwi) o
   `http://<IP-LAN-del-PC>:5000/api/v1/emf-monitoring/data-records` (ESP32 físico en la
   misma red). Ojo: la ruta real lleva guiones (`data-records`), no `data_records`.
2. **Autenticación** — agregar el header que el edge exige:
   `http.addHeader("X-API-Key", "emsafe-edge-key-6766");`
3. **Obedecer la orden del usuario (`desiredPlug`)** — el edge ya devuelve `desiredPlug`
   en la respuesta de cada POST (viene del backend: `PATCH /api/client/devices/{id}/plug`
   desde la app). Leerlo en `enviarDataRecord()` y aplicarlo al relé.
   **Regla acordada (profesor): la orden del usuario gana SIEMPRE, incluso en DANGER** —
   el corte automático por umbral solo aplica si el usuario no ha dado una orden.
4. *(Recomendado)* **Heartbeat**: enviar un POST cada ~10 s aunque nada cambie, para que
   la orden del usuario llegue rápido (hoy solo llegaría cuando cambia nivel/enchufe).
5. *(Solo prototipo físico)* WiFi real en `WIFI_SSID`/`WIFI_PASSWORD` (Wokwi usa `Wokwi-GUEST`).
