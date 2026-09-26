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
#include "driver/gpio.h"
#include "driver/rmt_tx.h"
#include "driver/i2c_master.h"  // NEW i2c master driver: QMA6100P rides the camera SCCB bus
                                // (legacy driver/i2c.h + qma6100p component CONFLICT with the
                                //  camera driver_ng -> check_i2c_driver_conflict abort at boot)
#include <inttypes.h>

// ================= BLE (NimBLE) =================
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

// ================= Config =================
#define USE_STATIC_IP_ENV 1  // 0=教室�?DHCP)  1=手机热点(静态IP)

#if USE_STATIC_IP_ENV == 0
// ---- Classroom network (DHCP) ----
#define WIFI_SSID "431"
#define WIFI_PASS "88888888"
#define SERVER_URL "http://10.1.41.53:5000"
#else
// ---- Windows hotspot (Static IP) ----
#define WIFI_SSID "hotspot"
#define WIFI_PASS "12345678"
#define SERVER_URL "http://192.168.137.1:5000"
#define STATIC_IP  "192.168.137.100"
#define STATIC_GW  "192.168.137.1"
#define STATIC_MASK "255.255.255.0"
#endif
// ===========================================

// ================= Camera pins (ESP32-S3-EYE OV2640) =================
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
// =====================================================================

// LCD ST7789 completely disabled to eliminate SPI DMA conflicts.
// Former LCD pins: CS=42 DC=40 RST=45 SCLK=47 MOSI=48 BL=46
// Repurpose: GPIO48 as status LED, GPIO46 pulled LOW (backlight off).
#define PIN_BL_OFF  46
#define PIN_LED     48

// ================= Button (BOOT = GPIO0) =================
#define BTN_PIN   0
// =========================================================

// ================= IMU (QMA6100P) config =================
#define IMU_I2C_PORT     I2C_NUM_0   // camera SCCB owns port 1; IMU keeps port 0 (week1 verified)
#define IMU_I2C_SDA      4           // same physical pins as SCCB (SDA=4, SCL=5)
#define IMU_I2C_SCL      5
#define IMU_I2C_FREQ_HZ  400000
#define IMU_GRAVITY      9.80665f    // library returns g -> convert to m/s^2
#define IMU_PERIOD_MS    1000        // 1 Hz upload. SAFETY KNOB: raise to 2000 if brownout/DMA issues
#define IMU_HTTP_TIMEOUT 1500        // HARD CAP per spec (<=1500ms); failures are dropped, never retried
// ==========================================================

static const char *TAG = "CAM_REMOTE";
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
static int s_retry_num = 0;
static const int MAX_RETRY = 5;

static volatile bool g_button_pressed = false;
static char g_trigger_source[20] = "web";
static volatile bool g_capture_busy = false;  // IMU gate: true during capture + cooldown
static i2c_master_dev_handle_t s_imu_dev = NULL;  // QMA6100P on camera SCCB bus (NULL = IMU disabled)

// ---------- WS2812 LED (GPIO48, RMT driver) ----------
static rmt_channel_handle_t led_tx_channel = NULL;
static rmt_encoder_handle_t led_bytes_encoder = NULL;
static rmt_encoder_handle_t led_copy_encoder = NULL;

static void ws2812_init(void) {
    // TX channel
    rmt_tx_channel_config_t tx_cfg = {
        .gpio_num = 48,
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,  // 10 MHz �?0.1 碌s/tick
        .mem_block_symbols = 64,
        .trans_queue_depth = 4,
        .flags.invert_out = false,
        .flags.with_dma = false,
    };
    ESP_ERROR_CHECK(rmt_new_tx_channel(&tx_cfg, &led_tx_channel));

    // Bytes encoder: maps each byte bit into WS2812 timing symbols
    rmt_bytes_encoder_config_t bytes_enc = {
        .bit0 = { .duration0 = 4, .level0 = 1, .duration1 = 9, .level1 = 0 },
        .bit1 = { .duration0 = 8, .level0 = 1, .duration1 = 5, .level1 = 0 },
        .flags.msb_first = true,
    };
    ESP_ERROR_CHECK(rmt_new_bytes_encoder(&bytes_enc, &led_bytes_encoder));

    // Copy encoder: used only for the reset pulse
    rmt_copy_encoder_config_t copy_enc = {};
    ESP_ERROR_CHECK(rmt_new_copy_encoder(&copy_enc, &led_copy_encoder));

    ESP_ERROR_CHECK(rmt_enable(led_tx_channel));
    ESP_LOGI(TAG, "[LED] WS2812 RMT init OK");
}

static void ws2812_set_color_raw(uint8_t r, uint8_t g, uint8_t b) {
    if (!led_tx_channel) return;
    // WS2812 expects GRB order
    uint8_t grb[3] = {g, r, b};

    rmt_transmit_config_t tx_conf = { .loop_count = 0 };
    rmt_transmit(led_tx_channel, led_bytes_encoder, grb, 3, &tx_conf);
    vTaskDelay(pdMS_TO_TICKS(1));  // non-blocking settle (avoid rmt_tx_wait_all_done timeout on S3-EYE)

    // Reset code: >50 碌s low pulse
    rmt_symbol_word_t reset_sym = { .val = 0 };
    reset_sym.duration0 = 600;
    reset_sym.level0 = 0;
    rmt_transmit(led_tx_channel, led_copy_encoder, &reset_sym, sizeof(reset_sym), &tx_conf);
    vTaskDelay(pdMS_TO_TICKS(1));
}

