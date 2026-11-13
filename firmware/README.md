# Firmware

| Carpeta | Placa | Qué hace |
|---|---|---|
| `emsafe_dosimetro/` | ESP32 #1 + magnetómetro QMC5883L, LED RGB y buzzer | Sensor fijo del hogar (y dosímetro del Edge industrial). Mide una vez por segundo, transmite por BLE como `EMSafe-DOS-01` y guarda hasta 1800 lecturas cuando no hay Edge conectado. La alarma local usa el umbral que le escribe el Edge. |
| `actuador_emsafe/` | ESP32 #2 + módulo relé en el GPIO 26 | Enchufe inteligente. Se conecta por WiFi y atiende `GET /off` (corta la energía) y `GET /on` (la restablece). Relé activo en LOW alimentado a 3V3. |
| `legacy_wokwi/` | Simulación en Wokwi | Prototipo anterior: sensor A3144 simulado, relé en el mismo ESP32 y envío por POST HTTP. Ya no lo usa este Edge; se conserva como referencia. |

Los dos sketches actuales usan solo librerías del paquete **esp32** de Espressif. En Arduino IDE:
*Board* **ESP32 Dev Module** y *Partition Scheme* **Huge APP (3MB No OTA/1MB SPIFFS)**.

El contrato BLE del sensor está en `monitoring/infrastructure/ble_protocol.py` y no debe
cambiarse sin actualizar los dos Edge.

Antes de cargar el actuador, cambia `SSID` y `PASS` por los de tu hotspot de 2.4 GHz. Los pasos
completos están en el README principal, en *Requisitos*.
