/*
 * EMSafe - Monitor EMF + Alertas (Safe/Caution/Danger) + Actuador remoto
 * Equipo GAUSS | UPC - Desarrollo de Soluciones IoT
 *
 * Embebido (ESP32 Wokwi): sensor A3144 (custom chip, ADC GPIO34)
 *   + LED RGB semaforo + rele enchufe inteligente.
 *
 * Sistema de alertas:
 *   SAFE    (verde) -> campo < 100 uT
 *   CAUTION (azul)  -> 100 - 200 uT
 *   DANGER  (rojo)  -> >= 200 uT
 *
 * ---- Actuador (rele) ----
 * El rele obedece dos fuentes, con esta PRIORIDAD:
 *   1) Orden del USUARIO (desiredPlug, viene en la respuesta del edge:
 *      mobile -> backend -> edge -> dispositivo).  ** GANA SIEMPRE, incluso
 *      en DANGER ** (regla acordada con el profesor).
 *   2) Si no hay orden del usuario (desiredPlug null) -> logica local: corta
 *      en DANGER con histeresis.
 *
 * Ademas de enviar por cambio de nivel/enchufe, manda un HEARTBEAT periodico
 * para recibir la orden del usuario con baja latencia (el edge devuelve el
 * desiredPlug en cada POST).
 *
 * NOTA: Wokwi no alcanza IPs locales -> exponer el edge con ngrok y poner esa
 * URL en ENDPOINT_URL. La ruta canonica del edge es
 * /api/v1/emf-monitoring/data-records (el edge tambien acepta el alias
 * historico /api/v1/data_records).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>

// ---------- WiFi ----------
#define WIFI_SSID      "Wokwi-GUEST"
#define WIFI_PASSWORD  ""

// ---------- Endpoint del EDGE real (via ngrok) ----------
// Reemplaza <NGROK> por el host que te da `ngrok http 5000` sobre el edge local.
// Ej: https://a1b2c3d4.ngrok-free.app/api/v1/emf-monitoring/data-records
#define ENDPOINT_URL   "https://dot-subaru-thomson-dos.trycloudflare.com/api/v1/emf-monitoring/data-records"
#define API_KEY        "emsafe-edge-key-6766"

// ---------- NTP (Peru UTC-5) ----------
const char* NTP_SERVER     = "pool.ntp.org";
const long  GMT_OFFSET_SEC = -5 * 3600;
const int   DST_OFFSET_SEC = 0;

// ---------- Dispositivo ----------
// Serial del sensor. El edge valida (X-API-Key) contra este device de prueba.
// Para la demo cliente: asignar este serial a un cliente desde el portal
// (panel del tecnico "reclamar sensor" o Devices del admin) para que las
// lecturas enlacen a su cuenta y aparezcan en su app movil.
#define DEVICE_ID      "EMSAFE-6766-01"
#define EMF_PIN        34        // OUT del custom chip -> GPIO34 (ADC)

// LED RGB (catodo comun en Wokwi -> HIGH = encendido)
#define LED_R  27
#define LED_G  25
#define LED_B  26
#define LED_ON  HIGH
#define LED_OFF LOW

// Rele = enchufe inteligente
#define RELE_PIN 32
#define RELE_ON  HIGH
#define RELE_OFF LOW

#define ADC_MAX               4095.0
#define FIELD_MAX_UT          1000.0

// Umbrales del sistema de alertas (uT)
#define UMBRAL_CAUTION_UT     100.0
#define UMBRAL_DANGER_UT      200.0
#define UMBRAL_HISTERESIS_UT  150.0

#define INTERVALO_MS          1000
#define MIN_POST_INTERVAL_MS  3000
#define HEARTBEAT_MS          10000   // POST periodico para traer la orden del usuario

unsigned long ultimaLectura  = 0;
unsigned long ultimoPost     = 0;
String        nivelEnviado   = "";
bool          autoPlug       = true;  // decision local (histeresis)
bool          enchufeOn       = true; // estado real aplicado al rele
bool          enchufeEnviado  = true;
int           enviosTotales   = 0;

// Orden del usuario (desde la app, via edge). valid=false => sin orden.
bool          desiredValid    = false;
bool          desiredOn        = true;

// Cliente TLS GLOBAL reutilizable: crear uno nuevo por POST fragmenta el heap y
// hace fallar los handshakes siguientes (HTTP -1 intermitente en Wokwi).
WiFiClientSecure clientTLS;

void setColor(bool r, bool g, bool b) {
  digitalWrite(LED_R, r ? LED_ON : LED_OFF);
  digitalWrite(LED_G, g ? LED_ON : LED_OFF);
  digitalWrite(LED_B, b ? LED_ON : LED_OFF);
}

String timestampActual() {
  struct tm t;
  char buf[25];
  if (!getLocalTime(&t)) return String("");
  strftime(buf, sizeof(buf), "%d/%m/%Y %H:%M:%S", &t);
  return String(buf);
}

bool wifiOK() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(200);
  return WiFi.status() == WL_CONNECTED;
}

// Envia la lectura + estado real del rele y LEE el desiredPlug de la respuesta.
void enviarDataRecord(float campo_uT, const String& nivel, bool plugOn) {
  if (!wifiOK()) {
    Serial.println("  [POST] WiFi no disponible, se omite envio.");
    return;
  }

  // Keys alineados con lo que espera el edge Flask.
  JsonDocument doc;
  doc["device_id"]  = DEVICE_ID;
  doc["field_uT"]   = round(campo_uT * 10) / 10.0;
  doc["level"]      = nivel;
  doc["plug"]       = plugOn ? "ON" : "OFF";   // estado REAL aplicado al rele
  doc["created_at"] = timestampActual();

  String payload;
  serializeJson(doc, payload);

  HTTPClient http;
  http.begin(clientTLS, ENDPOINT_URL);   // reutiliza el cliente TLS global
  http.setConnectTimeout(15000);
  http.setTimeout(15000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);   // requerido por el edge
  http.addHeader("ngrok-skip-browser-warning", "true");  // evita la interstitial del ngrok free (inofensivo en fisico)
  int code = http.POST(payload);

  enviosTotales++;
  Serial.print("  [POST #"); Serial.print(enviosTotales);
  Serial.print("] -> ");     Serial.print(payload);
  Serial.print("  | HTTP ");  Serial.println(code);

  if (code > 0) {
    String respuesta = http.getString();
    JsonDocument resp;
    if (!deserializeJson(resp, respuesta)) {
      const char* msg = resp["message"];
      if (msg) { Serial.print("  [EDGE] "); Serial.println(msg); }

      // Orden del usuario para el rele. "ON"/"OFF" -> aplica; null/ausente -> local.
      if (resp["desiredPlug"].is<const char*>()) {
        String d = resp["desiredPlug"].as<const char*>();
        desiredValid = true;
        desiredOn    = d.equalsIgnoreCase("ON");
        Serial.print("  [EDGE] desiredPlug="); Serial.println(d);
      } else {
        desiredValid = false;   // sin orden -> vuelve a mandar la logica local
      }
    }
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(LED_R, OUTPUT);
  pinMode(LED_G, OUTPUT);
  pinMode(LED_B, OUTPUT);
  pinMode(RELE_PIN, OUTPUT);
  setColor(false, false, false);
  digitalWrite(RELE_PIN, RELE_ON);

  Serial.println();
  Serial.println("==================================================");
  Serial.println("  EMSafe | Safe/Caution/Danger + Enchufe (Wokwi)");
  Serial.print  ("  Device: "); Serial.println(DEVICE_ID);
  Serial.println("  SAFE=Verde | CAUTION=Azul | DANGER=Rojo");
  Serial.println("  Rele: la orden del usuario (app) gana siempre");
  Serial.println("==================================================");

  WiFi.mode(WIFI_STA);
  clientTLS.setInsecure();   // TLS sin validar cert (una sola vez, no por POST)
  wifiOK();
  configTime(GMT_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER);

  struct tm t;
  unsigned long t0 = millis();
  while (!getLocalTime(&t) && millis() - t0 < 5000) delay(200);
}

void loop() {
  if (millis() - ultimaLectura < INTERVALO_MS) return;
  ultimaLectura = millis();

  int   raw      = analogRead(EMF_PIN);
  float campo_uT = (raw / ADC_MAX) * FIELD_MAX_UT;

  // Sistema de alertas + semaforo
  String nivel;
  if (campo_uT < UMBRAL_CAUTION_UT) {
    nivel = "SAFE";    setColor(false, true,  false);  // verde
  } else if (campo_uT < UMBRAL_DANGER_UT) {
    nivel = "CAUTION"; setColor(false, false, true);   // azul
  } else {
    nivel = "DANGER";  setColor(true,  false, false);  // rojo
  }

  // Logica LOCAL del rele (fallback): corta en DANGER, con histeresis.
  if (campo_uT >= UMBRAL_DANGER_UT) {
    autoPlug = false;
  } else if (campo_uT < UMBRAL_HISTERESIS_UT) {
    autoPlug = true;
  }

  // Decision final: la orden del usuario GANA SIEMPRE (incluso en DANGER).
  enchufeOn = desiredValid ? desiredOn : autoPlug;
  digitalWrite(RELE_PIN, enchufeOn ? RELE_ON : RELE_OFF);

  // Consola local
  Serial.print("[");        Serial.print(DEVICE_ID);
  Serial.print("] raw=");   Serial.print(raw);
  Serial.print("  campo="); Serial.print(campo_uT, 1);
  Serial.print(" uT  alerta="); Serial.print(nivel);
  Serial.print("  enchufe="); Serial.print(enchufeOn ? "ON" : "OFF");
  Serial.print(enchufeOn == autoPlug ? "" : "(usuario)");
  Serial.println();

  // Envio: por cambio de nivel/enchufe O heartbeat (para traer desiredPlug).
  bool cambio       = (nivel != nivelEnviado) || (enchufeOn != enchufeEnviado);
  bool heartbeat    = (millis() - ultimoPost > HEARTBEAT_MS);
  bool intervaloOk  = (nivelEnviado == "") || (millis() - ultimoPost > MIN_POST_INTERVAL_MS);
  if ((cambio || heartbeat) && intervaloOk) {
    enviarDataRecord(campo_uT, nivel, enchufeOn);
    nivelEnviado   = nivel;
    enchufeEnviado = enchufeOn;
    ultimoPost     = millis();

    // Aplica de inmediato la orden recien recibida (sin esperar al proximo ciclo).
    enchufeOn = desiredValid ? desiredOn : autoPlug;
    digitalWrite(RELE_PIN, enchufeOn ? RELE_ON : RELE_OFF);
  }
}
