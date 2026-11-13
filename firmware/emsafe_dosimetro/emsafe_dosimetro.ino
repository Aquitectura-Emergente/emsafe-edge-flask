// EMSafe - Firmware del dosimetro (prototipo)
#include <Wire.h>
#include <LittleFS.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ---------- Pines ----------
const int PIN_SDA = 21, PIN_SCL = 22;
const int PIN_R = 25, PIN_G = 26, PIN_B = 27;
const int PIN_BUZZER = 18;

// ---------- BLE ----------
#define DEVICE_NAME  "EMSafe-DOS-01"
#define SERVICE_UUID "6e7a0001-5a1f-4c3b-9d2e-0000000000a1"
#define READING_UUID "6e7a0002-5a1f-4c3b-9d2e-0000000000a1"
#define TIME_UUID    "6e7a0003-5a1f-4c3b-9d2e-0000000000a1"
#define THRESH_UUID  "6e7a0004-5a1f-4c3b-9d2e-0000000000a1"

// ---------- Parametros ----------
const uint32_t SAMPLE_MS  = 1000;   // 1 lectura por segundo
const uint32_t BUFFER_MAX = 1800;   // unos 30 minutos de lecturas
const char*    BUF_FILE   = "/buffer.bin";

// ---------- Estado ----------
volatile bool connected = false;     // hay un Edge conectado
volatile bool sessionReady = false;  // el Edge ya envio la hora
volatile bool clockValid = false;    // la hora se sincronizo al menos una vez
volatile float thresholdUT = 300.0;  // umbral por defecto; lo reemplaza el Edge
uint32_t epochAtSync = 0, millisAtSync = 0;
uint32_t seq = 0, lost = 0, buffered = 0, lastSample = 0;
bool magOverflow = false;            // algun eje fuera de rango en la ultima lectura
const float SATURATION_UT = 800.0;   // valor reportado cuando el sensor se satura
BLECharacteristic* chReading;

struct __attribute__((packed)) Reading {
  uint32_t seq;       // numero de secuencia
  uint32_t ts;        // hora Unix (0 si aun no se sincronizo)
  float    uT;        // campo medido en microteslas
  uint8_t  deferred;  // 1 = enviada desde el buffer
};

// ---------- Magnetometro (QMC5883L o HMC5883L) ----------
uint8_t magAddr = 0;
float bx0 = 0, by0 = 0, bz0 = 0;     // campo de base, sin iman

bool i2cPresent(uint8_t a) {
  Wire.beginTransmission(a);
  return Wire.endTransmission() == 0;
}

void writeReg(uint8_t a, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(a); Wire.write(reg); Wire.write(val); Wire.endTransmission();
}

bool magBegin() {
  if (i2cPresent(0x0D)) {            // QMC5883L: rango 8 G, modo continuo
    magAddr = 0x0D;
    writeReg(0x0D, 0x0B, 0x01);
    writeReg(0x0D, 0x09, 0x1D);
    return true;
  }
  if (i2cPresent(0x1E)) {            // HMC5883L: rango 8.1 G, modo continuo
    magAddr = 0x1E;
    writeReg(0x1E, 0x00, 0x70);
    writeReg(0x1E, 0x01, 0xE0);
    writeReg(0x1E, 0x02, 0x00);
    return true;
  }
  return false;
}

