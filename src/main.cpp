// Picture-transfer check for the Seeed Studio XIAO ESP32S3 Sense.
//
// One JPEG moves three times. Each time is measured on its own:
//   sensor — the OV3660 builds the JPEG and the ESP32-S3 stores it in the
//            8 MB picture memory inside the ESP32-S3 package
//   copy   — the ESP32-S3 processor copies that JPEG into internal memory,
//            which is the only memory the Wi-Fi block can transmit from
//   wifi   — the Wi-Fi block sends that internal copy out the antenna
//
// The Python program is the only stream client. It checks that every JPEG
// is 1024x768 and prints the report.

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy include/secrets.h.example to include/secrets.h and set the Wi-Fi name and password."
#endif

#include <esp_camera.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>

#include <WiFi.h>

#include <stdio.h>
#include <string.h>

namespace {

constexpr int kClockMhz = 21;
constexpr int kQuality = 30;
constexpr int kBrightness = 1;
constexpr int kSaturation = -2;
constexpr int kMirror = 0;
constexpr int kFlip = 1;
constexpr int kNight = 0;
constexpr uint16_t kPort = 80;

// Seeed Studio XIAO ESP32S3 Sense camera connector.
constexpr int kPwdn = -1;
constexpr int kReset = -1;
constexpr int kXclk = 10;
constexpr int kSiod = 40;
constexpr int kSioc = 39;
constexpr int kY9 = 48;
constexpr int kY8 = 11;
constexpr int kY7 = 12;
constexpr int kY6 = 14;
constexpr int kY5 = 16;
constexpr int kY4 = 18;
constexpr int kY3 = 17;
constexpr int kY2 = 15;
constexpr int kVsync = 38;
constexpr int kHref = 47;
constexpr int kPclk = 13;

WiFiServer server(kPort);
bool started = false;

uint32_t sinceUs(int64_t started) {
  int64_t delta = esp_timer_get_time() - started;
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
    size_t chunk = length - sent;
    int wrote = client.write(data + sent, chunk);
    if (wrote <= 0) {
      return false;
    }
    sent += static_cast<size_t>(wrote);
  }
  return true;
}