// Fallback: serial blink output in case WS2812 is physically unreachable
static void serial_blink(const char *label) {
    for (int i = 0; i < 10; i++) {
        ESP_LOGI(TAG, "[LED] BLINK! %s (%d/10)", label, i + 1);
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

static void led_off(void) { ws2812_set_color_raw(0, 0, 0); }
static void led_blue_on(void)   { ws2812_set_color_raw(0, 0, 64); serial_blink("BLUE"); }
static void led_green_on(void)  { ws2812_set_color_raw(0, 64, 0); serial_blink("GREEN"); }
static void led_red_on(void)    { ws2812_set_color_raw(64, 0, 0); serial_blink("RED"); }

static void led_pulse_blue(int ms)  { led_blue_on();  vTaskDelay(pdMS_TO_TICKS(ms)); led_off(); }
static void led_pulse_red(int ms)   { led_red_on();   vTaskDelay(pdMS_TO_TICKS(ms)); led_off(); }

static void led_flash_green(int count) {
    for (int i = 0; i < count; i++) {
        led_green_on(); vTaskDelay(pdMS_TO_TICKS(100));
        led_off();
        if (i < count - 1) vTaskDelay(pdMS_TO_TICKS(100));
    }
}

static void led_flash_blue(int count) {
    for (int i = 0; i < count; i++) {
        led_blue_on(); vTaskDelay(pdMS_TO_TICKS(150));
        led_off();
        if (i < count - 1) vTaskDelay(pdMS_TO_TICKS(150));
    }
}

static void bl_off(void) {
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_BL_OFF),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    gpio_set_level(PIN_BL_OFF, 0);
    ESP_LOGI(TAG, "[BL] Backlight GPIO%d OFF", PIN_BL_OFF);
}

// ---------- IMU (QMA6100P @0x12) on camera SCCB bus (SDA=4 SCL=5) ----------
// QMA6100P rides the CAMERA's SCCB bus (new i2c_master driver). esp32-camera's
// sccb-ng.c exports SCCB_Install_Device() which attaches our address to the same
// bus handle -> driver-internal locking, no second bus master on pins 4/5, and
// no legacy driver/i2c.h (which aborts at boot: driver_ng conflict).
// Register sequences ported 1:1 from the espressif qma6100p component
// (wake_up / config(ACCE_FS_2G) / get_raw_acce / get_acce).
extern int SCCB_Install_Device(uint8_t slv_addr);
extern i2c_master_dev_handle_t *get_handle_from_address(uint8_t slv_addr);

#define QMA_ADDR          0x12
#define QMA_REG_WHO_AM_I  0x00   // expects 0x90
#define QMA_REG_XOUT_L    0x01   // 6 bytes: XL XH YL YH ZL ZH (14-bit, <<2)
#define QMA_REG_ACCEL_CFG 0x0F   // bits[3:0] range; 0b0001 = +-2g -> 4096 LSB/g
#define QMA_REG_PWR_MGMT  0x11   // bit7 = EN
#define QMA_REG_NVM_LOAD  0x33   // bit3 = OTP load trigger
#define QMA_WHO_AM_I_VAL  0x90
#define QMA_SENS_2G       2461.0f

static esp_err_t qma_write_reg(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = { reg, val };
    return i2c_master_transmit(s_imu_dev, buf, 2, 100);
}

static esp_err_t qma_read_reg(uint8_t reg, uint8_t *out, size_t len) {
    return i2c_master_transmit_receive(s_imu_dev, &reg, 1, out, len, 100);
}

// MUST run AFTER camera_init(): the SCCB bus must already exist.
static esp_err_t imu_init(void) {
    if (SCCB_Install_Device(QMA_ADDR) != 0) {
        ESP_LOGE(TAG, "[IMU] SCCB_Install_Device(0x12) failed");
        return ESP_FAIL;
    }
    i2c_master_dev_handle_t *h = get_handle_from_address(QMA_ADDR);
    if (h == NULL || *h == NULL) {
        ESP_LOGE(TAG, "[IMU] no SCCB dev handle for 0x12");
        return ESP_FAIL;
    }
    s_imu_dev = *h;
    uint8_t id = 0;
    if (qma_read_reg(QMA_REG_WHO_AM_I, &id, 1) != ESP_OK || id != QMA_WHO_AM_I_VAL) {
        ESP_LOGE(TAG, "[IMU] WHO_AM_I=0x%02X (want 0x90) - sensor not responding", id);
        s_imu_dev = NULL;
        return ESP_ERR_NOT_FOUND;
    }
    // wake_up (component sequence): PWR EN, then OTP/NVM load
    if (qma_write_reg(QMA_REG_PWR_MGMT, 0x80) != ESP_OK) { s_imu_dev = NULL; return ESP_FAIL; }
    uint8_t nvm = 0;
    if (qma_read_reg(QMA_REG_NVM_LOAD, &nvm, 1) == ESP_OK) {
        qma_write_reg(QMA_REG_NVM_LOAD, (uint8_t)(nvm | 0x08));
    }
    vTaskDelay(pdMS_TO_TICKS(25));
    // range +-2g (read-modify-write, component sequence) + keep EN set
    uint8_t cfg = 0;
    if (qma_read_reg(QMA_REG_ACCEL_CFG, &cfg, 1) != ESP_OK) { s_imu_dev = NULL; return ESP_FAIL; }
    cfg = (uint8_t)((cfg & ~0x0F) | 0x01);
    if (qma_write_reg(QMA_REG_ACCEL_CFG, cfg) != ESP_OK) { s_imu_dev = NULL; return ESP_FAIL; }
    uint8_t pwr = 0;
    if (qma_read_reg(QMA_REG_PWR_MGMT, &pwr, 1) == ESP_OK) {
        qma_write_reg(QMA_REG_PWR_MGMT, (uint8_t)(pwr | 0x80));
    }
    vTaskDelay(pdMS_TO_TICKS(25));
    uint8_t rb = 0;
    qma_read_reg(QMA_REG_ACCEL_CFG, &rb, 1);
    ESP_LOGI(TAG, "[IMU] QMA6100P ready on SCCB bus (id=0x%02X, ACCEL_CFG=0x%02X)", id, rb);
    {
        uint8_t dd[6];
        if (qma_read_reg(QMA_REG_XOUT_L, dd, 6) == ESP_OK) {
            ESP_LOGI(TAG, "[IMU] first raw: x=%d y=%d z=%d", (int)((int16_t)((((uint16_t)dd[1]) << 8) | dd[0]) / 4), (int)((int16_t)((((uint16_t)dd[3]) << 8) | dd[2]) / 4), (int)((int16_t)((((uint16_t)dd[5]) << 8) | dd[4]) / 4));
        }
    }
    return ESP_OK;
}

// Dynamic sensitivity: read the ACTUAL range bits from reg 0x0F each sample
// (vendor component get_acce_sensitivity logic). Self-corrects if the chip's
// effective range differs from what we wrote (OTP defaults etc.).
static float qma_sensitivity(void) {
    uint8_t cfg = 0;
    if (qma_read_reg(QMA_REG_ACCEL_CFG, &cfg, 1) != ESP_OK) return QMA_SENS_2G;
    switch (cfg & 0x0F) {
        // CALIBRATED 2026-09-21: vendor component claims 4096 LSB/g @2g, but this
        // board's QMA6100P measures |gravity| = 2461 counts at cfg=0x01 (median of
        // 60 at-rest samples). Single-point gravity calibration: x0.6008. Raw
        // format verified 14-bit left-justified (LSB bits[1:0] always 0).
        case 0b0001: return 2461.0f;   // +-2g (calibrated)
        case 0b0010: return 1230.0f;   // +-4g
        case 0b0100: return 615.0f;    // +-8g
        case 0b1000: return 308.0f;    // +-16g
        case 0b1111: return 154.0f;    // +-32g
        default:     return 2461.0f;
    }
}

static esp_err_t imu_read(float *x, float *y, float *z) {
    if (s_imu_dev == NULL) return ESP_ERR_INVALID_STATE;
    uint8_t d[6];
    esp_err_t ret = qma_read_reg(QMA_REG_XOUT_L, d, 6);
    if (ret != ESP_OK) return ret;
    int16_t rx = (int16_t)((((uint16_t)d[1]) << 8) | d[0]) / 4;   // 14-bit (component formula)
    int16_t ry = (int16_t)((((uint16_t)d[3]) << 8) | d[2]) / 4;
    int16_t rz = (int16_t)((((uint16_t)d[5]) << 8) | d[4]) / 4;
    float sens = qma_sensitivity();
    *x = ((float)rx / sens) * IMU_GRAVITY;   // g -> m/s^2
    *y = ((float)ry / sens) * IMU_GRAVITY;
    *z = ((float)rz / sens) * IMU_GRAVITY;
    return ESP_OK;
}

// POST {"x":..,"y":..,"z":..} to /api/device/upload_imu.
// HARD RULES: timeout <= IMU_HTTP_TIMEOUT (1500ms); on ANY failure the sample is
// simply dropped (no retry loop, no red LED, never stalls the main loop/capture).
static void imu_post(float x, float y, float z) {
    static char payload[64];
    int len = snprintf(payload, sizeof(payload), "{\"x\":%.2f,\"y\":%.2f,\"z\":%.2f}", x, y, z);
    esp_http_client_config_t cfg = {
        .url = SERVER_URL "/api/device/upload_imu",
        .method = HTTP_METHOD_POST,
        .timeout_ms = IMU_HTTP_TIMEOUT,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) { ESP_LOGW(TAG, "[IMU] Skip: Network busy (client init)"); return; }
    int status = 0; bool ok = false;
    esp_http_client_set_header(c, "Content-Type", "application/json");
    if (esp_http_client_open(c, len) == ESP_OK
        && esp_http_client_write(c, payload, len) == len
        && esp_http_client_fetch_headers(c) >= 0) {
        status = esp_http_client_get_status_code(c);
        ok = (status == 200);
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    if (ok) ESP_LOGI(TAG, "[IMU] Sent: x=%.2f y=%.2f z=%.2f", x, y, z);
    else    ESP_LOGW(TAG, "[IMU] Skip: Network busy (status=%d)", status);
}
// Dedicated 1 Hz IMU task. Paused while g_capture_busy == true (capture +
// cooldown) so WiFi TX bursts never overlap camera DMA grabs
// (brownout / DMA-overflow protection on this WiFi+camera board).
static void imu_task(void *arg) {
    float x, y, z;
    uint32_t idle_cnt = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(IMU_PERIOD_MS));
        if (g_capture_busy) continue;   // capture or cooldown in progress -> pause uploads
        if (s_imu_dev == NULL) {        // sensor missing -> stay alive, no restart
            if (++idle_cnt % 60 == 1) ESP_LOGW(TAG, "[IMU] sensor absent, upload idle");
            continue;
        }
        if (!(xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT)) continue;
        if (imu_read(&x, &y, &z) != ESP_OK) { ESP_LOGW(TAG, "[IMU] Read fail"); continue; }
        imu_post(x, y, z);
    }
}