bool magRead(float &x, float &y, float &z) {   // resultado en uT
  bool qmc = (magAddr == 0x0D);
  uint8_t start = qmc ? 0x00 : 0x03;
  uint8_t n = qmc ? 7 : 6;                     // QMC: 6 bytes de datos + estado
  Wire.beginTransmission(magAddr); Wire.write(start); Wire.endTransmission(false);
  if (Wire.requestFrom(magAddr, n) != n) return false;
  uint8_t b[7];
  for (int i = 0; i < n; i++) b[i] = Wire.read();
  if (qmc) {                                   // X,Y,Z; byte bajo primero; 3000 LSB/G
    if (b[6] & 0x02) magOverflow = true;       // bit OVL: algun eje saturado
    x = (int16_t)(b[1] << 8 | b[0]) / 30.0;
    y = (int16_t)(b[3] << 8 | b[2]) / 30.0;
    z = (int16_t)(b[5] << 8 | b[4]) / 30.0;
  } else {                                     // X,Z,Y; byte alto primero; 230 LSB/G
    int16_t rx = (int16_t)(b[0] << 8 | b[1]);
    int16_t rz = (int16_t)(b[2] << 8 | b[3]);
    int16_t ry = (int16_t)(b[4] << 8 | b[5]);
    if (rx == -4096 || ry == -4096 || rz == -4096) magOverflow = true;
    x = rx / 2.3; y = ry / 2.3; z = rz / 2.3;
  }
  return true;
}

void magReset() {                              // reinicia el sensor tras saturarse
  if (magAddr == 0x0D) { writeReg(0x0D, 0x0A, 0x80); delay(10); }
  magBegin();
}

void calibrate() {                   // promedia 50 muestras sin iman
  float sx = 0, sy = 0, sz = 0; int n = 0;
  for (int i = 0; i < 50; i++) {
    float x, y, z;
    if (magRead(x, y, z)) { sx += x; sy += y; sz += z; n++; }
    delay(20);
  }
  if (n > 0) { bx0 = sx / n; by0 = sy / n; bz0 = sz / n; }
}

float readFieldUT() {                // magnitud del campo sobre la base
  magOverflow = false;
  float sx = 0, sy = 0, sz = 0; int n = 0;
  for (int i = 0; i < 5; i++) {
    float x, y, z;
    if (magRead(x, y, z)) { sx += x; sy += y; sz += z; n++; }
    delay(10);
  }
  if (n == 0) return -1;
  float dx = sx / n - bx0, dy = sy / n - by0, dz = sz / n - bz0;
  float uT = sqrtf(dx * dx + dy * dy + dz * dz);
  if (magOverflow) {                 // saturado: no reportar un valor falso mas bajo
    magReset();
    if (uT < SATURATION_UT) uT = SATURATION_UT;
  }
  return uT;
}

// ---------- LED ----------
void led(bool r, bool g, bool b) {
  digitalWrite(PIN_R, r); digitalWrite(PIN_G, g); digitalWrite(PIN_B, b);
}

// ---------- Hora ----------
uint32_t nowEpoch() {
  return clockValid ? epochAtSync + (millis() - millisAtSync) / 1000 : 0;
}

// ---------- Buffer en flash ----------
void bufferPush(const Reading& r) {
  if (buffered >= BUFFER_MAX) { lost++; return; }
  File f = LittleFS.open(BUF_FILE, FILE_APPEND);
  if (!f) return;
  f.write((const uint8_t*)&r, sizeof(r));
  f.close();
  buffered++;
}

void bufferFlush() {
  if (!LittleFS.exists(BUF_FILE)) { buffered = 0; return; }
  File f = LittleFS.open(BUF_FILE, FILE_READ);
  led(1, 0, 1);                      // morado: sincronizando
  Reading r; uint32_t sent = 0;
  while (connected && f.read((uint8_t*)&r, sizeof(r)) == sizeof(r)) {
    r.deferred = 1;
    chReading->setValue((uint8_t*)&r, sizeof(r));
    chReading->notify();
    sent++;
    delay(15);
  }
  bool done = (f.available() == 0);
  f.close();
  if (done && connected) { LittleFS.remove(BUF_FILE); buffered = 0; }
  Serial.printf("Buffer: %lu lecturas enviadas\n", (unsigned long)sent);
}

// ---------- Callbacks BLE ----------
class ServerCB : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override { connected = true; }
  void onDisconnect(BLEServer* s) override {
    connected = false; sessionReady = false;
    s->startAdvertising();             // volver a anunciarse de inmediato
  }
};

