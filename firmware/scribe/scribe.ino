// Scribe printer firmware.
//
// Agents do not talk to this board directly for weather or layouts. They call
// the host API, which sends the wire format documented in scribe/render.py:
//
//   drape 0|1
//   tailfeed <0-8>
//   text|<size>|<bold>|<inverse>|<payload>
// Wiring. The MC206H label says TTL, 9600 baud, 5-9V. Two plugs:
//   2-pin red/black is heater power. Red goes to a separate 5V >=2A supply,
//   never to the D1 Mini. Black joins D1 GND.
//   4-wire red/yellow/white/black is serial. Yellow is usually printer TX,
//   white usually printer RX, black signal ground. Read the print on the
//   insulation. Do not land the TTL red wire on a GPIO.
//
// Bench retry: D2 receives, D1 transmits.
// Printer TX (usually yellow) -> D2. Printer RX (usually white) <- D1.
#define PRINTER_RX_PIN D2
#define PRINTER_TX_PIN D1
#define PRINTER_RX_NAME "D2"
#define PRINTER_TX_NAME "D1"
#define PRINTER_BAUD 9600
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <SoftwareSerial.h>
#include <ArduinoOTA.h>

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef SCRIBE_TOKEN
#define SCRIBE_TOKEN ""
#endif


static const int MAX_OPS = 48;
static const int MAX_TEXT = 48;

struct Op {
  uint8_t kind;  // 1 = text, 2 = feed
  uint8_t size;
  uint8_t bold;
  uint8_t inverse;
  uint8_t n;
  char text[MAX_TEXT + 1];
};

SoftwareSerial printer(PRINTER_RX_PIN, PRINTER_TX_PIN);
ESP8266WebServer server(80);
Op ops[MAX_OPS];
int opCount = 0;
bool drapeOn = true;
int tailFeed = 3;
bool printing = false;
bool announced = false;
bool wifiWarned = false;
unsigned long lastWifiRetry = 0;
// ESC 7: max heat dots per burst ((n+1)*8), heat time and rest interval in 10 us.
// Tuned on the bench (MC206H on a 5 V breadboard module): 32 dots per burst
// at max heat time prints dark without browning out. Tune live with POST /heat.
uint8_t heatDots = 3;
uint8_t heatTime = 255;
uint8_t heatInterval = 120;
// Pause after each printed line (ms) so the 5 V regulator recovers. Slow is fine.
uint8_t lineDelay = 60;

void connectWifi();
void announce();
void setupRoutes();
bool authorized();
void handleRoot();
void handleHealth();
void handlePrint();
void handleTest();
void handleHeat();
bool parseJob(const String& body, String& error);
bool addText(uint8_t size, uint8_t bold, uint8_t inverse, const String& payload, String& error);
bool addFeed(int n, String& error);
void preparePrinter(bool upsideDown);
void setModes(uint8_t size, uint8_t bold, uint8_t inverse);
void printOp(const Op& op);
void runJob();
void printSlip(const char* top, const char* mid, const char* bottom);

void setup() {
  Serial.begin(115200);
  Serial.println("\n=== scribe ===");
  printer.begin(PRINTER_BAUD);
  delay(300);
  preparePrinter(true);
  connectWifi();
  setupRoutes();
  server.begin();
  // Firmware updates over Wi-Fi (port 8266), so the USB cable can stay on power.
  ArduinoOTA.setHostname("scribe");
  if (strlen(SCRIBE_TOKEN) > 0) ArduinoOTA.setPassword(SCRIBE_TOKEN);
  ArduinoOTA.begin();
  announce();
  if (WiFi.status() != WL_CONNECTED && !wifiWarned) {
    wifiWarned = true;
    if (strlen(WIFI_SSID) == 0) {
      printSlip("SET SECRETS.H", "copy example", "then reflash");
    } else {
      printSlip("WIFI FAILED", "check secrets.h", "serial 115200");
    }
  }
}