// ---------- WiFi event handler ----------
static void event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        // connect called manually after scan in wifi_init()
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t* disc = (wifi_event_sta_disconnected_t*) event_data;
        ESP_LOGW(TAG, "WiFi disconnected, reason=%d", disc->reason);
        if (s_retry_num < MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "WiFi retry %d/%d", s_retry_num, MAX_RETRY);
        } else {
            ESP_LOGE(TAG, "WiFi max retries, restarting...");
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
    nvs_flash_erase();  // clean NVS between env switches
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, NULL);
    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = {
                .capable = true,
                .required = false,
            },
        },
    };
    esp_wifi_set_mode(WIFI_MODE_STA);

    // Set China country code to enable channels 1-13
    wifi_country_t country = {
        .cc = "CN",
        .schan = 1,
        .nchan = 13,
        .policy = WIFI_COUNTRY_POLICY_MANUAL,
    };
    esp_wifi_set_country(&country);
    ESP_LOGI(TAG, "[WiFi] Country set: CN, channels 1-13");

    esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    esp_wifi_start();

    // Active scan to find all visible APs before connecting
    wifi_scan_config_t scan_cfg = { .scan_type = WIFI_SCAN_TYPE_ACTIVE };
    ESP_ERROR_CHECK(esp_wifi_scan_start(&scan_cfg, true));
    uint16_t ap_count = 0;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_num(&ap_count));
    ESP_LOGI(TAG, "[WiFi] Scan done, found %d AP(s)", ap_count);
    wifi_ap_record_t *ap_list = calloc(ap_count, sizeof(wifi_ap_record_t));
    if (ap_list) {
        ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&ap_count, ap_list));
        for (int i = 0; i < ap_count; i++) {
            ESP_LOGI(TAG, "[WiFi]   %-24s ch=%d rssi=%d auth=%d",
                     ap_list[i].ssid, ap_list[i].primary, ap_list[i].rssi, ap_list[i].authmode);
        }
        free(ap_list);
    }

    esp_wifi_connect();
    ESP_LOGI(TAG, "Connecting WiFi: %s ...", WIFI_SSID);

