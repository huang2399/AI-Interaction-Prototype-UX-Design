/*
 * imu_web_demo.c
 * 读取 QMA6100P 真实加速度 + WiFi 上传到本地 Flask 服务器
 * 硬件：ESP32-S3-EYE
 */

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
#define SERVER_URL "http://10.1.41.53:5000/imu_data"
// =========================================

// ================= IMU 配置 =================
#define I2C_PORT        I2C_NUM_0
#define I2C_SDA_GPIO    4      // ESP32-S3-EYE 的 IMU SDA
#define I2C_SCL_GPIO    5      // ESP32-S3-EYE 的 IMU SCL
#define I2C_FREQ_HZ     400000
#define GRAVITY         9.80665f
// =========================================

static const char *TAG = "IMU_WEB";
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static qma6100p_handle_t s_accel = NULL;

// ---------- WiFi 事件处理 ----------
static void event_handler(void* arg, esp_event_base_t event_base,
                          int32_t event_id, void* event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi 断开，正在重连...");
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "开发板自己的IP是: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    s_wifi_event_group = xEventGroupCreate();
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);

    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, NULL);

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    esp_wifi_start();
    ESP_LOGI(TAG, "正在连接 WiFi: %s ...", WIFI_SSID);
}

// ---------- IMU 初始化 ----------
static esp_err_t imu_init(void)
{
    // 1. 配置并安装 I2C 驱动（用 legacy 驱动，与 qma6100p 库匹配）
    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_SDA_GPIO,
        .scl_io_num = I2C_SCL_GPIO,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ_HZ,
    };
    esp_err_t ret = i2c_param_config(I2C_PORT, &i2c_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "i2c_param_config 失败: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = i2c_driver_install(I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "i2c_driver_install 失败: %s", esp_err_to_name(ret));
        return ret;
    }

    // 2. 尝试两个可能的 I2C 地址 (0x12 和 0x13)
    const uint8_t probe_addrs[] = { QMA6100P_I2C_ADDRESS, QMA6100P_I2C_ADDRESS_1 };
    for (size_t i = 0; i < sizeof(probe_addrs); i++) {
        qma6100p_handle_t probe = qma6100p_create(I2C_PORT, probe_addrs[i]);
        if (probe == NULL) continue;

        uint8_t device_id = 0;
        ret = qma6100p_get_deviceid(probe, &device_id);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "发现 QMA6100P! 地址=0x%02X, ID=0x%02X", probe_addrs[i], device_id);
            ret = qma6100p_wake_up(probe);
            if (ret == ESP_OK) ret = qma6100p_config(probe, ACCE_FS_2G);
            if (ret == ESP_OK) {
                s_accel = probe;
                return ESP_OK;
            }
        }
        qma6100p_delete(probe);
    }

    ESP_LOGE(TAG, "未找到 QMA6100P，请检查 SDA=%d SCL=%d 是否正确", I2C_SDA_GPIO, I2C_SCL_GPIO);
    return ESP_ERR_NOT_FOUND;
}

// ---------- 读取 IMU ----------
static esp_err_t imu_read(float *x, float *y, float *z)
{
    if (s_accel == NULL) return ESP_ERR_INVALID_STATE;
    qma6100p_acce_value_t acce = {0};
    esp_err_t ret = qma6100p_get_acce(s_accel, &acce);
    if (ret != ESP_OK) return ret;

    // 库返回单位是 g，转成 m/s²
    *x = acce.acce_x * GRAVITY;
    *y = acce.acce_y * GRAVITY;
    *z = acce.acce_z * GRAVITY;
    return ESP_OK;
}

// ---------- 上传到服务器 ----------
static void post_imu_data(float x, float y, float z)
{
    char post_data[128];
    snprintf(post_data, sizeof(post_data),
             "{\"x\":%.2f,\"y\":%.2f,\"z\":%.2f}", x, y, z);

    esp_http_client_config_t config = {
        .url = SERVER_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 3000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_post_field(client, post_data, strlen(post_data));
    esp_http_client_set_header(client, "Content-Type", "application/json");

    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "数据已发送 (%d): %s", status, post_data);
    } else {
        ESP_LOGE(TAG, "发送失败: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
}

// ---------- 主程序 ----------
void app_main(void)
{
    // 1. 初始化 IMU
    if (imu_init() != ESP_OK) {
        ESP_LOGE(TAG, "IMU 初始化失败！5 秒后重启...");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }

    // 2. 连接 WiFi
    wifi_init();
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
            WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(20000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi 连接成功，准备开始上传！");
    } else {
        ESP_LOGE(TAG, "WiFi 连接超时！");
    }

    // 3. 循环读取并上传
    float x, y, z;
    while (1) {
        if (imu_read(&x, &y, &z) == ESP_OK) {
            ESP_LOGI(TAG, "真实数据: x=%.2f, y=%.2f, z=%.2f", x, y, z);
            post_imu_data(x, y, z);
        } else {
            ESP_LOGE(TAG, "IMU 读取失败");
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}