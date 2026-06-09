/*
 * AI Smart Energy Meter — NodeMCU ESP8266
 * ────────────────────────────────────────
 * Home load  : 60W  incandescent → ~0.261A  (learned as baseline)
 * Theft load : 100W incandescent → ~0.435A  (triggers alert)
 * ACS_ZERO calibrated to RAW midpoint=548 (average of 538–558 swing)
 * Sampling window increased to 200ms for stable RMS over noisy ADC
 *
 * Pins:
 *   A0  — ACS712 output
 *   D2/D1 (GPIO4/5) — LCD SDA/SCL
 *   D5 (GPIO14) — Relay
 *   D6 (GPIO12) — Buzzer
 *
 * HARDWARE TIP: Add a 10uF capacitor between A0 and GND to reduce noise.
 */

#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>

// ═══════════════════════════════════════════════
//   SETTINGS
// ═══════════════════════════════════════════════
const char* WIFI_SSID     = "Suji";
const char* WIFI_PASSWORD = "sujitha5555";
const char* SERVER_IP     = "172.31.131.18";
const int   SERVER_PORT   = 5000;

// ═══════════════════════════════════════════════
//   PIN DEFINITIONS
// ═══════════════════════════════════════════════
#define PIN_SENSOR  A0
#define PIN_SDA     4    // D2
#define PIN_SCL     5    // D1
#define PIN_RELAY   14   // D5
#define PIN_BUZZER  12   // D6

// ═══════════════════════════════════════════════
//   LCD
// ═══════════════════════════════════════════════
LiquidCrystal_I2C lcd(0x27, 16, 2);

// ═══════════════════════════════════════════════
//   ACS712 CALIBRATION
//   ACS_ZERO  : FIXED to 548 (midpoint of your 538-558 RAW swing)
//   ACS_SENSITIVITY : 100mV/A for ACS712-20A
// ═══════════════════════════════════════════════
#define ACS_ZERO        548.0   // FIXED: was 555, corrected to 548
#define ACS_SENSITIVITY 0.050  // 100mV/A

// ═══════════════════════════════════════════════
//   RELAY
// ═══════════════════════════════════════════════
#define RELAY_ON  LOW
#define RELAY_OFF HIGH

// ═══════════════════════════════════════════════
//   DETECTION THRESHOLDS
// ═══════════════════════════════════════════════
#define OVERLOAD_AMPS   15.0
#define THEFT_JUMP      0.15

#define NOISE_FLOOR     0.03
#define BASELINE_COUNT  30
#define THEFT_CONFIRM   5

// ═══════════════════════════════════════════════
//   GLOBAL STATE
// ═══════════════════════════════════════════════
float         currentRMS      = 0.0;
float         voltageRMS      = 230.0;
float         power           = 0.0;
float         energyKWh       = 0.0;
float         baseline        = 0.0;
int           baselineSamples = 0;
bool          relayON         = true;
bool          theftFound      = false;
bool          wifiOK          = false;
int           theftCount      = 0;
unsigned long lastSend        = 0;
unsigned long lastDisplay     = 0;
unsigned long lastEnergy      = 0;
unsigned long lastBuzzer      = 0;
int           lcdPage         = 0;

// ═══════════════════════════════════════════════
//   FORWARD DECLARATIONS
// ═══════════════════════════════════════════════
float readCurrent();
void  learnBaseline();
void  checkForProblems(unsigned long);
void  triggerAlert(String, unsigned long);
void  updateLCD();
void  sendToServer();
void  connectWiFi();
void  beep(int);
void  threeBeeps();

// ═══════════════════════════════════════════════
//   SETUP
// ═══════════════════════════════════════════════
void setup() {
  Serial.begin(9600);
  delay(500);

  Serial.println("================================");
  Serial.println("  AI Smart Energy Meter");
  Serial.println("  Home=60W(0.261A) Theft=100W(0.435A)");
  Serial.println("  ACS_ZERO=548  Sampling=200ms");
  Serial.println("================================");

  pinMode(PIN_RELAY,  OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_RELAY,  RELAY_ON);
  digitalWrite(PIN_BUZZER, LOW);

  Wire.begin(PIN_SDA, PIN_SCL);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("AI Energy Meter");
  lcd.setCursor(0, 1); lcd.print("Starting up...");
  delay(1000);

  beep(100);
  connectWiFi();

  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("Learning...     ");
  lcd.setCursor(0, 1); lcd.print("60W only please ");

  lastEnergy = millis();
  Serial.println(">> Connect ONLY the 60W bulb now. Learning baseline...");
}

