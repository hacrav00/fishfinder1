/*
  =============================================================================
  🐟 FishFinder ROV - ESP Servo Telemetry Monitor for Arduino IDE
  =============================================================================
  This sketch receives live servo telemetry (Pan, Tilt, Pulse Widths) from the
  Raspberry Pi and outputs it directly to:
    1. Arduino IDE Serial Monitor (115200 baud) -> Clean live text
    2. Arduino IDE Serial Plotter (Ctrl + Shift + L) -> Real-time moving graphs!

  Compatibility: ESP32 / ESP8266 / Arduino
  Supports 2 Connection Modes:
    MODE 1: USB / Serial (ESP connected to Pi via USB or RX/TX pins)
    MODE 2: WiFi (ESP plugged into PC USB, receives live data from Pi wirelessly)
  =============================================================================
*/

#if defined(ESP32)
  #include <WiFi.h>
  #include <WiFiUdp.h>
#elif defined(ESP8266)
  #include <ESP8266WiFi.h>
  #include <WiFiUdp.h>
#endif

// -----------------------------------------------------------------------------
// [CONFIG] Set your Wi-Fi credentials (for Wireless Mode)
// If you are connecting ESP directly via Serial wires, WiFi is optional.
// -----------------------------------------------------------------------------
const char* WIFI_SSID     = "FishFinder_ROV";      // Your router or Pi hotspot SSID
const char* WIFI_PASSWORD = "password123";         // Your Wi-Fi password
const int   UDP_PORT      = 8888;                  // UDP broadcast port from Pi

// Servo State Variables
float panAngle  = 90.0;
float tiltAngle = 90.0;
int   panUS     = 1500;
int   tiltUS    = 1500;

#if defined(ESP32) || defined(ESP8266)
WiFiUDP udp;
char packetBuffer[256];
bool wifiConnected = false;
#endif

// -----------------------------------------------------------------------------
// Print formatted line for Arduino IDE Serial Plotter & Monitor
// Serial Plotter format: "Label1:Value1,Label2:Value2,Label3:Value3,Label4:Value4"
// -----------------------------------------------------------------------------
void outputToPlotterAndMonitor(float pDeg, float tDeg, int pUS, int tUS) {
  // 1. Output formatted variables for Arduino Serial Plotter (Graphs)
  Serial.print("Pan_Angle:");
  Serial.print(pDeg, 1);
  Serial.print(",");
  Serial.print("Tilt_Angle:");
  Serial.print(tDeg, 1);
  Serial.print(",");
  Serial.print("Pan_us:");
  Serial.print(pUS);
  Serial.print(",");
  Serial.print("Tilt_us:");
  Serial.println(tUS);
}

// -----------------------------------------------------------------------------
// Parse incoming telemetry line
// Format: "Pan_Angle:90,Tilt_Angle:90,Pan_us:1500,Tilt_us:1500" or "PAN:90,TILT:90"
// -----------------------------------------------------------------------------
void parseTelemetry(String line) {
  line.trim();
  if (line.length() == 0) return;

  // Check if string contains comma-separated key-values
  int idx = 0;
  while (idx < line.length()) {
    int nextComma = line.indexOf(',', idx);
    if (nextComma == -1) nextComma = line.length();
    String token = line.substring(idx, nextComma);
    token.trim();

    int colon = token.indexOf(':');
    if (colon != -1) {
      String key = token.substring(0, colon);
      String val = token.substring(colon + 1);
      key.trim();
      val.trim();
      key.toLowerCase();

      if (key == "pan_angle" || key == "pan") {
        panAngle = val.toFloat();
      } else if (key == "tilt_angle" || key == "tilt") {
        tiltAngle = val.toFloat();
      } else if (key == "pan_us" || key == "pan_pw") {
        panUS = val.toInt();
      } else if (key == "tilt_us" || key == "tilt_pw") {
        tiltUS = val.toInt();
      }
    }
    idx = nextComma + 1;
  }

  // Update Serial Plotter / Monitor
  outputToPlotterAndMonitor(panAngle, tiltAngle, panUS, tiltUS);
}

void setup() {
  // Start Serial at 115200 baud (Standard for ESP & Arduino)
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n==================================================");
  Serial.println("🐟 FishFinder ROV - ESP Servo Telemetry Monitor");
  Serial.println("Arduino IDE Serial Monitor & Plotter Ready!");
  Serial.println("==================================================");

#if defined(ESP32) || defined(ESP8266)
  // Optional: Connect to WiFi for wireless telemetry from Pi
  if (strlen(WIFI_SSID) > 0 && String(WIFI_SSID) != "FishFinder_ROV") {
    Serial.print("Connecting to Wi-Fi: ");
    Serial.println(WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int timeout = 0;
    while (WiFi.status() != WL_CONNECTED && timeout < 15) {
      delay(500);
      Serial.print(".");
      timeout++;
    }
    if (WiFi.status() == WL_CONNECTED) {
      wifiConnected = true;
      Serial.println("\nWiFi Connected! IP: " + WiFi.localIP().toString());
      udp.begin(UDP_PORT);
      Serial.println("Listening for UDP Servo Telemetry on port " + String(UDP_PORT));
    } else {
      Serial.println("\nWi-Fi not connected. Operating in direct Serial/USB mode.");
    }
  } else {
    Serial.println("Operating in direct Serial/USB mode.");
  }
#endif

  // Initial plot baseline
  outputToPlotterAndMonitor(panAngle, tiltAngle, panUS, tiltUS);
}

String serialBuffer = "";

void loop() {
  // 1. Check for incoming data over Hardware Serial / USB
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        parseTelemetry(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
      if (serialBuffer.length() > 256) serialBuffer = ""; // Safety overflow reset
    }
  }

#if defined(ESP32) || defined(ESP8266)
  // 2. Check for incoming data over Wireless UDP from Pi
  if (wifiConnected) {
    int packetSize = udp.parsePacket();
    if (packetSize > 0) {
      int len = udp.read(packetBuffer, 255);
      if (len > 0) {
        packetBuffer[len] = '\0';
        parseTelemetry(String(packetBuffer));
      }
    }
  }
#endif

  delay(5);
}