void loop() {
  server.handleClient();
  ArduinoOTA.handle();
  MDNS.update();
  if (WiFi.status() == WL_CONNECTED) {
    announce();
    return;
  }
  if (strlen(WIFI_SSID) == 0) return;
  if (millis() - lastWifiRetry < 30000) return;
  lastWifiRetry = millis();
  Serial.println("retrying wifi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void connectWifi() {
  if (strlen(WIFI_SSID) == 0) {
    Serial.println("WIFI_SSID empty. Copy secrets.example.h to secrets.h");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.hostname("scribe");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("wifi");
  for (int attempt = 0; attempt < 20 && WiFi.status() != WL_CONNECTED; attempt++) {
    delay(500);
    Serial.print(".");
    yield();
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("ip ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("wifi failed");
  }
}

void announce() {
  if (announced || WiFi.status() != WL_CONNECTED) return;
  announced = true;
  if (MDNS.begin("scribe")) {
    MDNS.addService("http", "tcp", 80);
    Serial.println("mdns scribe.local");
  }
  String ip = WiFi.localIP().toString();
  printSlip("SCRIBE", ip.c_str(), "scribe.local");
}

void setupRoutes() {
  server.collectHeaders("X-Scribe-Token", "Content-Length");
  server.on("/", HTTP_GET, handleRoot);
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/print", HTTP_POST, handlePrint);
  server.on("/print", HTTP_GET, handlePrint);
  server.on("/test", HTTP_POST, handleTest);
  server.on("/heat", HTTP_POST, handleHeat);
  server.onNotFound(handleNotFound);
}

bool authorized() {
  if (strlen(SCRIBE_TOKEN) == 0) return true;
  if (!server.hasHeader("X-Scribe-Token")) return false;
  return server.header("X-Scribe-Token") == String(SCRIBE_TOKEN);
}

void handleRoot() {
  String page = "scribe\n";
  page += "tx ";
  page += PRINTER_TX_NAME;
  page += "  rx ";
  page += PRINTER_RX_NAME;
  page += "  baud ";
  page += String(PRINTER_BAUD);
  page += "\n";
  if (WiFi.status() == WL_CONNECTED) {
    page += "http://";
    page += WiFi.localIP().toString();
    page += "\n";
  } else {
    page += "wifi down\n";
  }
  page += "POST /print\n";
  server.send(200, "text/plain", page);
}

void handleHealth() {
  String ip = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "";
  String body = "{\"ok\":true,\"name\":\"scribe\",\"ip\":\"";
  body += ip;
  body += "\",\"tx\":\"";
  body += PRINTER_TX_NAME;
  body += "\",\"rx\":\"";
  body += PRINTER_RX_NAME;
  body += "\",\"baud\":";
  body += String(PRINTER_BAUD);
  body += ",\"heat\":[";
  body += String(heatDots);
  body += ",";
  body += String(heatTime);
  body += ",";
  body += String(heatInterval);
  body += ",";
  body += String(lineDelay);
  body += "],\"wifi\":";
  body += WiFi.status() == WL_CONNECTED ? "true" : "false";
  body += "}";
  server.send(200, "application/json", body);
}

void handlePrint() {
  if (server.method() == HTTP_GET) {
    server.send(405, "text/plain", "POST the scribe wire format to /print\n");
    return;
  }
  if (!authorized()) {
    server.send(401, "text/plain", "unauthorized\n");
    return;
  }
  if (printing) {
    server.send(503, "text/plain", "busy\n");
    return;
  }
  if (server.hasHeader("Content-Length") && server.header("Content-Length").toInt() > 4096) {
    server.send(413, "text/plain", "body too large\n");
    return;
  }
  String error;
  if (!parseJob(server.arg("plain"), error)) {
    server.send(400, "text/plain", error + "\n");
    return;
  }
  printing = true;
  runJob();
  printing = false;
  server.send(200, "text/plain", "ok\n");
}

void handleTest() {
  if (!authorized()) {
    server.send(401, "text/plain", "unauthorized\n");
    return;
  }
  if (printing) {
    server.send(503, "text/plain", "busy\n");
    return;
  }
  opCount = 0;
  drapeOn = true;
  tailFeed = 4;
  String error;
  String pins = String(PRINTER_TX_NAME) + " TX  " + PRINTER_RX_NAME + " RX";
  if (!addText(1, 1, 0, "SCRIBE TEST", error) || !addText(1, 0, 0, pins, error)) {
    server.send(500, "text/plain", "test failed\n");
    return;
  }
  printing = true;
  runJob();
  printing = false;
  server.send(200, "text/plain", "ok\n");
}

void handleHeat() {
  if (!authorized()) {
    server.send(401, "text/plain", "unauthorized\n");
    return;
  }
  const char* names[] = {"dots", "time", "interval", "pace"};
  uint8_t* targets[] = {&heatDots, &heatTime, &heatInterval, &lineDelay};
  for (int i = 0; i < 4; i++) {
    if (!server.hasArg(names[i])) continue;
    long value = server.arg(names[i]).toInt();
    if (value < 0 || value > 255 || (i == 1 && value < 3)) {
      server.send(400, "text/plain", String(names[i]) + " out of range\n");
      return;
    }
    *targets[i] = (uint8_t)value;
  }
  handleHealth();
}

void handleNotFound() {
  server.send(404, "text/plain", "not found\n");
}

bool parseJob(const String& body, String& error) {
  opCount = 0;
  drapeOn = true;
  tailFeed = 3;
  bool sawOp = false;
  if (body.length() == 0) {
    error = "empty body";
    return false;
  }
  int start = 0;
  while (start < (int)body.length()) {
    int end = body.indexOf('\n', start);
    if (end < 0) end = body.length();
    String line = body.substring(start, end);
    start = end + 1;
    if (line.endsWith("\r")) line.remove(line.length() - 1);
    if (line.length() == 0 || line.charAt(0) == '#') continue;

    if (line.startsWith("drape ")) {
      if (sawOp) {
        error = "drape must be a header";
        return false;
      }
      String value = line.substring(6);
      if (value == "0") drapeOn = false;
      else if (value == "1") drapeOn = true;
      else {
        error = "drape must be 0 or 1";
        return false;
      }
      continue;
    }
    if (line.startsWith("tailfeed ")) {
      if (sawOp) {
        error = "tailfeed must be a header";
        return false;
      }
      int value = line.substring(9).toInt();
      if (value < 0 || value > 8 || line.substring(9) != String(value)) {
        error = "tailfeed must be 0 to 8";
        return false;
      }
      tailFeed = value;
      continue;
    }
    if (line.startsWith("feed|")) {
      int value = line.substring(5).toInt();
      if (value < 1 || value > 4 || line.substring(5) != String(value)) {
        error = "feed must be 1 to 4";
        return false;
      }
      if (!addFeed(value, error)) return false;
      sawOp = true;
      continue;
    }
    if (line.startsWith("text|")) {
      int p1 = line.indexOf('|');
      int p2 = line.indexOf('|', p1 + 1);
      int p3 = line.indexOf('|', p2 + 1);
      int p4 = line.indexOf('|', p3 + 1);
      if (p1 < 0 || p2 < 0 || p3 < 0 || p4 < 0) {
        error = "text op needs size, bold, inverse, and payload";
        return false;
      }
      int size = line.substring(p1 + 1, p2).toInt();
      int bold = line.substring(p2 + 1, p3).toInt();
      int inverse = line.substring(p3 + 1, p4).toInt();
      if (size < 1 || size > 3 || (bold != 0 && bold != 1) || (inverse != 0 && inverse != 1)) {
        error = "bad text op flags";
        return false;
      }
      if (!addText(size, bold, inverse, line.substring(p4 + 1), error)) return false;
      sawOp = true;
      continue;
    }
    error = "unknown opcode";
    return false;
  }
  if (opCount == 0) {
    error = "no print ops";
    return false;
  }
  return true;
}

bool addText(uint8_t size, uint8_t bold, uint8_t inverse, const String& payload, String& error) {
  if (opCount >= MAX_OPS) {
    error = "too many lines";
    return false;
  }
  if (payload.length() > MAX_TEXT) {
    error = "line too long";
    return false;
  }
  Op& op = ops[opCount++];
  op.kind = 1;
  op.size = size;
  op.bold = bold;
  op.inverse = inverse;
  op.n = 0;
  payload.toCharArray(op.text, sizeof(op.text));
  return true;
}

bool addFeed(int n, String& error) {
  if (opCount >= MAX_OPS) {
    error = "too many lines";
    return false;
  }
  Op& op = ops[opCount++];
  op.kind = 2;
  op.size = 1;
  op.bold = 0;
  op.inverse = 0;
  op.n = n;
  op.text[0] = 0;
  return true;
}

void preparePrinter(bool upsideDown) {
  printer.write(0x1B);
  printer.write('@');
  delay(80);
  printer.write(0x1B);
  printer.write('7');
  printer.write(heatDots);
  printer.write(heatTime);
  printer.write(heatInterval);
  printer.write(0x1B);
  printer.write('t');
  printer.write((uint8_t)0);
  printer.write(0x1B);
  printer.write('2');
  if (upsideDown) {
    printer.write(0x1B);
    printer.write('{');
    printer.write((uint8_t)1);
  }
  yield();
}

void setModes(uint8_t size, uint8_t bold, uint8_t inverse) {
  uint8_t scale = 0x00;
  if (size == 2) scale = 0x11;
  else if (size == 3) scale = 0x22;
  printer.write(0x1B);
  printer.write('a');
  printer.write((uint8_t)0);
  printer.write(0x1D);
  printer.write('!');
  printer.write(scale);
  printer.write(0x1B);
  printer.write('E');
  printer.write(bold ? 1 : 0);
  printer.write(0x1D);
  printer.write('B');
  printer.write(inverse ? 1 : 0);
}

void printOp(const Op& op) {
  if (op.kind == 2) {
    setModes(1, 0, 0);
    for (uint8_t i = 0; i < op.n; i++) printer.write('\n');
    yield();
    return;
  }
  setModes(op.size, op.bold, op.inverse);
  printer.print(op.text);
  printer.write('\n');
  delay(lineDelay);
  yield();
}

void runJob() {
  preparePrinter(drapeOn);
  if (drapeOn) {
    for (int i = opCount - 1; i >= 0; --i) printOp(ops[i]);
  } else {
    for (int i = 0; i < opCount; ++i) printOp(ops[i]);
  }
  setModes(1, 0, 0);
  for (int i = 0; i < tailFeed; ++i) printer.write('\n');
}

void printSlip(const char* top, const char* mid, const char* bottom) {
  preparePrinter(true);
  setModes(1, 0, 0);
  if (bottom && bottom[0]) {
    printer.print(bottom);
    printer.write('\n');
  }
  if (mid && mid[0]) {
    printer.print(mid);
    printer.write('\n');
  }
  setModes(1, 1, 0);
  printer.print(top);
  printer.write('\n');
  setModes(1, 0, 0);
  for (int i = 0; i < 4; ++i) printer.write('\n');
}
