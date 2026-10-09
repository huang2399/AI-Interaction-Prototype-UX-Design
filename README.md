# 人工智能交互原型设计 — ESP32-S3-EYE 智能体项目

## 项目简介

基于 ESP32-S3-EYE 开发板，构建"传感器采集 + Web 展示 + 远程控制 + AI 自然语言交互 + BLE 蓝牙 + 语音"的完整 AIoT 交互原型系统。

## 系统架构
ESP32-S3-EYE (QMA6100P IMU / OV2640 摄像头 / 麦克风)
│ WiFi (HTTP)
▼
本地 Flask 服务器 (VPS)
│
├── 智谱 GLM (AI 对话 + Function Calling)
├── 阿里云百炼 (语音识别 + 语音合成)
▼
Web 前端 (深色控制台 + 波形图 + 照片画廊 + AI 聊天)
▲
│ BLE
│
手机 BLE 调试助手

## 每周任务与对应标签

| 周次 | 任务 | Git 标签 |
| :--- | :--- | :--- |
| 第1周 | IMU 采集 + Web 展示（基础版） | `week1-complete` |
| 第1周 | 增强版（来源 / 时间 / 单位 / 失败三态） | `week1-enhanced` |
| 第2周 | 远程拍照 + 状态回执 + 照片画廊 | `week2-complete` |
| 第3周 | 按键触发 + 本地反馈 + 断网测试 | `week3-complete` |
| 第4周 | AI 自然语言控制 | `week4-core-complete` |
| 第4周 | BLE 蓝牙控制 | `week4-ble-complete` |
| 第5周 | 语音输入 + 语音播报 | 开发中 |

## 目录结构
imu_web_demo/
├── main/ # 板端代码
│ ├── imu_web_demo.c # 主程序（IMU + WiFi + BLE + HTTP）
│ └── CMakeLists.txt
├── server/ # 服务端代码
│ ├── app.py # Flask 服务器（接收 + 状态机 + AI + 语音）
│ ├── templates/index.html # 前端页面
│ └── static/ # 静态资源
├── docs/ # 文档
│ ├── week4_ai_ble.md
│ └── README.md
├── managed_components/ # ESP-IDF 第三方组件
├── CMakeLists.txt
├── sdkconfig
└── README.md

## 关键环境变量

启动 Flask 前需要设置：

```powershell
$env:MY_IMU_LLM_KEY="..."        # 智谱 GLM API Key（第4周 AI 对话）
$env:DASHSCOPE_API_KEY="sk-..."  # 阿里云百炼 API Key（第5周 语音）

运行方式
1. 启动服务端
cd server
python app.py
服务端默认监听 0.0.0.0:5000。

2. 烧录板端
cd D:\esp_project\imu_web_demo
idf.py -p COM4 flash monitor

3. 打开网页
浏览器访问 http://127.0.0.1:5000 或 http://192.168.137.1:5000。

技术栈
硬件：ESP32-S3-EYE（QMA6100P IMU + OV2640 摄像头 + 麦克风）

板端框架：ESP-IDF v5.4.4

服务端：Python + Flask

前端：原生 HTML / CSS / JS + ECharts

AI 模型：智谱 GLM-4.7-Flash

语音服务：阿里云百炼 Paraformer + CosyVoice

通信协议：HTTP + BLE (NimBLE)

开发说明
本项目使用 Cline（AI 辅助编程）配合 Git 分支管理迭代开发。从第1周的"4 行数值"到第4周的"完整 AI 交互控制台"，逐步叠加功能。各周代码通过 Git 标签隔离保存，可随时切换回任一阶段。

---

## 📄 文件 2：`D:\esp_project\imu_web_demo\docs\README.md`

进入 `docs` 文件夹，新建 `README.md`，粘贴：

```markdown
# 项目文档索引

## 各周文档

| 周次 | 文档 | 说明 |
| :--- | :--- | :--- |
| 第1周 | week1_report.md | IMU 采集 + Web 展示（含失败三态测试） |
| 第2周 | week2_report.md | 远程拍照 + 状态回执 + 照片画廊 |
| 第3周 | week3_report.md | 按键触发 + 断网本地反馈测试 |
| 第4周 | week4_ai_ble.md | AI 自然语言控制 + BLE 蓝牙 |

> 说明：文档随每周任务逐步补充。目前已完成第四周文档。