#if USE_STATIC_IP_ENV
    // Stop DHCP, assign static IP
    esp_netif_dhcpc_stop(sta_netif);
    esp_netif_ip_info_t ip_info = {0};
    ESP_ERROR_CHECK(esp_netif_str_to_ip4(STATIC_IP, &ip_info.ip));
    ESP_ERROR_CHECK(esp_netif_str_to_ip4(STATIC_GW, &ip_info.gw));
    ESP_ERROR_CHECK(esp_netif_str_to_ip4(STATIC_MASK, &ip_info.netmask));
    ESP_ERROR_CHECK(esp_netif_set_ip_info(sta_netif, &ip_info));
    esp_netif_dns_info_t dns;
    dns.ip.u_addr.ip4.addr = ip_info.gw.addr;
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    ESP_ERROR_CHECK(esp_netif_set_dns_info(sta_netif, ESP_NETIF_DNS_MAIN, &dns));
    ESP_LOGI(TAG, "[WiFi] Static IP set: " STATIC_IP);
#endif
}

// ---------- Camera Init (xclk=8MHz for DMA stability) ----------
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
        .xclk_freq_hz = 6000000,     // 6 MHz �?minimized to prevent DMA overflow
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = FRAMESIZE_QQVGA,
        .jpeg_quality = 10,


        .fb_count = 1,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
    };

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: 0x%x", err);
        return err;
    }
    ESP_LOGI(TAG, "Camera init OK (XCLK=8MHz)");
    return ESP_OK;
}

// ---------- Button ISR (IRAM-safe) ----------
static void IRAM_ATTR btn_isr(void* arg) {
    g_button_pressed = true;
}

static void btn_init(void) {
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << BTN_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    gpio_config(&btn_cfg);
    gpio_install_isr_service(ESP_INTR_FLAG_IRAM);
    gpio_isr_handler_add(BTN_PIN, btn_isr, NULL);
    ESP_LOGI(TAG, "[BTN] GPIO%d ISR installed", BTN_PIN);
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
            if (strstr(response, "\"has_task\":true") != NULL) has_task = true;
        }
    } else {
        ESP_LOGE(TAG, "[POLL] HTTP open err: %s", esp_err_to_name(err));
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return has_task;
}

// ---------- Capture & Upload (zero-copy) ----------
static char g_part_hdr[256];
static char g_part_ftr[128];
static char g_url[128];
static char g_ct[128];
static char g_resp[256];