// ═══════════════════════════════════════════════
//   MAIN LOOP
// ═══════════════════════════════════════════════
void loop() {
  unsigned long now = millis();

  currentRMS = readCurrent();
  power      = voltageRMS * currentRMS;

  float hours = (float)(now - lastEnergy) / 3600000.0;
  energyKWh  += (power * hours) / 1000.0;
  lastEnergy  = now;

  if (baselineSamples < BASELINE_COUNT) {
    learnBaseline();
  } else {
    checkForProblems(now);
  }

  if (now - lastDisplay >= 1500) {
    updateLCD();
    lastDisplay = now;
    lcdPage = (lcdPage + 1) % 2;
  }

  if (wifiOK && (now - lastSend >= 2000)) {
    sendToServer();
    lastSend = now;
  }

  if (WiFi.status() != WL_CONNECTED) {
    wifiOK = false;
  }

  delay(200);
}

// ═══════════════════════════════════════════════
//   READ CURRENT
//   FIXED: 200ms window (was 60ms) = 10 full AC cycles
//   Much more stable RMS — reduces effect of RAW 538-558 swing
// ═══════════════════════════════════════════════
float readCurrent() {
  float         sum   = 0.0;
  int           count = 0;
  unsigned long t     = millis();

  float zeroV = (ACS_ZERO / 1024.0);

  while (millis() - t < 200) {      // FIXED: was 60, now 200
    int   raw  = analogRead(PIN_SENSOR);
    float v    = raw / 1024.0;
    float amps = (v - zeroV) / ACS_SENSITIVITY;
    sum  += amps * amps;
    count++;
    delayMicroseconds(200);
  }

  if (count == 0) return 0.0;

  float rms = sqrt(sum / count);

  if (rms < NOISE_FLOOR) rms = 0.0;

  static unsigned long dbgTime = 0;
  if (millis() - dbgTime > 3000) {
    Serial.print("RAW=");     Serial.print(analogRead(PIN_SENSOR));
    Serial.print("  I=");     Serial.print(rms, 3);
    Serial.print("A  P=");   Serial.print(rms * voltageRMS, 1);
    Serial.print("W  Base=");Serial.print(baseline, 3);
    Serial.print("A  Thr="); Serial.println(baseline + THEFT_JUMP, 3);
    dbgTime = millis();
  }

  return rms;
}

// ═══════════════════════════════════════════════
//   LEARN BASELINE
// ═══════════════════════════════════════════════
void learnBaseline() {
  baseline = ((baseline * baselineSamples) + currentRMS)
             / (baselineSamples + 1);
  baselineSamples++;

  lcd.setCursor(0, 1);
  lcd.print("Learn:");
  lcd.print(baselineSamples);
  lcd.print("/30  ");

  if (baselineSamples == BASELINE_COUNT) {
    Serial.println("----------------------------");
    Serial.print("Baseline learned = ");
    Serial.print(baseline, 3);
    Serial.println(" A  (expected ~0.261A for 60W)");
    Serial.print("Theft threshold  = ");
    Serial.println(baseline + THEFT_JUMP, 3);
    Serial.println("----------------------------");

    if (baseline < 0.10) {
      Serial.println("WARNING: Baseline too low! Try ACS_ZERO = 538");
    }
    if (baseline > 0.40) {
      Serial.println("WARNING: Baseline too high! Try ACS_ZERO = 558");
    }

    beep(400);
    lcd.clear();
  }
}

// ═══════════════════════════════════════════════
//   DETECTION
// ═══════════════════════════════════════════════
void checkForProblems(unsigned long now) {

  if (currentRMS > OVERLOAD_AMPS) {
    Serial.print("OVERLOAD! I="); Serial.println(currentRMS, 2);
    triggerAlert("OVERLOAD", now);
    return;
  }

  float theftThreshold = baseline + THEFT_JUMP;

  if (relayON && currentRMS > theftThreshold) {
    theftCount++;
    Serial.print("Theft suspect #"); Serial.print(theftCount);
    Serial.print("  I=");          Serial.print(currentRMS, 3);
    Serial.print("A  Threshold="); Serial.print(theftThreshold, 3);
    Serial.println("A");

    if (theftCount >= THEFT_CONFIRM) {
      theftFound = true;
      Serial.println("*** THEFT CONFIRMED — cutting relay ***");
      triggerAlert("THEFT", now);
    }

  } else {
    theftCount = 0;

    if (currentRMS >= NOISE_FLOOR && currentRMS <= (baseline + 0.05)) {
      baseline = (baseline * 0.97) + (currentRMS * 0.03);
    }

    if (theftFound && currentRMS < theftThreshold) {
      theftFound = false;
      digitalWrite(PIN_RELAY, RELAY_ON);
      relayON = true;
      Serial.println("Current normal — relay restored");
    }
  }
}

