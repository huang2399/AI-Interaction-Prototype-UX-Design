#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_camera.h"
#include "esp_psram.h"
#include "esp_system.h"
#include <inttypes.h>

// ================= 配置区 =================
#define WIFI_SSID "431"
#define WIFI_PASS "88888888"
#define SERVER_URL "http://10.1.41.53:5000"
// =========================================

// ================= 摄像头引脚配置 (ESP32-S3-EYE OV2640) =================
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4
#define CAM_PIN_SIOC    5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13
// =========================================================================

static const char *TAG = "CAM_REMOTE";
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
static int s_retry_num = 0;
static const int MAX_RETRY = 5;

static void event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "WiFi disconnected, retry %d/%d", s_retry_num, MAX_RETRY);
        } else {
            ESP_LOGE(TAG, "WiFi max retries reached, will restart...");
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "Board IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void) {
    s_wifi_event_group = xEventGroupCreate();
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, NULL);
    wifi_config_t wifi_config = { .sta = { .ssid = WIFI_SSID, .password = WIFI_PASS } };
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    esp_wifi_start();
    ESP_LOGI(TAG, "Connecting WiFi: %s ...", WIFI_SSID);
}

// ---------- Camera Init ----------
static esp_err_t camera_init(void) {
    if (!esp_psram_is_initialized()) {
        ESP_LOGE(TAG, "PSRAM not initialized!");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "PSRAM ready, size: %d bytes", esp_psram_get_size());

    camera_config_t config = {
        .ledc_channel = LEDC_CHANNEL_0,
        .ledc_timer = LEDC_TIMER_0,
        .pin_d0 = CAM_PIN_D0,
        .pin_d1 = CAM_PIN_D1,
        .pin_d2 = CAM_PIN_D2,
        .pin_d3 = CAM_PIN_D3,
        .pin_d4 = CAM_PIN_D4,
        .pin_d5 = CAM_PIN_D5,
        .pin_d6 = CAM_PIN_D6,
        .pin_d7 = CAM_PIN_D7,
        .pin_xclk = CAM_PIN_XCLK,
        .pin_pclk = CAM_PIN_PCLK,
        .pin_vsync = CAM_PIN_VSYNC,
        .pin_href = CAM_PIN_HREF,
        .pin_sccb_sda = CAM_PIN_SIOD,
        .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_pwdn = CAM_PIN_PWDN,
        .pin_reset = CAM_PIN_RESET,
        .xclk_freq_hz = 10000000,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = FRAMESIZE_QQVGA,   // 160x120, ultra-light
        .jpeg_quality = 10,              // maximum compression
        .fb_count = 1,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
    };

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: 0x%x", err);
        return err;
    }
    ESP_LOGI(TAG, "Camera init OK");
    return ESP_OK;
}

// ---------- Poll Server ----------
static bool poll_server_for_task(void) {
    char url[128];
    snprintf(url, sizeof(url), "%s/api/device/poll", SERVER_URL);
    esp_http_client_config_t config = { .url = url, .method = HTTP_METHOD_GET, .timeout_ms = 3000 };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    char response[256] = {0};
    bool has_task = false;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        esp_http_client_fetch_headers(client);
        int read_len = esp_http_client_read(client, response, sizeof(response) - 1);
        if (read_len > 0) {
            response[read_len] = '\0';
            ESP_LOGI(TAG, "[POLL] Server response: %s", response);
            if (strstr(response, "\"has_task\":true") != NULL) has_task = true;
        } else {
            ESP_LOGW(TAG, "[POLL] Read failed or empty response, err=%d", read_len);
        }
    } else {
        ESP_LOGE(TAG, "[POLL] HTTP open failed: %s", esp_err_to_name(err));
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return has_task;
}

// ---------- Capture & Upload (zero-copy chunked send) ----------
// Static buffers to avoid stack overflow (~512 bytes saved from stack)
static char g_part_hdr[256];
static char g_part_ftr[128];
static char g_url[128];
static char g_ct[128];
static char g_resp[256];

