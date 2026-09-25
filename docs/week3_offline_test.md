# 第三周断网本地反馈测试记录

## 测试目标
验证物理断网状态下，板端本地按键反馈与"不误报远端已收到"机制。

## 测试环境
- 开发板：ESP32-S3-EYE
- 网络：Windows 移动热点 `hotspot`（网关 192.168.137.1）
- 板子静态 IP：192.168.137.100
- 服务器：Flask 运行在 192.168.137.1:5000

## 测试方法
1. 关闭 Windows 移动热点，物理切断板子与服务器的通信链路。
2. 按下板子上的 BOOT 键。
3. 观察串口日志与 LED 反馈。

## 关键日志（时间戳 100163~104733）

\`\`\`
I (100163) CAM_REMOTE: [BTN] Physical capture triggered!
I (100163) CAM_REMOTE: [LED] BLINK! BLUE (1/10)
...
I (100613) CAM_REMOTE: [LED] BLINK! BLUE (10/10)
I (101213) CAM_REMOTE: [CAP] Trigger=physical_button | Free heap=8588704
I (103213) CAM_REMOTE: [CAP] Photo: 1633 bytes
E (103223) CAM_REMOTE: [CAP] HTTP open err: ESP_ERR_HTTP_CONNECT
I (103223) CAM_REMOTE: [LED] BLINK! RED (1/10)
...
I (103683) CAM_REMOTE: [LED] BLINK! RED (10/10)
\`\`\`

## 验证结论

| 任务书要求 | 结果 |
| :--- | :--- |
| 断开外网后本地仍能确认按键触发 | ✅ `[BTN] Physical capture triggered!` |
| 本地物理反馈正常 | ✅ `[LED] BLINK! BLUE` ×10 |
| 无远端回执时不显示"对方已收到" | ✅ 全程未出现 `[LED] BLINK! GREEN` |
| 板子本地仍能拍照 | ✅ `[CAP] Photo: 1633 bytes` |
| 上传失败有正确反馈 | ✅ `HTTP open err: ESP_ERR_HTTP_CONNECT` + 红灯 |

## 已知限制
WiFi 连续 5 次重试失败后，板子会触发保护性 `esp_restart()`，属于主动设计，避免长时间卡在无网状态。

## 附件
见 `docs/evidence/` 目录下的截图与录像（可选）。