// ═══════════════════════════════════════════════
//   TRIGGER ALERT
// ═══════════════════════════════════════════════
void triggerAlert(String type, unsigned long now) {

  if (type == "THEFT" || type == "OVERLOAD") {
    digitalWrite(PIN_RELAY, RELAY_OFF);
    relayON = false;
    Serial.println("Relay OFF — load disconnected");
  }

  if (now - lastBuzzer > 30000) {
    threeBeeps();
    lastBuzzer = now;
  }

  lcd.clear();
  lcd.setCursor(0, 0);
  if (type == "THEFT")    lcd.print("!! THEFT ALERT !!");
  if (type == "OVERLOAD") lcd.print("!! OVERLOAD !!  ");
  lcd.setCursor(0, 1);
  lcd.print("I=");  lcd.print(currentRMS, 2);
  lcd.print("A P=");lcd.print(power, 0); lcd.print("W");
}

// ═══════════════════════════════════════════════
//   LCD DISPLAY
// ═══════════════════════════════════════════════
void updateLCD() {
  lcd.clear();

  if (lcdPage == 0) {
    lcd.setCursor(0, 0);
    lcd.print("V:");  lcd.print(voltageRMS, 0);
    lcd.print(" I:"); lcd.print(currentRMS, 2); lcd.print("A");
    lcd.setCursor(0, 1);
    lcd.print("P:"); lcd.print(power, 1); lcd.print("W");

  } else {
    lcd.setCursor(0, 0);
    lcd.print("E:"); lcd.print(energyKWh, 4); lcd.print("kWh");
    lcd.setCursor(0, 1);
    if      (theftFound)                        lcd.print("**THEFT ALERT** ");
    else if (baselineSamples < BASELINE_COUNT) { lcd.print("Learn:"); lcd.print(baselineSamples); lcd.print("/30"); }
    else if (!relayON)                          lcd.print("Relay:OFF CUT!  ");
    else                                        lcd.print(wifiOK ? "Normal  WiFi:OK " : "Normal  WiFi:-- ");
  }
}

// ═══════════════════════════════════════════════
//   SEND DATA TO PYTHON SERVER
// ═══════════════════════════════════════════════
void sendToServer() {
  WiFiClient  client;
  HTTPClient  http;

  String url = "http://" + String(SERVER_IP) +
               ":"       + String(SERVER_PORT) + "/data";

  http.begin(client, url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(3000);

  String fault = "NORMAL";
  if      (theftFound)                  fault = "THEFT";
  else if (currentRMS > OVERLOAD_AMPS) fault = "OVERLOAD";

  String json;
  json  = "{";
  json += "\"voltage\":"   + String(voltageRMS,  1) + ",";
  json += "\"current\":"   + String(currentRMS,  3) + ",";
  json += "\"power\":"     + String(power,        1) + ",";
  json += "\"energy\":"    + String(energyKWh,    4) + ",";
  json += "\"relay\":"     + String(relayON ? "true" : "false") + ",";
  json += "\"baseline\":"  + String(baseline,     3) + ",";
  json += "\"threshold\":" + String(baseline + THEFT_JUMP, 3) + ",";
  json += "\"fault\":\""   + fault + "\"";
  json += "}";

  int code = http.POST(json);

  if (code == 200) {
    Serial.println("Server: " + http.getString());
  } else {
    Serial.print("HTTP error: "); Serial.println(code);
  }
  http.end();
}

// ═══════════════════════════════════════════════
//   WIFI CONNECTION
// ═══════════════════════════════════════════════
void connectWiFi() {
  Serial.print("Connecting to "); Serial.println(WIFI_SSID);
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("WiFi connecting ");
  lcd.setCursor(0, 1); lcd.print(WIFI_SSID);

  WiFi.disconnect(true);
  delay(1000);
  WiFi.mode(WIFI_STA);
  delay(300);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 60) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    wifiOK = true;
    Serial.print("WiFi OK  IP: "); Serial.println(WiFi.localIP());
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("WiFi Connected! ");
    lcd.setCursor(0, 1); lcd.print(WiFi.localIP());
    delay(2000);
  } else {
    wifiOK = false;
    Serial.println("WiFi FAILED — running offline");
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("WiFi Failed!    ");
    lcd.setCursor(0, 1); lcd.print("Offline mode    ");
    delay(2000);
  }
}

// ═══════════════════════════════════════════════
//   BUZZER HELPERS
// ═══════════════════════════════════════════════
void beep(int ms) {
  digitalWrite(PIN_BUZZER, HIGH);
  delay(ms);
  digitalWrite(PIN_BUZZER, LOW);
}

void threeBeeps() {
  for (int i = 0; i < 3; i++) {
    beep(200);
    delay(150);
  }
}