static void capture_and_upload(void) {
    uint32_t heap_before = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[CAPTURE] Free heap before: %" PRIu32 " bytes", heap_before);

    ESP_LOGI(TAG, "[CAPTURE] Taking photo...");
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "[CAPTURE] esp_camera_fb_get returned NULL! (heap=%" PRIu32 ")", esp_get_free_heap_size());
        return;
    }
    ESP_LOGI(TAG, "[CAPTURE] Photo taken, size: %zu bytes, heap now: %" PRIu32, fb->len, esp_get_free_heap_size());

    // Pre-compute multipart header & footer into static buffers
    char boundary[] = "----ESP32Boundary";
    int hdr_len = snprintf(g_part_hdr, sizeof(g_part_hdr),
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"image\"; filename=\"capture.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n"
        "\r\n",
        boundary);
    int ftr_len = snprintf(g_part_ftr, sizeof(g_part_ftr),
        "\r\n--%s--\r\n", boundary);

    int total_len = hdr_len + (int)fb->len + ftr_len;
    ESP_LOGI(TAG, "[CAPTURE] HTTP total: %d (hdr=%d + img=%zu + ftr=%d)",
             total_len, hdr_len, fb->len, ftr_len);

    snprintf(g_url, sizeof(g_url), "%s/api/device/upload_photo", SERVER_URL);

    esp_http_client_config_t config = {
        .url = g_url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
        .buffer_size = 2048,   // smaller buffer
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);

    snprintf(g_ct, sizeof(g_ct), "multipart/form-data; boundary=%s", boundary);
    esp_http_client_set_header(client, "Content-Type", g_ct);

    // Open connection with Content-Length
    esp_err_t err = esp_http_client_open(client, total_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "[CAPTURE] HTTP open failed: %s", esp_err_to_name(err));
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        return;
    }

    // 1) Write multipart header
    int w = esp_http_client_write(client, g_part_hdr, hdr_len);
    if (w != hdr_len) {
        ESP_LOGE(TAG, "[CAPTURE] Write header failed: %d/%d", w, hdr_len);
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        return;
    }
    ESP_LOGI(TAG, "[CAPTURE] Header sent: %d bytes", w);

    // 2) Write image data directly from PSRAM frame buffer (zero-copy)
    w = esp_http_client_write(client, (const char *)fb->buf, fb->len);
    if (w != (int)fb->len) {
        ESP_LOGE(TAG, "[CAPTURE] Write image failed: %d/%zu", w, fb->len);
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        return;
    }
    ESP_LOGI(TAG, "[CAPTURE] Image sent: %d bytes", w);

    // *** RETURN FRAME BUFFER IMMEDIATELY after data is sent ***
    esp_camera_fb_return(fb);
    fb = NULL;
    ESP_LOGI(TAG, "[CAPTURE] Frame buffer released");

    // 3) Write multipart footer
    w = esp_http_client_write(client, g_part_ftr, ftr_len);
    if (w != ftr_len) {
        ESP_LOGE(TAG, "[CAPTURE] Write footer failed: %d/%d", w, ftr_len);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return;
    }
    ESP_LOGI(TAG, "[CAPTURE] Footer sent: %d bytes", w);

    // 4) Fetch response
    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);

    memset(g_resp, 0, sizeof(g_resp));
    int read_len = esp_http_client_read(client, g_resp, sizeof(g_resp) - 1);
    if (read_len > 0) g_resp[read_len] = '\0';

    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    uint32_t heap_after = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[CLEANUP] Free heap after: %" PRIu32 " bytes (delta: %+d)",
             heap_after, (int)(heap_after - heap_before));

    if (status_code == 200) {
        ESP_LOGI(TAG, "[DONE] Photo uploaded successfully! HTTP %d", status_code);
    } else {
        ESP_LOGE(TAG, "[FAIL] Upload HTTP %d, resp: %s",
                 status_code, read_len > 0 ? g_resp : "(empty)");
    }
}

// ---------- Main ----------
void app_main(void) {
    if (camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "Camera init fatal, restarting in 5s...");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }
    wifi_init();

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(30000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected. Starting main loop...");
    } else {
        ESP_LOGE(TAG, "WiFi connection failed, restarting...");
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }

    int consecutive_failures = 0;
    uint32_t loop_count = 0;

    while (1) {
        loop_count++;

        // Check WiFi alive every 10 loops
        if (loop_count % 10 == 0) {
            bits = xEventGroupGetBits(s_wifi_event_group);
            if (!(bits & WIFI_CONNECTED_BIT)) {
                ESP_LOGW(TAG, "[MAIN] WiFi lost! Waiting for reconnect...");
                bits = xEventGroupWaitBits(s_wifi_event_group,
                    WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
                if (bits & WIFI_FAIL_BIT) {
                    ESP_LOGE(TAG, "[MAIN] WiFi unrecoverable, restarting!");
                    esp_restart();
                }
                consecutive_failures = 0;
                continue;
            }
        }

        if (poll_server_for_task()) {
            consecutive_failures = 0;
            vTaskDelay(pdMS_TO_TICKS(200));
            ESP_LOGI(TAG, "[MAIN] Entering capture_and_upload...");
            capture_and_upload();
            ESP_LOGI(TAG, "[MAIN] capture_and_upload returned. Cooldown 1s.");
            vTaskDelay(pdMS_TO_TICKS(1000));
        } else {
            // Just a normal poll cycle
            if (loop_count % 60 == 0) {
                ESP_LOGI(TAG, "[MAIN] Loop #%d, heap: %" PRIu32, (int)loop_count, esp_get_free_heap_size());
            }
        }

        // If we have many consecutive failures (polls failing), something is wrong
        if (!(xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT)) {
            consecutive_failures++;
            if (consecutive_failures > 5) {
                ESP_LOGE(TAG, "[MAIN] Too many failures, rebooting!");
                esp_restart();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}