bool startCamera() {
  if (!psramFound()) {
    Serial.println("camera failed");
    return false;
  }

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = kY2;
  config.pin_d1 = kY3;
  config.pin_d2 = kY4;
  config.pin_d3 = kY5;
  config.pin_d4 = kY6;
  config.pin_d5 = kY7;
  config.pin_d6 = kY8;
  config.pin_d7 = kY9;
  config.pin_xclk = kXclk;
  config.pin_pclk = kPclk;
  config.pin_vsync = kVsync;
  config.pin_href = kHref;
  config.pin_sccb_sda = kSiod;
  config.pin_sccb_scl = kSioc;
  config.pin_pwdn = kPwdn;
  config.pin_reset = kReset;
  config.xclk_freq_hz = kClockMhz * 1000000;
  config.frame_size = FRAMESIZE_XGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = kQuality;
  config.fb_count = 2;

  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("camera failed");
    return false;
  }

  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("camera failed");
    return false;
  }
  if (sensor->set_framesize != nullptr) {
    sensor->set_framesize(sensor, FRAMESIZE_XGA);
  }
  if (sensor->set_quality != nullptr) {
    sensor->set_quality(sensor, kQuality);
  }
  if (sensor->set_brightness != nullptr) {
    sensor->set_brightness(sensor, kBrightness);
  }
  if (sensor->set_saturation != nullptr) {
    sensor->set_saturation(sensor, kSaturation);
  }
  if (sensor->set_hmirror != nullptr) {
    sensor->set_hmirror(sensor, kMirror);
  }
  if (sensor->set_vflip != nullptr) {
    sensor->set_vflip(sensor, kFlip);
  }
  if (sensor->set_aec2 != nullptr) {
    sensor->set_aec2(sensor, kNight);
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

bool readRequest(WiFiClient &client, char *request, size_t length) {
  size_t used = 0;
  request[0] = '\0';
  uint32_t start = millis();
  while (client.connected() && millis() - start < 2000) {
    while (client.available() > 0 && used + 1 < length) {
      request[used++] = static_cast<char>(client.read());
      request[used] = '\0';
      if (strstr(request, "\r\n\r\n") != nullptr) {
        return true;
      }
    }
    if (used + 1 >= length) {
      return false;
    }
    delay(1);
  }
  return false;
}

void sendText(WiFiClient &client, const char *body) {
  char header[80];
  int length = static_cast<int>(strlen(body));
  snprintf(header, sizeof(header), "--frame\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n", length);
  client.print(header);
  client.print(body);
}

void sendBoard(WiFiClient &client) {
  char body[320];
  snprintf(body, sizeof(body),
           "board mac=%s\n"
           "board rssi=%d\n"
           "board clock_mhz=%d\n"
           "board frame=XGA\n"
           "board quality=%d\n"
           "board brightness=%d\n"
           "board saturation=%d\n"
           "board mirror=%d\n"
           "board flip=%d\n"
           "board night=%d\n",
           WiFi.macAddress().c_str(), WiFi.RSSI(), kClockMhz, kQuality, kBrightness, kSaturation, kMirror, kFlip,
           kNight);
  sendText(client, body);
}

void sendMetric(WiFiClient &client, uint32_t sensorUs, int32_t copyUs, uint32_t wifiUs, int rssi) {
  char line[96];
  snprintf(line, sizeof(line), "metric sensor_us=%lu copy_us=%ld wifi_us=%lu rssi=%d\n",
           static_cast<unsigned long>(sensorUs), static_cast<long>(copyUs), static_cast<unsigned long>(wifiUs), rssi);
  client.print(line);
}

bool serveStream(WiFiClient &client) {
  client.print("HTTP/1.1 200 OK\r\n");
  client.print("Content-Type: multipart/x-mixed-replace; boundary=frame\r\n");
  client.print("\r\n");
  sendBoard(client);

  while (client.connected()) {
    int64_t sensorStarted = esp_timer_get_time();
    camera_fb_t *frame = esp_camera_fb_get();
    uint32_t sensorUs = sinceUs(sensorStarted);
    if (frame == nullptr || frame->format != PIXFORMAT_JPEG || frame->len == 0) {
      if (frame != nullptr) {
        esp_camera_fb_return(frame);
      }
      delay(20);
      continue;
    }

    uint8_t *internal = static_cast<uint8_t *>(heap_caps_malloc(frame->len, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (internal == nullptr) {
      esp_camera_fb_return(frame);
      sendMetric(client, sensorUs, -1, 0, WiFi.RSSI());
      continue;
    }

    int64_t copyStarted = esp_timer_get_time();
    memcpy(internal, frame->buf, frame->len);
    uint32_t copyUs = sinceUs(copyStarted);
    size_t length = frame->len;
    esp_camera_fb_return(frame);

    client.print("--frame\r\nContent-Type: image/jpeg\r\n\r\n");
    int64_t wifiStarted = esp_timer_get_time();
    bool wrote = writeAll(client, internal, length);
    uint32_t wifiUs = sinceUs(wifiStarted);
    heap_caps_free(internal);
    if (!wrote) {
      return false;
    }
    sendMetric(client, sensorUs, static_cast<int32_t>(copyUs), wifiUs, WiFi.RSSI());
  }
  return true;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  // Opening the port resets the chip. Wait past that, then print, so a reader
  // that flushes the old bytes still sees this boot.
  delay(300);
  Serial.println("validator");
  if (!startCamera() || !joinWifi()) {
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
  char request[800];
  if (!readRequest(client, request, sizeof(request))) {
    client.stop();
    return;
  }
  if (strncmp(request, "GET /stream ", 12) == 0) {
    serveStream(client);
  } else {
    client.print("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
  }
  client.stop();
}
