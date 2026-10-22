// Radio performance test for the Seeed Studio XIAO ESP32S3 Sense.
//
// The camera module stays unplugged. This sketch never starts it.
// One buffer is allocated in internal memory, the only memory the Wi-Fi
// block can transmit from. Each send times only the write of that buffer.

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy include/secrets.h.example to include/secrets.h and set the Wi-Fi name and password."
#endif

#include <esp_heap_caps.h>
#include <esp_timer.h>

#include <WiFi.h>

#include <stdio.h>
#include <string.h>

namespace {

// Same size as the JPEGs the picture test was sending, so the send time
// can be compared with that test. 20 KB is 20480 bytes.
constexpr size_t kBytes = 20 * 1024;
constexpr uint16_t kPort = 80;

WiFiServer server(kPort);
uint8_t *buffer = nullptr;
bool started = false;

uint32_t sinceUs(int64_t startedAt) {
  int64_t delta = esp_timer_get_time() - startedAt;
  if (delta <= 0) {
    return 0;
  }
  if (delta > 10000000) {
    return 10000000;
  }
  return static_cast<uint32_t>(delta);
}

bool writeAll(WiFiClient &client, const uint8_t *data, size_t length) {
  size_t sent = 0;
  while (sent < length) {
    int wrote = client.write(data + sent, length - sent);
    if (wrote <= 0) {
      return false;
    }
    sent += static_cast<size_t>(wrote);
  }
  return true;
}

bool prepareBuffer() {
  buffer = static_cast<uint8_t *>(heap_caps_malloc(kBytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (buffer == nullptr) {
    Serial.println("radio performance failed");
    return false;
  }
  for (size_t i = 0; i < kBytes; i++) {
    buffer[i] = static_cast<uint8_t>(i & 0xff);
  }
  return true;
}

bool joinWifi() {
  if (strlen(WIFI_SSID) == 0) {
    Serial.println("wifi failed");
    return false;
  }
  // The password stays in this image. It is not also written into the Wi-Fi
  // store, so erasing the sketch removes it.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Joining %s\n", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > 20000) {
      Serial.println("wifi failed");
      return false;
    }
    delay(200);
  }
  Serial.printf("ready http://%s\n", WiFi.localIP().toString().c_str());
  return true;
}

void serve(WiFiClient &client) {
  char line[96];
  snprintf(line, sizeof(line), "radio_performance mac=%s bytes=%u\n", WiFi.macAddress().c_str(), static_cast<unsigned>(kBytes));
  client.print(line);

  while (client.connected()) {
    int64_t wifiStarted = esp_timer_get_time();
    bool wrote = writeAll(client, buffer, kBytes);
    uint32_t wifiUs = sinceUs(wifiStarted);
    if (!wrote) {
      return;
    }
    snprintf(line, sizeof(line), "metric wifi_us=%lu rssi=%d\n", static_cast<unsigned long>(wifiUs), WiFi.RSSI());
    client.print(line);
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  // Opening the port resets the chip. Wait past that, then print, so a reader
  // that flushes the old bytes still sees this boot.
  delay(300);
  Serial.println("radio performance");
  if (!prepareBuffer() || !joinWifi()) {
    return;
  }
  server.begin();
  started = true;
}

void loop() {
  if (!started) {
    delay(1000);
    return;
  }
  WiFiClient client = server.accept();
  if (!client) {
    delay(10);
    return;
  }
  client.setNoDelay(true);
  serve(client);
  client.stop();
}