class TimeCB : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    if (c->getLength() < 4) return;
    uint32_t e; memcpy(&e, c->getData(), 4);
    epochAtSync = e; millisAtSync = millis();
    clockValid = true; sessionReady = true;
  }
};

class ThreshCB : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    if (c->getLength() < 4) return;
    float t; memcpy(&t, c->getData(), 4);
    if (t > 0) thresholdUT = t;
  }
};

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_R, OUTPUT); pinMode(PIN_G, OUTPUT); pinMode(PIN_B, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT); digitalWrite(PIN_BUZZER, LOW);
  led(1, 1, 1);                      // blanco: arrancando

  Wire.begin(PIN_SDA, PIN_SCL);
  if (!magBegin()) {
    Serial.println("ERROR: magnetometro no encontrado");
    while (true) { led(1, 0, 0); delay(300); led(0, 0, 0); delay(300); }
  }
  Serial.printf("Magnetometro en 0x%02X. Calibrando: no acerques el iman...\n", magAddr);
  calibrate();

  LittleFS.begin(true);
  if (LittleFS.exists(BUF_FILE)) {
    File f = LittleFS.open(BUF_FILE, FILE_READ);
    buffered = f.size() / sizeof(Reading);
    f.close();
  }
  Serial.printf("Lecturas pendientes en buffer: %lu\n", (unsigned long)buffered);

  BLEDevice::init(DEVICE_NAME);
  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new ServerCB());
  BLEService* svc = server->createService(SERVICE_UUID);
  chReading = svc->createCharacteristic(READING_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  chReading->addDescriptor(new BLE2902());
  BLECharacteristic* chTime = svc->createCharacteristic(TIME_UUID, BLECharacteristic::PROPERTY_WRITE);
  chTime->setCallbacks(new TimeCB());
  BLECharacteristic* chThresh = svc->createCharacteristic(THRESH_UUID, BLECharacteristic::PROPERTY_WRITE);
  chThresh->setCallbacks(new ThreshCB());
  svc->start();
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  Serial.println("Listo. Esperando al Edge por BLE...");
}

// ---------- Loop ----------
void loop() {
  // Respaldo: mientras no haya conexion, asegurar que siga anunciandose
  static uint32_t lastAdv = 0;
  if (!connected && millis() - lastAdv > 5000) {
    BLEDevice::startAdvertising();
    lastAdv = millis();
  }

  // Enviar lo pendiente apenas el Edge esta listo
  if (connected && sessionReady && buffered > 0) bufferFlush();

  if (millis() - lastSample < SAMPLE_MS) return;
  lastSample = millis();

  float uT = readFieldUT();
  if (uT < 0) { Serial.println("Error leyendo el magnetometro"); return; }

  Reading r = { ++seq, nowEpoch(), uT, 0 };
  bool online = connected && sessionReady;
  bool over = uT >= thresholdUT;

  // Alarma local: pitido intermitente mientras se supere el umbral
  digitalWrite(PIN_BUZZER, (over && (seq % 2)) ? HIGH : LOW);

  // LED de estado
  if (over)                         led(1, 0, 0);  // rojo
  else if (uT >= 0.7 * thresholdUT) led(1, 1, 0);  // amarillo
  else if (online)                  led(0, 1, 0);  // verde
  else                              led(0, 0, 1);  // azul

  if (online) {
    chReading->setValue((uint8_t*)&r, sizeof(r));
    chReading->notify();
  } else {
    bufferPush(r);
  }

  Serial.printf("#%lu  %.1f uT  umbral %.0f  %s  (buffer %lu, perdidas %lu)\n",
    (unsigned long)r.seq, uT, (float)thresholdUT, online ? "ENVIADA" : "GUARDADA",
    (unsigned long)buffered, (unsigned long)lost);
}