static void capture_and_upload(void) {
    uint32_t heap_before = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[CAP] Trigger=%s | Free heap=%" PRIu32, g_trigger_source, heap_before);

    // *** 1000ms WiFi+DMA cooldown before camera DMA grab ***
    vTaskDelay(pdMS_TO_TICKS(2000));

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "[CAP] Camera returned NULL! heap=%" PRIu32 " �?skipping", esp_get_free_heap_size());
        led_pulse_red(1000);
        vTaskDelay(pdMS_TO_TICKS(200));
        return;
    }
    ESP_LOGI(TAG, "[CAP] Photo: %zu bytes, heap=%" PRIu32, fb->len, esp_get_free_heap_size());

    // Reject tiny/corrupt images (< 500 bytes �?likely empty)
    if (fb->len < 500) {
        ESP_LOGE(TAG, "[CAP] Photo too small (%zu bytes < 500), rejecting!", fb->len);
        esp_camera_fb_return(fb);
        led_pulse_red(1000);
        return;
    }

    // Multipart body
    char boundary[] = "ESP32_CAM_BOUNDARY";
    char trigger_field[128];   // was 80 �?overflow for "physical_button" (needs 92 bytes)
    int trigger_len = snprintf(trigger_field, sizeof(trigger_field),
        "--%s\r\nContent-Disposition: form-data; name=\"trigger\"\r\n\r\n%s\r\n",
        boundary, g_trigger_source);
    int hdr_len = snprintf(g_part_hdr, sizeof(g_part_hdr),
        "--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"capture.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n",
        boundary);
    int ftr_len = snprintf(g_part_ftr, sizeof(g_part_ftr), "\r\n--%s--\r\n", boundary);
    int total_len = trigger_len + hdr_len + (int)fb->len + ftr_len;

    snprintf(g_url, sizeof(g_url), "%s/api/device/upload_photo", SERVER_URL);
    esp_http_client_config_t config = {
        .url = g_url, .method = HTTP_METHOD_POST, .timeout_ms = 15000, .buffer_size = 2048,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    snprintf(g_ct, sizeof(g_ct), "multipart/form-data; boundary=%s", boundary);
    esp_http_client_set_header(client, "Content-Type", g_ct);

    esp_err_t err = esp_http_client_open(client, total_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "[CAP] HTTP open err: %s", esp_err_to_name(err));
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        led_pulse_red(1000);
        return;
    }

    int w = esp_http_client_write(client, trigger_field, trigger_len);
    if (w != trigger_len) {
        ESP_LOGE(TAG, "[CAP] Write trigger fail: %d/%d", w, trigger_len);
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        led_pulse_red(1000);
        return;
    }

    w = esp_http_client_write(client, g_part_hdr, hdr_len);
    if (w != hdr_len) {
        ESP_LOGE(TAG, "[CAP] Write hdr fail: %d/%d", w, hdr_len);
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        led_pulse_red(1000);
        return;
    }

    w = esp_http_client_write(client, (const char *)fb->buf, fb->len);
    if (w != (int)fb->len) {
        ESP_LOGE(TAG, "[CAP] Write img fail: %d/%zu", w, fb->len);
        esp_camera_fb_return(fb);
        esp_http_client_cleanup(client);
        led_pulse_red(1000);
        return;
    }

    // *** Release frame buffer immediately after data sent ***
    esp_camera_fb_return(fb);
    fb = NULL;

    w = esp_http_client_write(client, g_part_ftr, ftr_len);
    if (w != ftr_len) {
        ESP_LOGE(TAG, "[CAP] Write ftr fail: %d/%d", w, ftr_len);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        led_pulse_red(1000);
        return;
    }

    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);
    memset(g_resp, 0, sizeof(g_resp));
    int read_len = esp_http_client_read(client, g_resp, sizeof(g_resp) - 1);
    if (read_len > 0) g_resp[read_len] = '\0';
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    uint32_t heap_after = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[CAP] Free heap after: %" PRIu32 " (delta=%+d)", heap_after, (int)(heap_after - heap_before));

    if (status_code == 200) {
        ESP_LOGI(TAG, "[OK] Upload HTTP 200!");
        led_flash_green(3);          // 3 green flashes = success
    } else {
        ESP_LOGE(TAG, "[FAIL] HTTP %d, resp: %s", status_code, read_len > 0 ? g_resp : "(empty)");
        led_pulse_red(1000);          // 1s red = fail
    }
    strcpy(g_trigger_source, "web");
}

// ---------- BLE Service (NimBLE) ----------
#define BLE_DEVICE_NAME    "ESP32-S3-EYE"

// Service UUID: 4fafc201-1fb5-459e-8fcc-c5c9c331914b
// Bleprph convention: pass bytes in REVERSE order to BLE_UUID128_INIT
static const ble_uuid128_t g_ble_svc_uuid =
    BLE_UUID128_INIT(0x4b,0x91,0x31,0xc3, 0xc9,0xc5,0xcc,0x8f,
                     0x9e,0x45,0xb5,0x1f, 0x01,0xc2,0xaf,0x4f);

// Char UUID: beb5483e-36e1-4688-b7f5-ea07361b26a8
static const ble_uuid128_t g_ble_char_uuid =
    BLE_UUID128_INIT(0xa8, 0x26, 0x1b, 0x36, 0x07, 0xea, 0xf5, 0xb7,
                     0x88, 0x46, 0xe1, 0x36, 0x3e, 0x48, 0xb5, 0xbe);

