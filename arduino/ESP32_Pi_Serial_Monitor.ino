/*
  =============================================================================
  🐟 ESP32 - Raspberry Pi Hardware UART Telemetry Bridge (GPIO 14 & 15)
  =============================================================================
  Connects directly to the Raspberry Pi's Hardware UART (GPIO 14 TX, GPIO 15 RX)
  and outputs clean telemetry directly to the Arduino IDE Serial Monitor & Plotter!

  Wiring (Raspberry Pi to ESP32):
    1. Pi GND     (Pin 6)   <--> ESP32 GND        (MANDATORY Shared Ground)
    2. Pi GPIO 14 (Pin 8)   <--> ESP32 GPIO 16 (RX2)  (Pi TX to ESP32 RX)
    3. Pi GPIO 15 (Pin 10)  <--> ESP32 GPIO 17 (TX2)  (Pi RX to ESP32 TX)

  Outputs:
    - Arduino IDE Serial Monitor (115200 baud)
    - Arduino IDE Serial Plotter (Ctrl + Shift + L)
  =============================================================================
*/

// Hardware Serial 2 pins on ESP32
#define RXD2 16   // Connect to Raspberry Pi GPIO 14 (TXD)
#define TXD2 17   // Connect to Raspberry Pi GPIO 15 (RXD)

// Default servo values
float pan_deg  = 90.0;
float tilt_deg = 90.0;
int   pan_us   = 1500;
int   tilt_us  = 1500;

void setup() {
  // Serial to Computer (USB)
  Serial.begin(115200);
  while (!Serial && millis() < 1500);

  // Serial2 to Raspberry Pi Hardware UART (GPIO 14 & 15)
  Serial2.begin(115200, SERIAL_8N1, RXD2, TXD2);

  Serial.println();
  Serial.println("==================================================");
  Serial.println(" ESP32 Pi UART Telemetry Bridge Ready (115200)    ");
  Serial.println("==================================================");
  Serial.println("Wiring:");
  Serial.println("  Pi GND (Pin 6)       <--> ESP32 GND");
  Serial.println("  Pi GPIO 14 TX (Pin 8)<--> ESP32 GPIO 16 (RX2)");
  Serial.println("  Pi GPIO 15 RX (Pin 10)<-> ESP32 GPIO 17 (TX2)");
  Serial.println("==================================================");
}

void loop() {
  // Read complete lines from Raspberry Pi Hardware UART
  if (Serial2.available()) {
    String line = Serial2.readStringUntil('\n');
    line.trim();

    if (line.length() > 0) {
      // Check if line contains comma-separated key:value pairs
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
            pan_deg = val.toFloat();
          } else if (key == "tilt_angle" || key == "tilt") {
            tilt_deg = val.toFloat();
          } else if (key == "pan_us" || key == "pan_pw") {
            pan_us = val.toInt();
          } else if (key == "tilt_us" || key == "tilt_pw") {
            tilt_us = val.toInt();
          }
        }
        idx = nextComma + 1;
      }

      // Output formatted line for Arduino IDE Serial Plotter & Monitor
      Serial.print("Pan_Angle:");
      Serial.print(pan_deg, 1);
      Serial.print(",");
      Serial.print("Tilt_Angle:");
      Serial.print(tilt_deg, 1);
      Serial.print(",");
      Serial.print("Pan_us:");
      Serial.print(pan_us);
      Serial.print(",");
      Serial.print("Tilt_us:");
      Serial.println(tilt_us);
    }
  }

  // Also forward any keystrokes from Serial Monitor to Raspberry Pi
  if (Serial.available()) {
    char c = Serial.read();
    Serial2.write(c);
  }
}
