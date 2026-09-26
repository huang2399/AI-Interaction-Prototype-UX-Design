# Week 4: AI Natural-Language Control & BLE Phone Capture

## Overview

Week 4 introduces two major capabilities:
1. **AI Chat Assistant** — Natural language control of the ESP32-S3-EYE via `/api/chat` with LLM Function Calling
2. **BLE Remote Capture** — Phone can send "capture" command via Bluetooth BLE to trigger photo

## 1. AI LLM Integration

### Model Selection
- **Provider**: DeepSeek (OpenAI-compatible API)
- **Model**: `deepseek-chat`
- **Endpoint**: `https://api.deepseek.com/v1/chat/completions`
- **Configurable via** env vars: `DEEPSEEK_API_KEY`/`LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`

### Function Calling Design

Two tools are defined for the LLM:

| Tool | Description | Internal API |
|------|-------------|-------------|
| `get_device_status` | 查询设备在线状态、IP、RSSI、任务状态 | `/api/status` (in-process) |
| `take_photo` | 发送拍照指令并等待结果 | `create_capture_task("ai_chat")` + polling |

### Chat Flow
```
User: "帮我拍张照"
  -> POST /api/chat {message: "..."}
  -> Server calls DeepSeek with tools
  -> LLM decides: tool=take_photo
  -> Server executes take_photo:
      1. Check device online? no -> return "设备未响应"
      2. Create capture task
      3. Poll every 1s until COMPLETED/TIMEOUT/OFFLINE (max 35s)
      4. Return result to LLM
  -> LLM formats natural reply
  -> Response: {reply: "已采集成功。", tool_used: "take_photo"}
```

### Anti-False-Positive Logic
1. **Pre-check**: Verify device online (last seen < 60s) before creating task
2. **Polling**: Poll task_state at 1s intervals after task creation
3. **Timeout**: If 35s elapsed without COMPLETED, return "任务超时"
4. **Device offline during poll**: Return "设备未响应"
5. **System prompt enforces**: "只有take_photo返回success=true才能说已采集成功"

### Ambiguity Handling
Unsupported sensors (temperature, humidity, pressure, etc.) are intercepted **before** the LLM call:
```python
UNSUPPORTED_KEYWORDS = {"temperature", "humidity", "pressure", "light",
                        "sound", "distance", "proximity", "gas", "co2", "voc"}
```
Response: "本系统不支持该功能。当前仅支持：摄像头拍照、IMU加速度计、WiFi状态查询。"

## 2. BLE Service Design

### Stack: ESP-IDF NimBLE (lightweight, lower memory than Bluedroid)

### UUIDs
- **Service**: `4fafc201-1fb5-459e-8fcc-c5c9c331914b` (128-bit custom)
- **Characteristic**: `beb5483e-36e1-4688-b7f5-ea07361b26a8` (128-bit custom)

### Characteristic Properties
- **Write**: Phone sends `"capture"` to trigger camera
- **Notify**: Board sends `"OK"` or `"FAIL"` as result

### Protocol
```
Phone                              ESP32-S3-EYE
  |--- BLE Connect ---------------->|
  |--- Write Char: "capture" ------>| (sets g_ble_capture_req, blocks on sem)
  |                                 | (main loop handles capture_and_upload)
  |<-- Notify: "OK" ----------------| (or "FAIL" / "ERR")
```

### Implementation Architecture
- BLE write callback (NimBLE task) sets `g_ble_capture_req=true`, waits on semaphore (15s)
- Main loop checks `g_ble_capture_req` before server poll, takes mutex, calls `capture_and_upload()`
- `ble_init()` called after WiFi connects, before IMU task

### Memory
- BLE after WiFi; logs heap delta; warns if free < 30KB
- Kconfig: `MAX_CONNECTIONS=1`, `MAX_BONDS=1`, `MAX_CCCDS=2`

### Ble Low Memory Warning
If heap drops below 30KB after BLE init, warning is logged. This is informational; BLE and WiFi can coexist on ESP32-S3-EYE with 2MB PSRAM but stack usage should be monitored.

## 3. Frontend Chat Window

Full-width card at bottom of dashboard, below photo gallery.
Chat bubbles, Enter-key support, typing indicator, tool-used pill, auto-refresh gallery after capture.
Dark theme consistent with existing dashboard (`--bg: #0f172a`, cyan/violet accents).

## 4. Files Modified

| File | Change |
|------|--------|
| `server/app.py` | +~180 lines: `/api/chat`, `_call_llm()`, `_execute_tool()`, tool defs |
| `server/templates/index.html` | +80 lines: Chat CSS + HTML widget + JS |
| `main/imu_web_demo.c` | +~140 lines: NimBLE service, callbacks, init, main-loop BLE check |
| `main/CMakeLists.txt` | Added `bt` to REQUIRES |
| `sdkconfig` | Enabled BT, NimBLE, SINGLE_APP_LARGE partition |
| `sdkconfig.defaults` | New: BLE defaults for clean builds |
| `docs/week4_ai_ble.md` | This document |

## 5. Build: PASSED

```
$ idf.py build  ->  Project build complete.
Target: esp32s3, IDF v5.4.4, GCC 14.2.0
Binary: build/imu_web_demo.bin (~1.22MB)
Partition: SINGLE_APP_LARGE
```

## 6. Test Plan (Pending Board Online)

| # | Test | Action | Expected |
|---|------|--------|----------|
| 1 | AI Status | "现在状态怎么样" | Returns device online/offline |
| 2 | AI Capture (online) | "帮我拍张照" | "已采集成功", photo in gallery |
| 3 | AI Capture (offline) | Unplug board, capture | "设备未响应" (NOT "已采集成功") |
| 4 | AI Ambiguity | "温度多少" | "本系统不支持该功能" |
| 5 | BLE Scan | nRF Connect | "ESP32-S3-EYE" visible |
| 6 | BLE Connect | Connect | Service/Char UUIDs discovered |
| 7 | BLE Capture | Write "capture" | Notify: "OK", photo uploaded |
| 8 | BLE Unknown Cmd | Write "status" | Notify: "ERR" |
| 9 | BLE+WiFi Concurrent | BLE capture during upload | Both succeed, no crash |
| 10 | AI Capture via BLE trigger | Phone BLE + AI chat refresh | Gallery updates

## BLE 蓝牙调试记录

### 已完成
- AI 自然语言控制：✅ 100% 跑通（智谱 GLM-4.7-Flash + Function Calling）
- 防误报机制：✅ 验证通过（断网时 AI 如实回复"设备未响应"）
- 歧义处理：✅ 验证通过（温度/湿度等不支持传感器被本地拦截）

### 未完成：BLE 蓝牙
- 状态：❌ 未跑通
- 现象：BLE 广播正常（手机能扫到 MAC 94:A9:90:1C:70:52），但 GATT Service 注册失败（count_cfg rc=3），手机连接后 Services 页面空白。
- 根因分析：NimBLE 的 BLE_UUID128_INIT 字节序与编译期常量初始化存在兼容性问题，多次调试未解决。
- 后续计划：将参考 ESP-IDF 官方 bleprph 示例重构 BLE 初始化代码，或改用 ESP-IDF 的 Bluedroid 栈。