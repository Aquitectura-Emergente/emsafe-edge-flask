// EMSafe - Nodo actuador (simula el enchufe inteligente)
#include <WiFi.h>
#include <WebServer.h>

const char* SSID = "iPhone de Carlos";
const char* PASS = "contraseniai18n";
const int PIN_RELAY = 26;

WebServer server(80);

// Modulo activo en LOW, alimentado con 3V3
void relayOn()  { digitalWrite(PIN_RELAY, LOW);  }   // apaga el dispositivo
void relayOff() { digitalWrite(PIN_RELAY, HIGH); }   // dispositivo encendido

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RELAY, OUTPUT);
  relayOff();                              // dispositivo encendido al iniciar

  WiFi.begin(SSID, PASS);
  Serial.print("Conectando a WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nIP del actuador: %s\n", WiFi.localIP().toString().c_str());

  server.on("/off", []() { relayOn();  server.send(200, "text/plain", "Dispositivo apagado"); });
  server.on("/on",  []() { relayOff(); server.send(200, "text/plain", "Dispositivo encendido"); });
  server.begin();
}

void loop() {
  server.handleClient();
}