static volatile bool     g_ble_capture_req = false;
static volatile int      g_ble_capture_ok  = 0;   // 0=pending, 1=ok, -1=fail
static SemaphoreHandle_t g_ble_done_sem    = NULL;
static SemaphoreHandle_t g_capture_mutex   = NULL;
static uint16_t          g_ble_char_handle = 0;
static uint16_t          g_ble_conn_handle = 0;

// BLE GAP event callback
static int ble_gap_cb(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            g_ble_conn_handle = event->connect.conn_handle;
            ESP_LOGI(TAG, "[BLE] Client connected, conn=%d", g_ble_conn_handle);
        } else { ESP_LOGW(TAG, "[BLE] Connect fail: %d", event->connect.status); }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "[BLE] Disconnected, reason=%d", event->disconnect.reason);
        g_ble_conn_handle = 0;
        // Re-start advertising so the device can be found again
        {
            struct ble_gap_adv_params adv = { .conn_mode = BLE_GAP_CONN_MODE_UND,
                                              .disc_mode = BLE_GAP_DISC_MODE_GEN };
            int rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &adv, ble_gap_cb, NULL);
            if (rc == 0)
                ESP_LOGI(TAG, "[BLE] Re-advertising after disconnect");
            else
                ESP_LOGE(TAG, "[BLE] Re-adv failed: %d", rc);
        }
        return 0;
    default: return 0;
    }
}

// GATT characteristic write callback �?triggers capture via semaphore
static int ble_char_write_cb(uint16_t conn_handle, uint16_t attr_handle,
                              struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR && ctxt->om) {
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len > 0 && len < 32) {
            char buf[32] = {0};
            os_mbuf_copydata(ctxt->om, 0, len, buf);
            buf[len] = '\0';
            ESP_LOGI(TAG, "[BLE] Write: '%s'", buf);

            if (strcmp(buf, "capture") == 0) {
                g_ble_capture_req = true;
                if (xSemaphoreTake(g_ble_done_sem, pdMS_TO_TICKS(15000))) {
                    char notify[48];
                    if (g_ble_capture_ok == 1)
                        snprintf(notify, sizeof(notify), "OK");
                    else
                        snprintf(notify, sizeof(notify), "FAIL");
                    ESP_LOGI(TAG, "[BLE] Notify: %s", notify);
                    struct os_mbuf *om = ble_hs_mbuf_from_flat(notify, strlen(notify));
                    if (om) ble_gattc_notify_custom(conn_handle, g_ble_char_handle, om);
                }
            } else {
                const char *err = "ERR";
                struct os_mbuf *om = ble_hs_mbuf_from_flat(err, strlen(err));
                if (om) ble_gattc_notify_custom(conn_handle, g_ble_char_handle, om);
            }
        }
    }
    return 0;
}

// ---------- GATT service definition (EXACT bleprph gatt_svr.c pattern) ----------

// CCCD UUID — 16-bit standard descriptor, static variable like bleprph uses
static const ble_uuid16_t g_ble_cccd_uuid = BLE_UUID16_INIT(BLE_GATT_DSC_CLT_CFG_UUID16);

// Service definition: nested compound literals, EXACT structure copied from bleprph
static const struct ble_gatt_svc_def g_ble_svcs[] = {
    {
        /*** Service ***/
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &g_ble_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[])
        { {
            /*** Characteristic: write + notify ***/
            .uuid = &g_ble_char_uuid.u,
            .access_cb = ble_char_write_cb,
            .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY,
            .val_handle = &g_ble_char_handle,
            .descriptors = (struct ble_gatt_dsc_def[])
            { {
                /*** CCCD descriptor for notifications ***/
                .uuid = &g_ble_cccd_uuid.u,
                .att_flags = BLE_ATT_F_READ | BLE_ATT_F_WRITE,
                .access_cb = NULL,
              }, {
                0, /* No more descriptors */
              }
            },
          }, {
            0, /* No more characteristics */
          }
        },
    }, {
        0, /* No more services */
    }
};

static void ble_on_reset(int reason) { ESP_LOGE(TAG, "[BLE] Reset: %d", reason); }

