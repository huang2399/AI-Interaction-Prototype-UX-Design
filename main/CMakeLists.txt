idf_component_register(SRCS "test_create.c" "imu_web_demo.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_psram esp32-camera esp_wifi esp_event esp_netif esp_http_client nvs_flash esp_lcd driver esp_driver_rmt bt)