/*
  =============================================================================
  🐟 ESP32 - Raspberry Pi Servo PWM Pulse Reader & Analyzer
  =============================================================================
  Reads hardware PWM pulses directly from Raspberry Pi GPIO pins:
    - Pi GND            --> ESP32 GND (MANDATORY common ground!)
    - Pi GPIO 18 (Pan)  --> ESP32 GPIO 34 (D34) or GPIO 4
    - Pi GPIO 19 (Tilt) --> ESP32 GPIO 35 (D35) or GPIO 14

  Outputs to:
    1. Arduino Serial Monitor (115200 baud)
    2. Arduino Serial Plotter (Ctrl + Shift + L) for live moving multi-colored graphs
  =============================================================================
*/

// =============================================================================
// PIN CONFIGURATION
// Use GPIO 34 & 35 (dedicated input-only pins) OR GPIO 4 & 14
// =============================================================================
#define PIN_PWM1_PAN   34    // Connect to Raspberry Pi GPIO 18 (Physical Pin 12)
#define PIN_PWM2_TILT  35    // Connect to Raspberry Pi GPIO 19 (Physical Pin 35)

// Alternative pins if your ESP32 board does not label 34/35:
// #define PIN_PWM1_PAN   4    // Connect to Raspberry Pi GPIO 18
// #define PIN_PWM2_TILT  14   // Connect to Raspberry Pi GPIO 19

// Interrupt timing variables
volatile unsigned long pan_rise_time  = 0;
volatile unsigned long pan_pulse_us   = 0;
volatile unsigned long tilt_rise_time = 0;
volatile unsigned long tilt_pulse_us  = 0;
volatile unsigned long pan_last_seen  = 0;
volatile unsigned long tilt_last_seen = 0;

// High-speed precision ISR for Pan PWM
void IRAM_ATTR isr_pan() {
  unsigned long now = micros();
  if (digitalRead(PIN_PWM1_PAN) == HIGH) {
    pan_rise_time = now;
  } else {
    if (pan_rise_time > 0) {
      pan_pulse_us = now - pan_rise_time;
      pan_last_seen = millis();
    }
  }
}

// High-speed precision ISR for Tilt PWM
void IRAM_ATTR isr_tilt() {
  unsigned long now = micros();
  if (digitalRead(PIN_PWM2_TILT) == HIGH) {
    tilt_rise_time = now;
  } else {
    if (tilt_rise_time > 0) {
      tilt_pulse_us = now - tilt_rise_time;
      tilt_last_seen = millis();
    }
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000);

  Serial.println();
  Serial.println("==================================================");
  Serial.println(" ESP32 PWM Analyzer for Raspberry Pi Servos Ready ");
  Serial.println("==================================================");
  Serial.println("Wiring Check:");
  Serial.println("  1. Pi GND     <--> ESP32 GND (MANDATORY Shared Ground)");
  Serial.print("  2. Pi GPIO 18 <--> ESP32 GPIO "); Serial.println(PIN_PWM1_PAN);
  Serial.print("  3. Pi GPIO 19 <--> ESP32 GPIO "); Serial.println(PIN_PWM2_TILT);
  Serial.println("==================================================");

  pinMode(PIN_PWM1_PAN, INPUT);
  pinMode(PIN_PWM2_TILT, INPUT);

  // Attach non-blocking hardware interrupts on pin CHANGE
  attachInterrupt(digitalPinToInterrupt(PIN_PWM1_PAN), isr_pan, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_PWM2_TILT), isr_tilt, CHANGE);
}

void loop() {
  // Grab latest values atomically
  noInterrupts();
  unsigned long p_us = pan_pulse_us;
  unsigned long t_us = tilt_pulse_us;
  unsigned long p_last = pan_last_seen;
  unsigned long t_last = tilt_last_seen;
  interrupts();

  unsigned long now_ms = millis();

  // If no pulse received in last 800ms (Pi auto-sleep turned PWM off to prevent buzzing):
  if (now_ms - p_last > 800) {
    p_us = 0;
  }
  if (now_ms - t_last > 800) {
    t_us = 0;
  }

  // Filter out any electrical noise spikes or stuck line holds (> 2600us)
  if (p_us > 2600) p_us = 0;
  if (t_us > 2600) t_us = 0;

  // Calculate angles accurately (1000us = 0 deg, 1500us = 90 deg, 2000us = 180 deg)
  // Signed math avoids unsigned underflow when pulse is under 1000us
  float pan_deg = 0.0;
  if (p_us >= 800 && p_us <= 2400) {
    long diff = (long)p_us - 1000;
    pan_deg = (float)diff * 180.0 / 1000.0;
    if (pan_deg < 0.0) pan_deg = 0.0;
    if (pan_deg > 180.0) pan_deg = 180.0;
  }

  float tilt_deg = 0.0;
  if (t_us >= 800 && t_us <= 2400) {
    long diff = (long)t_us - 1000;
    tilt_deg = (float)diff * 180.0 / 1000.0;
    if (tilt_deg < 0.0) tilt_deg = 0.0;
    if (tilt_deg > 180.0) tilt_deg = 180.0;
  }

  // -------------------------------------------------------------------------
  // FORMAT FOR ARDUINO IDE SERIAL PLOTTER & SERIAL MONITOR:
  // "Label:Value,Label:Value,Label:Value\r\n"
  // -------------------------------------------------------------------------
  Serial.print("PWM1_Pan_us:");
  Serial.print(p_us);
  Serial.print(",");
  Serial.print("PWM2_Tilt_us:");
  Serial.print(t_us);
  Serial.print(",");
  Serial.print("Pan_Deg:");
  Serial.print(pan_deg, 1);
  Serial.print(",");
  Serial.print("Tilt_Deg:");
  Serial.println(tilt_deg, 1);

  delay(50); // 20 updates per second (smooth graph in Serial Plotter)
}