static void ble_on_sync(void) {
    ESP_LOGI(TAG, "[BLE] Synced, registering GATT services...");

    // Dump service definition for debug
    const ble_uuid_t *svc_uuid = g_ble_svcs[0].uuid;
    ESP_LOGI(TAG, "[BLE] Svc[0].type=%d, .uuid=%p, .uuid->type=%d",
             g_ble_svcs[0].type, (void*)svc_uuid, svc_uuid ? svc_uuid->type : -1);
    if (svc_uuid && svc_uuid->type == BLE_UUID_TYPE_128) {
        const uint8_t *v = ((const ble_uuid128_t *)svc_uuid)->value;
        ESP_LOGI(TAG, "[BLE] Svc UUID: %02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                 v[0],v[1],v[2],v[3], v[4],v[5], v[6],v[7], v[8],v[9], v[10],v[11],v[12],v[13],v[14],v[15]);
    }

    const struct ble_gatt_chr_def *chr = g_ble_svcs[0].characteristics;
    ESP_LOGI(TAG, "[BLE] Chr[0].uuid=%p", (void*)chr->uuid);
    if (chr->uuid) {
        ESP_LOGI(TAG, "[BLE] Chr[0].uuid->type=%d, .flags=0x%02lx, .descriptors=%p",
                 chr->uuid->type, (unsigned long)chr->flags, (void*)chr->descriptors);
        if (chr->uuid->type == BLE_UUID_TYPE_128) {
            const uint8_t *v = ((const ble_uuid128_t *)chr->uuid)->value;
            ESP_LOGI(TAG, "[BLE] Chr UUID: %02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                     v[0],v[1],v[2],v[3], v[4],v[5], v[6],v[7], v[8],v[9], v[10],v[11],v[12],v[13],v[14],v[15]);
        }
    }

    int rc;
    rc = ble_gatts_count_cfg(g_ble_svcs);
    if (rc) { ESP_LOGE(TAG, "[BLE] GATT count_cfg FAILED, rc=%d", rc); return; }
    ESP_LOGI(TAG, "[BLE] GATT count_cfg OK, total_entries=%d", rc);
    rc = ble_gatts_add_svcs(g_ble_svcs);
    if (rc) { ESP_LOGE(TAG, "[BLE] GATT add_svcs FAILED, rc=%d", rc); return; }
    ESP_LOGI(TAG, "[BLE] GATT service added, 1 primary svc + 1 char + CCCD");
    ble_svc_gap_device_name_set(BLE_DEVICE_NAME);

    struct ble_gap_adv_params adv = { .conn_mode = BLE_GAP_CONN_MODE_UND, .disc_mode = BLE_GAP_DISC_MODE_GEN };
    struct ble_hs_adv_fields fields = { .flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP };
    rc = ble_gap_adv_set_fields(&fields);
    if (rc) { ESP_LOGE(TAG, "[BLE] adv_fields err %d", rc); return; }
    rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &adv, ble_gap_cb, NULL);
    if (rc) ESP_LOGE(TAG, "[BLE] adv_start err %d", rc);
    else ESP_LOGI(TAG, "[BLE] Advertising as '%s'", BLE_DEVICE_NAME);
}

static void ble_host_task(void *arg) {
    ESP_LOGI(TAG, "[BLE] Host task started");
    nimble_port_run();
}

static void ble_init(void) {
    uint32_t heap_before = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[BLE] Init. Free heap: %" PRIu32, heap_before);

    g_ble_done_sem  = xSemaphoreCreateBinary();
    g_capture_mutex = xSemaphoreCreateMutex();
    if (!g_ble_done_sem || !g_capture_mutex) {
        ESP_LOGE(TAG, "[BLE] Semaphore fail!"); return;
    }

    if (nimble_port_init() != ESP_OK) { ESP_LOGE(TAG, "[BLE] port_init fail"); return; }

    // Register GAP + GATT built-in services (REQUIRED or GATT DB is empty!)
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ESP_LOGI(TAG, "[BLE] GAP+GATT base services registered");

    ble_hs_cfg.reset_cb = ble_on_reset;
    ble_hs_cfg.sync_cb  = ble_on_sync;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;

    nimble_port_freertos_init(ble_host_task);
    uint32_t heap_after = esp_get_free_heap_size();
    ESP_LOGI(TAG, "[BLE] Ready. Free heap: %" PRIu32 " (used %" PRIu32 ")",
             heap_after, heap_before - heap_after);
    if (heap_after < 30000)
        ESP_LOGW(TAG, "[BLE] Low heap! WiFi+BLE may be tight.");
}

// ---------- Main ----------
void app_main(void) {
    // Init status LED
    ws2812_init();
    bl_off();

    // Quick LED pulse to signal "alive"
    led_pulse_blue(200);

    if (camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "Camera fatal, restarting in 5s...");
        led_pulse_red(5000);
        esp_restart();
    }

    // ---- IMU init AFTER camera (SCCB on port 1 finished; IMU takes legacy port 0, pins 4/5) ----
    if (imu_init() == ESP_OK) {
        ESP_LOGI(TAG, "[IMU] QMA6100P ready, 1 Hz upload will start after WiFi");
    } else {
        ESP_LOGE(TAG, "[IMU] init FAILED - uploads disabled (board keeps running, camera unaffected)");
    }

    wifi_init();
    esp_wifi_set_max_tx_power(32);  // 8 dBm, reduced for stability

    // Wait for IP with retry logic --- classroom DHCP can be very slow (90+ sec).
    // Retry up to 3 disconnect/reconnect cycles before giving up.
    bool wifi_ok = false;
    int dhcp_retries = 0;
    const int MAX_DHCP_RETRIES = 3;
    const int DHCP_TIMEOUT_SEC = 90;

    while (!wifi_ok && dhcp_retries < MAX_DHCP_RETRIES) {
        if (dhcp_retries > 0) {
            ESP_LOGW(TAG, "[WiFi] DHCP timeout (attempt %d/%d), reconnecting...",
                     dhcp_retries, MAX_DHCP_RETRIES);
            s_retry_num = 0;              // reset disconnect counter
            xEventGroupClearBits(s_wifi_event_group, WIFI_FAIL_BIT);
            esp_wifi_disconnect();
            vTaskDelay(pdMS_TO_TICKS(5000));
            esp_wifi_connect();
            xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        }

        int elapsed = 0;
        while (elapsed < DHCP_TIMEOUT_SEC) {
            EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                pdFALSE, pdFALSE, pdMS_TO_TICKS(5000));
            elapsed += 5;
            if (bits & WIFI_CONNECTED_BIT) {
                wifi_ok = true;
                break;
            }
            if (bits & WIFI_FAIL_BIT) {
                ESP_LOGW(TAG, "[WiFi] AP disconnect at %d sec, will retry", elapsed);
                break;  // exit inner loop -> go to retry
            }
            ESP_LOGI(TAG, "[WiFi] Waiting for IP... (%d sec)", elapsed);
        }
        if (!wifi_ok) dhcp_retries++;
    }

    if (wifi_ok) {
        ESP_LOGI(TAG, "WiFi OK. Starting main loop...");
        led_flash_blue(2);  // 2 blue flashes = WiFi connected
        btn_init();
        btn_init();
        // ---- BLE: Bluetooth remote capture (phone) ----
        ble_init();
        // ---- IMU upload task: 1 Hz, 1500ms HTTP cap, auto-pauses during captures ----
        xTaskCreate(imu_task, "imu_task", 4096, NULL, 4, NULL);
        ESP_LOGI(TAG, "[IMU] Task started: period=%dms http_timeout=%dms", IMU_PERIOD_MS, IMU_HTTP_TIMEOUT);
    } else {
        ESP_LOGE(TAG, "WiFi failed after %d retries (%d sec total), restarting...",
                 MAX_DHCP_RETRIES, MAX_DHCP_RETRIES * DHCP_TIMEOUT_SEC);
        led_pulse_red(3000);
        esp_restart();
    }

    int consecutive_failures = 0;
    uint32_t loop_count = 0;
    uint32_t last_capture_ms = 0;   // anti-thrash cooldown
    EventBits_t bits;

    while (1) {

        // ======== CHECK PHYSICAL BUTTON FIRST ========
        if (g_button_pressed) {
            g_button_pressed = false;

            // 3-second anti-thrash: ignore rapid repeated presses
            uint32_t now_ms = pdTICKS_TO_MS(xTaskGetTickCount());
            if (now_ms - last_capture_ms < 3000) {
                ESP_LOGW(TAG, "[BTN] Ignored (cooldown, %" PRIu32 "ms since last)", now_ms - last_capture_ms);
                led_pulse_red(150);  // quick red = rejected
                continue;
            }
            last_capture_ms = now_ms;

            ESP_LOGI(TAG, "[BTN] Physical capture triggered!");
            strcpy(g_trigger_source, "physical_button");
            g_capture_busy = true;   // pause IMU uploads for the whole capture + cooldown
            led_pulse_blue(500);   // 500ms BLUE flash = "taking photo"
            vTaskDelay(pdMS_TO_TICKS(50));  // debounce
            capture_and_upload();
            ESP_LOGI(TAG, "[MAIN] Physical capture done. Cooldown 3s.");
            vTaskDelay(pdMS_TO_TICKS(3000));
            g_capture_busy = false;  // capture fully done + cooled down -> IMU may resume
            continue;
        }
        loop_count++;

        // Check WiFi
        if (loop_count % 10 == 0) {
            bits = xEventGroupGetBits(s_wifi_event_group);
            if (!(bits & WIFI_CONNECTED_BIT)) {
                ESP_LOGW(TAG, "[MAIN] WiFi lost! Waiting...");
                bits = xEventGroupWaitBits(s_wifi_event_group,
                    WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
                if (bits & WIFI_FAIL_BIT) {
                    ESP_LOGE(TAG, "[MAIN] WiFi unrecoverable, restart!");
                    esp_restart();
                }
                continue;
            }
        }

        // ---- BLE capture request (phone remote trigger) ----
        if (g_ble_capture_req) {
            g_ble_capture_req = false;
            ESP_LOGI(TAG, "[BLE] Handling capture request");
            if (xSemaphoreTake(g_capture_mutex, pdMS_TO_TICKS(1000))) {
                g_capture_busy = true;
                strcpy(g_trigger_source, "ble");
                capture_and_upload();
                g_ble_capture_ok = 1;
                g_capture_busy = false;
                xSemaphoreGive(g_capture_mutex);
                strcpy(g_trigger_source, "web");
            } else {
                g_ble_capture_ok = -1;
            }
            xSemaphoreGive(g_ble_done_sem);
        }

        if (poll_server_for_task()) {
            consecutive_failures = 0;
            last_capture_ms = pdTICKS_TO_MS(xTaskGetTickCount());  // also apply anti-thrash to web captures
            g_capture_busy = true;   // pause IMU uploads during web capture too
            capture_and_upload();
            vTaskDelay(pdMS_TO_TICKS(2000));
            g_capture_busy = false;  // resume IMU uploads
        } else {
            if (loop_count % 60 == 0) {
                ESP_LOGI(TAG, "[MAIN] Loop #%d, heap=%" PRIu32, (int)loop_count, esp_get_free_heap_size());
            }
        }

        if (!(xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT)) {
            consecutive_failures++;
            if (consecutive_failures > 10) {
                ESP_LOGE(TAG, "[MAIN] Too many failures, rebooting!");
                esp_restart();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
