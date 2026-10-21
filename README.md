# 人工智能交互原型设计 — ESP32-S3-EYE 智能体项目

## 项目简介

基于 ESP32-S3-EYE 开发板，构建“传感器采集 + Web 大屏展示 + 远程控制 + AI 自然语言交互 + BLE 蓝牙 + 语音对话 + 视觉推理”的完整 AIoT 多模态交互原型系统。

## 系统架构

ESP32-S3-EYE (QMA6100P IMU / OV2640 摄像头)
│ WiFi (HTTP)
▼
本地 Flask 服务器 (充当 VPS)
│
├── 智谱 GLM (AI 对话 + Function Calling)
├── 阿里云百炼 (语音识别 ASR + 语音合成 TTS)
├── 轻量级视觉引擎 (场景分类与遮挡判定)
▼
Web 前端 (深色控制台 + IMU波形图 + 照片画廊 + AI 聊天 + 语音交互)
▲
│ BLE
│
手机 BLE 调试助手

## 每周任务与对应里程碑标签

| 周次 | 任务 | Git 标签 |
| :--- | :--- | :--- |
| 第1周 | IMU 采集 + Web 展示（基础版与增强版） | `week1-complete` / `week1-enhanced` |
| 第2周 | 远程拍照 + 状态回执 + 照片画廊 | `week2-complete` |
| 第3周 | 按键触发 + 本地反馈 + 断网测试 | `week3-complete` |
| 第4周 | AI 自然语言控制 (GLM) + BLE 蓝牙 | `week4-core-complete` / `week4-ble-complete` |
| 第5周 | 语音输入 (ASR) + 意图处理 + 语音播报 (TTS) | `week5-complete` |
| 第6周 | 语音任务澄清、中途停止、请求隔离 | `week6-complete` |
| 第7周 | 按需图像采集、防旧图机制、遮挡灰度警告 | `week7-complete` |
| 第8周 | 视觉推理反馈、置信度打分、低分拒识与人工纠错 | `week8-complete` |

## 目录结构

```text
imu_web_demo/
├── main/                      # 板端代码
│   ├── imu_web_demo.c         # 主程序（IMU + WiFi + BLE + HTTP）
│   └── CMakeLists.txt
├── server/                    # 服务端代码
│   ├── app.py                 # Flask 服务器（接收 + 状态机 + AI + 语音 + 视觉）
│   ├── templates/index.html   # 前端页面
│   └── static/                # 静态资源（图标、合成音频等）
├── docs/                      # 项目文档与测试证据
│   ├── week4_ai_ble.md
│   └── README.md
├── managed_components/        # ESP-IDF 第三方组件
├── CMakeLists.txt
├── sdkconfig
└── README.md


---

### 📄 文件 2：`docs/README.md`

请把 `D:\esp_project\imu_web_demo\imu_web_demo\docs\README.md` 的内容替换为：

```markdown
# 项目文档索引

## 各周文档与验证证据

| 周次 | 核心实现 | 验证证据/文档说明 |
| :--- | :--- | :--- |
| 第1周 | IMU 采集与 Web 展示 | 包含失败三态显示、单位校验 (详见 Git 历史) |
| 第2周 | 远程拍照与状态回执 | 状态机 PENDING -> COMPLETED 流转 |
| 第3周 | 按键触发与断网反馈 | 断网时本地 LED 反馈与防误报验证 |
| 第4周 | AI 自然语言与 BLE | [week4_ai_ble.md](./week4_ai_ble.md) (记录大模型接入与蓝牙固定 MAC) |
| 第5周 | 语音输入与播报 | 成功跑通 ASR -> 智谱 -> TTS 音频合成与网页播放 |
| 第6周 | 任务澄清与停止 | 测试了中途打断与迟到请求隔离 (防串台) |
| 第7周 | 按需图像采集 | 验证了防旧图机制、遮挡黄条警告、离线波形冻结 |
| 第8周 | 视觉推理与人工纠错 | 验证了真实特征打分、低分拒识 (<0.4) 与修改标签闭环 |

> **说明**：项目的完整迭代过程除了代码外，也详细保留在 Git 的 Commit 记录与各周里程碑 Tag 中。老师可通过 `git log` 或 `git checkout weekX-complete` 查看任意时间节点的状态。