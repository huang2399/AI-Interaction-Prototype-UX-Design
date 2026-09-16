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
#include "driver/i2c.h"
#include "qma6100p.h"

// ================= 配置区 =================
#define WIFI_SSID "431"
#define WIFI_PASS "88888888"
#define SERVER_URL "http://10.1.41.53:5000" // 只写基础URL，后面拼接路径
// =========================================

// ================= IMU 配置 =================
#define I2C_PORT        I2C_NUM_0
#define I2C_SDA_GPIO    4
#define I2C_SCL_GPIO    5
#define I2C_FREQ_HZ     400000
#define GRAVITY         9.80665f
// =========================================

static const char *TAG = "IMU_REMOTE";
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static qma6100p_handle_t s_accel = NULL;

// ---------- WiFi 事件处理 ----------
static void event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "开发板IP: " IPSTR, IP2STR(&event->ip_info.ip));
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
    ESP_LOGI(TAG, "正在连接 WiFi: %s ...", WIFI_SSID);
}

// ---------- IMU 初始化 ----------
static esp_err_t imu_init(void) {
    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER, .sda_io_num = I2C_SDA_GPIO, .scl_io_num = I2C_SCL_GPIO,
        .sda_pullup_en = GPIO_PULLUP_ENABLE, .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ_HZ,
    };
    i2c_param_config(I2C_PORT, &i2c_conf);
    i2c_driver_install(I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);

    const uint8_t probe_addrs[] = { QMA6100P_I2C_ADDRESS, QMA6100P_I2C_ADDRESS_1 };
    for (size_t i = 0; i < sizeof(probe_addrs); i++) {
        qma6100p_handle_t probe = qma6100p_create(I2C_PORT, probe_addrs[i]);
        if (!probe) continue;
        uint8_t device_id = 0;
        if (qma6100p_get_deviceid(probe, &device_id) == ESP_OK) {
            qma6100p_wake_up(probe);
            qma6100p_config(probe, ACCE_FS_2G);
            s_accel = probe;
            ESP_LOGI(TAG, "IMU 初始化成功 (ID: 0x%02X)", device_id);
            return ESP_OK;
        }
        qma6100p_delete(probe);
    }
    ESP_LOGE(TAG, "未找到 IMU");
    return ESP_ERR_NOT_FOUND;
}

// ---------- 读取 IMU ----------
static esp_err_t imu_read(float *x, float *y, float *z) {
    if (s_accel == NULL) return ESP_ERR_INVALID_STATE;
    qma6100p_acce_value_t acce = {0};
    esp_err_t ret = qma6100p_get_acce(s_accel, &acce);
    if (ret != ESP_OK) return ret;
    *x = acce.acce_x * GRAVITY;
    *y = acce.acce_y * GRAVITY;
    *z = acce.acce_z * GRAVITY;
    return ESP_OK;
}

// ---------- 轮询服务器是否有任务 ----------
static bool poll_server_for_task(void) {
    char url[128];
    snprintf(url, sizeof(url), "%s/api/device/poll", SERVER_URL);
    
    esp_http_client_config_t config = { .url = url, .method = HTTP_METHOD_GET, .timeout_ms = 3000 };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    
    char response[256] = {0};
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        esp_http_client_fetch_headers(client);
        int read_len = esp_http_client_read(client, response, sizeof(response) - 1);
        if (read_len > 0) {
            response[read_len] = '\0';
            if (strstr(response, "\"has_task\":true") != NULL) {
                ESP_LOGI(TAG, "【设备接收】收到服务器下发的采集任务！");
                esp_http_client_cleanup(client);
                return true;
            }
        }
    }
    esp_http_client_cleanup(client);
    return false;
}

// ---------- 上报数据 ----------
static void report_data(float x, float y, float z) {
    char url[128];
    snprintf(url, sizeof(url), "%s/api/device/report", SERVER_URL);
    char post_data[128];
    snprintf(post_data, sizeof(post_data), "{\"x\":%.2f,\"y\":%.2f,\"z\":%.2f}", x, y, z);
    
    esp_http_client_config_t config = { .url = url, .method = HTTP_METHOD_POST, .timeout_ms = 3000 };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_post_field(client, post_data, strlen(post_data));
    esp_http_client_set_header(client, "Content-Type", "application/json");
    
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "【任务完成】数据已上报: %s", post_data);
    } else {
        ESP_LOGE(TAG, "上报失败: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
}

// ---------- 主程序 ----------
void app_main(void) {
    if (imu_init() != ESP_OK) { vTaskDelay(pdMS_TO_TICKS(5000)); esp_restart(); }
    wifi_init();
    
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(20000));
    if (bits & WIFI_CONNECTED_BIT) ESP_LOGI(TAG, "WiFi 连接成功，开始轮询等待指令...");
    else ESP_LOGE(TAG, "WiFi 连接超时！");

    float x, y, z;
    while (1) {
        // 1. 轮询服务器
        if (poll_server_for_task()) {
            // 2. 如果有任务，读取IMU
            vTaskDelay(pdMS_TO_TICKS(200)); // 稍微等待，确保指令接收完成
            if (imu_read(&x, &y, &z) == ESP_OK) {
                ESP_LOGI(TAG, "真实数据: x=%.2f, y=%.2f, z=%.2f", x, y, z);
                // 3. 上报数据
                report_data(x, y, z);
            } else {
                ESP_LOGE(TAG, "IMU 读取失败");
            }
        }
        // 每 1 秒轮询一次（可根据需要调整，太快会造成服务器压力）
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}