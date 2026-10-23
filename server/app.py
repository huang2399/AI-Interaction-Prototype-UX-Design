from flask import Flask, request, jsonify, send_from_directory, render_template
import time, threading, os, glob, json, math, random, re, tempfile, uuid
from collections import deque

# ---- DashScope (Aliyun Bailian) for ASR + TTS ----
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
_dashscope_available = False
try:
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback
    from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
    if DASHSCOPE_API_KEY:
        dashscope.api_key = DASHSCOPE_API_KEY
        _dashscope_available = True
        print(f"【语音】DashScope SDK 已初始化 (key: {DASHSCOPE_API_KEY[:4]}****{DASHSCOPE_API_KEY[-4:] if len(DASHSCOPE_API_KEY)>=8 else '---'})")
    else:
        print("【语音】⚠ DASHSCOPE_API_KEY 未设置，语音功能不可用")
except ImportError:
    print("【语音】⚠ dashscope 未安装，请执行 pip install dashscope")
except Exception as e:
    print(f"【语音】⚠ DashScope 初始化失败: {e}")

VOICE_DIR = os.path.join(os.path.dirname(__file__), 'static', 'voice')
os.makedirs(VOICE_DIR, exist_ok=True)

app = Flask(__name__)
# Photo storage is resolved relative to this file so the app keeps working when
# the project folder is moved/copied (these were absolute paths of one machine).
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))      # <repo>/server
_PROJECT_DIR = os.path.dirname(_BASE_DIR)                   # <repo>
_LEGACY_PHOTO_DIR = r'D:\esp_project\imu_web_demo\photos'
_LEGACY_LEDGER_FILE = r'D:\esp_project\imu_web_demo\server\photos_ledger.json'
UPLOAD_FOLDER = os.path.join(_PROJECT_DIR, 'photos')
if not os.path.isdir(UPLOAD_FOLDER) and os.path.isdir(_LEGACY_PHOTO_DIR):
    UPLOAD_FOLDER = _LEGACY_PHOTO_DIR                       # keep serving old data
LEDGER_FILE = os.path.join(_BASE_DIR, 'photos_ledger.json')
if not os.path.exists(LEDGER_FILE) and os.path.exists(_LEGACY_LEDGER_FILE):
    LEDGER_FILE = _LEGACY_LEDGER_FILE
MIN_PHOTO_SIZE = 500  # reject images smaller than 500 bytes (corrupt/empty)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------- week7: 按需单张采集（One-Shot Capture）门限 ----------------
# 任务书要求：
#   1) 点击抓拍后状态必须精确到 PENDING → ACCEPTED → “采集中...”，超过 10s 未收到
#      图片或板端报错必须如实显示“采集失败”，绝不刷新上一张旧图糊弄；
#   2) 每次采集成败都要在 photos_ledger.json 记录 request_id 与毫秒级时间戳；
#   3) <500B 或纯黑（平均灰度极低）→ 遮挡/画面不可用（黄色告警）；
#   4) 连续 2 次无有效采集 → 锁定抓拍按钮 30s。
#   5) week7 Bug1 修复：遮挡/偏黑阈值放宽到平均灰度 < 40（原 <12 会漏判“遮住镜头”的黑图）。
CAPTURE_TIMEOUT_SECONDS = 10.0   # 下发后 10s 内未收到图片 → 采集失败
COLLECTING_AFTER_SECONDS = 2.5   # 板端 poll 后固定 2s 冷却才开始抓帧 → 之后进入“采集中...”
COLLECT_LOCK_FAILS = 2           # 连续 2 次采集失败/无响应 → 锁定抓拍按钮
COLLECT_LOCK_SECONDS = 30.0      # 锁定 30 秒后自动解锁
BLOCKED_MIN_SIZE = 500           # < 500B → 空帧，判定画面不可用（镜头遮挡）
# week7 fix(Bug1): 阈值由 12 放宽到 40。实测“遮住镜头”的黑图平均灰度 13.5 / 16.97（AGC+噪声
# 不可能压到 <12），而正常室内画面 76~162，40 与两者都有足够安全裕量，既不漏报也不误报。
BLOCKED_MAX_GRAY = 40.0          # 平均灰度 < 40/255 → 偏黑，判定画面不可用（镜头遮挡）
DARK_LEVEL = 64                  # dark_ratio 统计级别（灰度 < 64 视为“暗像素”，仅诊断用）
GRAY_SAMPLE = 64                 # 灰度统计前缩放到 64×64，足够快也足够准
# week7 fix(Bug2): 设备“在线”以【板端心跳】为准（板端 2s 轮询 + 1Hz IMU 上传），
# 而不是任务状态；超过该窗口没有任何板端请求 → 判定离线 → 前端必须冻结波形、绝不注入模拟数据。
DEVICE_ONLINE_WINDOW = 6.0       # 秒；>6s 无心跳 = 离线（抓拍入口另用 60s 宽松窗口，见 trigger_capture）

# ---------------- week10: 多源传感器状态 / 三种时间戳 / 数据陈旧（Stale）判定 ----------------
# 任务书要求“区分传感器观测与当前状态”——绝不能把旧数据当成当前状态展示。实现要点：
#   1) 三种时间【严格分开】，任何一处都不允许互相冒充：
#      collected_at —— 传感器采集时间：只有板端明确上报才可信；没上报就是 null，
#                      绝不用“服务器接收时间”伪装成采集时间（不同源 = 不同字段）；
#      received_at  —— 服务器接收时间：权威、必有，用于算传输/入库延迟；
#      displayed_at —— 前端展示刷新时间：由浏览器 POST /api/display_ack 上报并写回台账；
#   2) 每个传感器源各自维护状态与时间戳（imu_state / camera_state / location_state），
#      某个源陈旧绝不影响其它源的判定（不允许“拿最后一次成功值顶替当前状态”）；
#   3) 数据陈旧判定（IMU 观测年龄，基准是【真实板端采样】而不是模拟预热点）：
#        age <= IMU_STALE_SECONDS(5s)    → online  （绿：数据新鲜）
#        5s < age <= IMU_OFFLINE_SECONDS(10s) → stale（黄：数据陈旧 / 网络连通但传感器无新数据）
#        age > IMU_OFFLINE_SECONDS(10s)  → offline（红：数据丢失 / 设备离线）
#      从未收到过真实采样 → no_data；板端上报 IMU 故障 → fail。
IMU_STALE_SECONDS = 5.0           # > 5s 无新 IMU 观测 → stale（黄底：数据陈旧）
IMU_OFFLINE_SECONDS = 10.0        # > 10s 无新 IMU 观测 → offline（红底：数据丢失/设备离线）
LOCATION_STALE_SECONDS = 30.0     # 定位观测陈旧阈值（仅用于状态展示与提示）
LOCATION_OFFLINE_SECONDS = 120.0  # 定位观测丢失阈值（仅用于状态展示与提示）
OBSERVATION_NO_FIX_NOTE = "照片已拍，但本次无位置数据（保留历史位置）"   # week10 边界用例文案（前端与后端共用同一句）
OBSERVATION_NET_NO_DATA = "网络连通，但传感器无新数据"                  # week10 边界用例文案（同上）

try:
    from PIL import Image as _PILImage   # 仅用于“遮挡初判”的灰度统计（缺失时自动降级）
    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None
    _PIL_AVAILABLE = False
    print("【告警】未安装 Pillow，遮挡初判仅按文件大小判定（pip install pillow 可启用灰度判定）")

# ---------------- week8: 轻量视觉推理（真实像素特征 + 可解释分类器） ----------------
# 任务书要求“把视觉推理结果变成可理解反馈”，同时硬性规定“未经校准的模型分数不能写成现实正确率”。
# 因此这里的做法是：
#   1) 特征全部来自真实像素统计（亮度/对比度/饱和度/色相分布/彩色度/纹理梯度），
#      代码里没有任何写死的“键盘/杯子”之类结论，也没有编造任何位置坐标；
#   2) 分类器是确定性规则集成：score = 特征隶属度 × 区分度裕量，只表示“这幅图有多像该描述”，
#      是【置信度】而不是经过标注集校准的准确率（calibrated=False，前端也只显示“置信度”）；
#   3) 最高置信度 < INFER_LOW_CONF(0.4) → status=low_confidence，
#      如实提示“无法确定画面内容，请重新采集”，并允许人工纠正
#      （/api/correct_label 写回台账，标记 ai_source='manual'）。
# 特征提取后端二选一：检测到 OpenCV 就用 cv2（RGB→HSV + 直方图），
# 否则用 Pillow + NumPy 做等价实现（本机 Anaconda 未装 cv2，实测走 NumPy 通道）。
try:
    import numpy as _np
    _NP_AVAILABLE = True
except ImportError:
    _np = None
    _NP_AVAILABLE = False
    print("【视觉推理】⚠ 未安装 NumPy，/api/infer_image 将返回 error（pip install numpy 可启用）")

try:
    import cv2 as _cv2
    _CV2_AVAILABLE = True
except ImportError:
    _cv2 = None
    _CV2_AVAILABLE = False

# Pillow ≥ 9.1 用 Image.Resampling.LANCZOS，旧版本用 Image.LANCZOS（缺失时传 None 用默认算法）
_PIL_RESAMPLE = getattr(getattr(_PILImage, "Resampling", _PILImage), "LANCZOS", None)

INFER_SAMPLE = 96            # 推理前缩放到 96×96：特征稳定且单张耗时毫秒级
INFER_LOW_CONF = 0.4         # 最高置信度 < 0.4 → low_confidence（任务书硬性要求）
INFER_ENGINE = "week8-lite-vision-v1"   # 自研轻量特征分类器（无外部权重文件）
# 可解释标签集：前端“人工纠正”下拉框直接复用这一份，避免前后端两套文案
AI_LABELS = [
    "画面过暗（疑似遮挡）",
    "画面过曝（强光/反光）",
    "偏绿环境（植被/绿幕）",
    "偏蓝画面（天空/水面/冷光）",
    "暖色环境（室内灯光/木质）",
    "低彩度平坦画面（白墙/桌面/弱光室内）",
    "细节丰富的杂色场景",
    "常规场景（亮度与彩度均衡，无强烈色偏）",
]
print(f"【视觉推理】引擎 {INFER_ENGINE} 就绪 · 特征后端 "
      f"{'opencv' if _CV2_AVAILABLE else 'pillow+numpy'} · 拒识阈值 {INFER_LOW_CONF}")

# ---------------- week9: 视觉事件触发的【主动询问】(Proactive Prompt) + 冷却 ----------------
# 任务书要求：当视觉引擎判定“画面不可用”时（遮挡 / 置信度 < 0.4 / 空帧），系统必须【主动询问】
# 用户是否重拍，并处理【确认】/【忽略】与【冷却】：
#   1) 判定依据全部来自真实数据：week7 的遮挡初判（平均灰度 < 40 或 < 500B 空帧）、
#      week8 的置信度（< INFER_LOW_CONF 拒识）、以及采集失败（空帧/无图片）—— 不编造任何结论；
#   2) 用户点“忽略”后 PROMPT_COOLDOWN_SECONDS(30s) 内【不再弹窗】（黄条/徽章照旧显示，
#      后端只累加 suppressed 计数并打日志），避免系统反复打扰；
#   3) 每次询问都带 prompt_id = "pr_<request_id>"，动作请求必须与 request_id 匹配，
#      否则按“已过期”拒绝（409）→ 绝不给 A 照片弹窗却重拍 B；
#   4) 用户动作写回 photos_ledger.json 的新字段 user_action = confirmed_retake / ignored_retake。
PROMPT_COOLDOWN_SECONDS = 30.0     # 用户忽略后的静默期（任务书硬性要求 30s）
PROMPT_REASK_SECONDS = 60.0        # 同一张画面已询问且用户未操作 → 该窗口内不重复弹窗（防 2s 轮询刷屏）
PROMPT_VOICE_TEXT = "画面无法识别，是否重拍？"   # 浏览器 TTS 播报文案（权限不允许时只保留弹窗文字）
# 询问文案：原因 → (标题, 正文)。只描述画面级特征（亮度/置信度/空帧），不含编造的物体与坐标。
PROMPT_TEXT = {
    "blocked": ("识别到画面过暗/模糊", "⚠️ 识别到画面过暗/模糊（疑似遮挡）。是否立即重新抓拍？"),
    "low_confidence": ("识别置信度不足", "⚠️ 视觉模型无法确定画面内容（置信度偏低）。是否立即重新抓拍？"),
    "infer_error": ("画面无法解码", "⚠️ 视觉推理不可用（图片无法解码或已清理）。是否立即重新抓拍？"),
    "no_image": ("未收到有效画面", "⚠️ 本次未收到有效画面（空帧/采集失败）。是否立即重新抓拍？"),
}

task_state = {
    "task_id": None,
    "status": "IDLE",  # IDLE, PENDING, ACCEPTED, COLLECTING, COMPLETED, TIMEOUT, FAIL
    "last_update": time.time(),
    "latest_photo": None,
    "trigger_source": "web",
    "ack_needed": False,
    # ---- week7: 本次采集与 request_id / 毫秒时间戳的强关联 ----
    "request_id": None,       # 本次采集的 request_id（cap_* 云端下发 / hw_* 板端主动/BLE）
    "created_at_ms": None,    # 指令下发时刻（毫秒 epoch）
    "accepted_at": None,      # 板端拉走指令的时刻
    "deadline": None,         # 采集硬截止时刻（created_at + CAPTURE_TIMEOUT_SECONDS）
    "fail_reason": None,      # timeout / offline / device_error
    "result": None,           # 成功时的元数据（大小 / 平均灰度 / 是否遮挡）
    # ---- week9: 主动询问 → 重拍的溯源字段（本次采集是谁建议重拍的） ----
    "retake_of": None,        # 源自哪一次采集（被判定“画面不可用”的 request_id）
    "prompt_id": None,        # 源自哪一次主动询问（"pr_<request_id>"）
}
device_info = {
    "ip": None,        # learned from device poll/upload requests (remote_addr)
    "last_seen": None,
    "rssi": 32,        # board firmware does not report RSSI yet -> hard-coded per requirement
}
lock = threading.Lock()
TIMEOUT_SECONDS = CAPTURE_TIMEOUT_SECONDS   # week7: 由 30s 收紧为 10s（任务书硬性要求）

# ---------------- week7: 抓拍门控（连续失败 → 30s 锁定） ----------------
capture_lock = threading.Lock()
capture_gate = {
    "consecutive_fails": 0,   # 连续失败次数（第 1 次仅计数，第 2 次锁定）
    "locked_until": 0.0,      # 锁定截止时刻（time.time() 基准，便于前端倒计时）
    "last_fail": None,        # {"reason": ..., "detail": ..., "ts_ms": ...}
}

# ---------------- week9: 主动询问门控（30s 冷却 + 去重 + 已处理记录） ----------------
prompt_lock = threading.Lock()
prompt_gate = {
    "cooldown_until": 0.0,     # 静默期截止（time.time() 基准）→ 期间不弹窗，只后台记录
    "last_action": None,       # ignored_retake / confirmed_retake
    "last_action_ts_ms": None,
    "last_request_id": None,   # 最近一次动作关联的采集 request_id
    "last_prompt_id": None,    # 最近一次动作关联的 prompt_id
    "suppressed": 0,           # 冷却期内被压制（不弹窗、仅记录）的【不同画面】数量
    "suppressed_ids": set(),   # 已计入 suppressed 的 request_id（同一张画面反复轮询只计一次）
    "suppressed_last": None,   # {"request_id": ..., "reason": ..., "ts_ms": ...}
    "asked": {},               # request_id -> {"prompt_id":..., "ts":..., "reason":...}（防重复弹窗）
    "handled": {},             # request_id -> 用户动作（已处理过的画面不再询问）
    "counters": {"checked": 0, "ask": 0, "confirmed": 0, "ignored": 0},
}


def _prompt_state():
    """主动询问门控快照：冷却到期惰性解锁（不需要后台线程），供前端显示倒计时。"""
    now = time.time()
    with prompt_lock:
        if prompt_gate["cooldown_until"] and now >= prompt_gate["cooldown_until"]:
            prompt_gate["cooldown_until"] = 0.0          # 静默期结束 → 恢复可询问
            print("【主动询问·冷却结束】静默期已过 → 恢复“画面不可用即询问重拍”")
        rem = max(0.0, prompt_gate["cooldown_until"] - now)
        return {
            "cooldown_active": rem > 0,
            "cooldown_remaining": round(rem, 1),
            "cooldown_seconds": PROMPT_COOLDOWN_SECONDS,
            "reask_seconds": PROMPT_REASK_SECONDS,
            "last_action": prompt_gate["last_action"],
            "last_action_ts_ms": prompt_gate["last_action_ts_ms"],
            "last_request_id": prompt_gate["last_request_id"],
            "last_prompt_id": prompt_gate["last_prompt_id"],
            "suppressed": prompt_gate["suppressed"],
            "suppressed_last": prompt_gate["suppressed_last"],
            "counters": dict(prompt_gate["counters"]),
        }


def _prompt_cooldown_active():
    """当前是否处于“用户已忽略 → 不再打扰”的静默期。"""
    with prompt_lock:
        return prompt_gate["cooldown_until"] > time.time()

# ================== week10: 多源传感器独立状态 + 三种时间戳 ==================
# 设计原则（任务书“区分传感器观测与当前状态”）：
#   * 每个源的状态只由【该源自己的观测/事件】决定（IMU 观测 / 抓拍状态机 / 定位观测），
#     某个源陈旧时其它源照旧判定 —— 不允许用别的源的“新鲜”来掩盖本源的陈旧；
#   * 状态推导是【惰性】的：每次读取用「现在 - 该源最后一次真实观测时间」现算，
#     所以 age_sec 永远在涨，不会出现“最后一次成功值被当成当前状态”的假象；
#   * 所有时间都是 epoch 秒 / 毫秒，前端负责本地化显示。
sensor_lock = threading.Lock()
imu_fail = {                      # 板端上报的 IMU 故障（真故障才置位，不猜）
    "active": False,
    "detail": None,
    "since_ms": None,
}
imu_display = {                   # 前端展示刷新记录（displayed_at 的回执）
    "displayed_at": None,         # epoch 秒（浏览器时间）
    "displayed_at_ms": None,      # epoch 毫秒
    "request_id": None,           # 这次展示讨论的是哪一次采集
    "photo": None,                # 哪张图（可空）
    "source": None,               # 哪个接口上报的（imu / observation / ...）
    "received_at": None,          # 上报时刻的服务器时间（对照浏览器时间的偏差用）
    "client_time_ms": None,       # 浏览器本地时钟（用于展示两边时钟差）
    "clock_skew_ms": None,        # 浏览器时间 - 服务器时间（>0 表示浏览器走得快）
}
location_state = {                # 定位模块（板端未上报 GPS 硬件时如实为 no_data）
    "fix": False,                 # 本次是否有位置解
    "latitude": None,
    "longitude": None,
    "satellites": None,
    "accuracy_m": None,
    "collected_at_ms": None,      # 传感器采集时间（板端上报才有）
    "received_at_ms": None,       # 服务器接收时间
    "received_at": None,
    "last_fix_ms": None,          # 最近一次【成功定位】的采集时间（历史位置，仅作参考）
    "last_fix_received_at_ms": None,
    "last_fix_latitude": None,
    "last_fix_longitude": None,
    "last_fix_satellites": None,
    "last_fix_request_id": None,  # 那次定位是绑在哪一次采集上的
    "fail": False,
    "fail_detail": None,
    "fail_since_ms": None,
    "count": 0,                   # 收到过的定位观测次数（含 fail）
}
camera_last_success_ms = None     # 最近一次“成功拿到画面”的服务器接收时间（毫秒）
camera_last_fail_ms = None        # 最近一次“采集失败”的判定时间（毫秒，用于区分“没拍”与“拍失败”）


def _ms_str(ms):
    """epoch 毫秒 → 'YYYY-mm-dd HH:MM:SS.mmm'（与 week7 画廊时间戳同一格式）。"""
    if ms is None:
        return None
    ms = int(ms)
    return (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ms / 1000.0))
            + ".%03d" % (ms % 1000))


def _parse_ms_field(value):
    """板端上报的时间字段 → epoch 毫秒（>1e11 视为毫秒、>1e9 视为秒，其余视为非法）。

    week10：采集时间（collected_at）只接受板端明确上报的值，解析不出来就是 None，
    由调用方决定“标注 unknown”，绝不用服务器时间冒充。"""
    if value in (None, "", 0, "0"):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e11:
        return int(v)
    if v > 1e9:
        return int(v * 1000)
    return None


def _imu_latest_real_sample():
    """最近一条【真实板端采样】的时间戳（epoch 秒）。

    注意用 imu_last_real（只有 /api/device/upload_imu 真实上报才会推进）而不是
    imu_history 的最后一点：这样“模拟预热点/SIM 点”永远不会被算成真实观测年龄。
    """
    with imu_lock:
        t = float(imu_last_real or 0.0)
        n = sum(1 for s in imu_history if s.get("source") == "device")
    return (t if t > 0 else None), n


def _obs_age_sec(ts, now=None):
    """观测年龄（秒）。未收到过观测 → None（不是 0，也不允许拿别的源的时间顶替）。"""
    if ts is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, now - ts)


def _imu_state(now=None):
    """IMU 源状态：online / stale / offline / no_data / fail。

    * online   —— age_sec <= IMU_STALE_SECONDS(5s)：数据新鲜（前端绿底）
    * stale    —— 5s < age_sec <= IMU_OFFLINE_SECONDS(10s)：数据陈旧（前端黄底）
    * offline  —— age_sec > 10s：数据丢失 / 设备离线（前端红底）
    * no_data  —— 从未收到真实采样（新板子/刚启动/固件没在传）
    * fail     —— 板端明确上报 IMU 故障
    注意：状态是【现算】的，age_sec 只增不减 —— 绝不会把“最后一次成功值”当成当前状态。
    """
    now = time.time() if now is None else now
    with sensor_lock:
        fail = dict(imu_fail)
    last, samples = _imu_latest_real_sample()
    age = _obs_age_sec(last, now)
    last_ms = None if last is None else int(last * 1000)
    if fail["active"]:
        status = "fail"
        hint = "板端上报 IMU 故障：" + str(fail.get("detail") or "未知原因")
    elif last is None:
        status = "no_data"
        hint = "尚未收到任何 IMU 采样（板端未开始上传）"
    elif age <= IMU_STALE_SECONDS:
        status = "online"
        hint = "IMU 数据新鲜"
    elif age <= IMU_OFFLINE_SECONDS:
        status = "stale"
        hint = "数据陈旧：超过 %.0f 秒未收到新 IMU 采样" % IMU_STALE_SECONDS
    else:
        status = "offline"
        hint = "数据丢失：超过 %.0f 秒未收到新 IMU 采样" % IMU_OFFLINE_SECONDS
    return {
        "status": status,
        "age_sec": None if age is None else round(age, 1),
        "last_time": None if last_ms is None else _ms_str(last_ms),
        "last_time_ms": last_ms,
        "displayed_at": _display_public().get("displayed_at"),     # 三种时间的第三种（前端刷新）
        "fresh": status == "online",
        "stale": status in ("stale", "offline", "no_data", "fail"),
        "stale_threshold_sec": IMU_STALE_SECONDS,       # > 5s → stale
        "offline_threshold_sec": IMU_OFFLINE_SECONDS,   # > 10s → offline
        "offline_at_ms": None if last_ms is None else last_ms + int(IMU_OFFLINE_SECONDS * 1000),
        "fail": bool(fail["active"]),
        "fail_detail": fail.get("detail"),
        "fail_since_ms": fail.get("since_ms"),
        "samples": samples,                             # 已接收的真实采样条数
        "exclude_simulated": True,                      # SIM 预热点不参与年龄计算（诚实标注）
        "hint": hint,
    }


def _camera_state(now=None):
    """摄像头/抓拍源状态：ready / capturing / fail（与 week7 采集状态机同一份事实）。"""
    now = time.time() if now is None else now
    with lock:
        ts = dict(task_state)
    gate = _capture_gate_state()
    with sensor_lock:
        last_ms = camera_last_success_ms
        last_fail_ms = camera_last_fail_ms
    st = ts.get("status") or "IDLE"
    status = {"PENDING": "capturing", "ACCEPTED": "capturing",
              "COLLECTING": "capturing", "COMPLETED": "ready",
              "TIMEOUT": "fail", "FAIL": "fail"}.get(st, "ready")
    hint = {"ready": "摄像头就绪（最近一次采集链路正常）",
            "capturing": "正在采集画面（%s）" % st,
            "fail": "最近一次采集失败：%s" % (ts.get("fail_reason") or st)}[status]
    return {
        "status": status,
        "capture_state": st,
        "last_time": None if last_ms is None else _ms_str(last_ms),
        "last_time_ms": last_ms,
        "age_sec": _obs_age_sec(None if last_ms is None else last_ms / 1000.0, now),
        # week10: “没有画面”与“画面拍失败”是两件事，分开记录、分别展示
        "last_fail_time": None if last_fail_ms is None else _ms_str(last_fail_ms),
        "last_fail_time_ms": last_fail_ms,
        "fail_age_sec": _obs_age_sec(None if last_fail_ms is None else last_fail_ms / 1000.0, now),
        "fail_reason": ts.get("fail_reason"),
        "has_frame": last_ms is not None,
        "request_id": ts.get("request_id"),
        "locked": gate["locked"],
        "lock_remaining": gate["locked_remaining"],
        "consecutive_fails": gate["consecutive_fails"],
        "fresh": status != "fail",
        "stale": status == "fail",
        "stale_threshold_sec": None,      # 摄像头是【事件源】不是周期采样源：没有“几秒没数据”的阈值
        "hint": hint,
    }


def _location_state(now=None):
    """定位源状态：online / stale / offline / no_data / fail。

    定位模块在本项目固件里【没有上报任何位置数据】，因此如实为 no_data；
    有历史成功定位时，历史位置单独放在 historical 字段里（只作参考、带“不代表本次”标注），
    绝不会被当成“当前状态”替代本次缺失的观测。
    """
    now = time.time() if now is None else now
    with sensor_lock:
        loc = dict(location_state)
    last_fix_sec = loc["last_fix_ms"] / 1000.0 if loc.get("last_fix_ms") else None
    age = _obs_age_sec(last_fix_sec, now)
    if loc.get("fail"):
        status = "fail"
    elif loc.get("fix"):
        status = "online"
    elif last_fix_sec is None:
        status = "no_data"
    elif age <= LOCATION_STALE_SECONDS:
        status = "online"
    elif age <= LOCATION_OFFLINE_SECONDS:
        status = "stale"
    else:
        status = "offline"
    hint = {
        "online": "定位数据新鲜",
        "stale": "定位数据陈旧（超过 %.0f 秒无新定位）" % LOCATION_STALE_SECONDS,
        "offline": "定位数据丢失（超过 %.0f 秒无新定位）" % LOCATION_OFFLINE_SECONDS,
        "no_data": "本设备未上报位置数据（拍照时不使用历史位置顶替本次观测）",
        "fail": "板端上报定位故障",
    }[status]
    has_fix = bool(loc.get("fix"))
    return {
        "status": status,
        "fix": has_fix,                       # 本次观测是否有位置解（no_fix 时严格为 False）
        "has_fix": has_fix,
        "fix_is_current": has_fix,            # 只有“本次观测”才算当前状态
        "no_fix": not has_fix,
        "age_sec": None if age is None else round(age, 1),
        "collected_at": None if not has_fix else _ms_str(loc.get("collected_at_ms")),
        "collected_at_ms": loc.get("collected_at_ms") if has_fix else None,
        "received_at": loc.get("received_at") and _ms_str(int(loc["received_at"] * 1000)),
        "received_at_ms": loc.get("received_at_ms") if has_fix else None,
        "last_time": None if not loc.get("collected_at_ms") else _ms_str(loc["collected_at_ms"]),
        "latitude": loc.get("latitude") if has_fix else None,
        "longitude": loc.get("longitude") if has_fix else None,
        "satellites": loc.get("satellites") if has_fix else None,
        "accuracy_m": loc.get("accuracy_m") if has_fix else None,
        "request_id": loc.get("request_id") if has_fix else None,
        "fresh": status == "online",
        "stale": status in ("stale", "offline", "no_data", "fail"),
        "stale_threshold_sec": LOCATION_STALE_SECONDS,
        "offline_threshold_sec": LOCATION_OFFLINE_SECONDS,
        "fail": bool(loc.get("fail")),
        "fail_detail": loc.get("fail_detail"),
        "observations": loc.get("count", 0),
        # 历史位置（只作参考，绝不顶替当前观测）
        "historical": {
            "available": bool(loc.get("last_fix_ms")),
            "collected_at": None if not loc.get("last_fix_ms") else _ms_str(loc["last_fix_ms"]),
            "collected_at_ms": loc.get("last_fix_ms"),
            "received_at_ms": loc.get("last_fix_received_at_ms"),
            "latitude": loc.get("last_fix_latitude"),
            "longitude": loc.get("last_fix_longitude"),
            "satellites": loc.get("last_fix_satellites"),
            "request_id": loc.get("last_fix_request_id"),
            "age_sec": None if age is None else round(age, 1),
            "note": "历史位置仅作参考，不代表本次采集时的位置" if loc.get("last_fix_ms") else None,
        },
        "hint": hint,
    }


def _device_state(now=None):
    """网络/设备心跳状态（week7 既有语义，此处只做一次封装，字段保持兼容）。"""
    now = time.time() if now is None else now
    last = device_info.get("last_seen")
    age = _obs_age_sec(last, now)
    online = bool(last and age is not None and age <= DEVICE_ONLINE_WINDOW)
    return {
        "online": online,
        "ip": device_info.get("ip"),
        "rssi": device_info.get("rssi"),
        "last_seen": last,
        "last_seen_ms": None if last is None else int(last * 1000),
        "age_sec": None if age is None else round(age, 1),
        "window_sec": DEVICE_ONLINE_WINDOW,
        "hint": "网络连通（板端心跳正常）" if online else "网络不通 / 设备离线（未收到板端心跳）",
    }


def _freshness_policy():
    """阈值与文案集中一处：前端渲染底色/文案时直接取用，避免前后端两套阈值。"""
    return {
        "imu_stale_sec": IMU_STALE_SECONDS,
        "imu_offline_sec": IMU_OFFLINE_SECONDS,
        "location_stale_sec": LOCATION_STALE_SECONDS,
        "location_offline_sec": LOCATION_OFFLINE_SECONDS,
        "device_window_sec": DEVICE_ONLINE_WINDOW,
        "stale_note": "数据陈旧",
        "offline_note": "数据丢失/设备离线",
        "no_fix_note": OBSERVATION_NO_FIX_NOTE,
        "network_but_no_data_note": OBSERVATION_NET_NO_DATA,
    }


def _sensor_role_hints(net_ok, imu):
    """week10 边界用例②：网络在线但数据未更新 → 明确标注，而不是伪装成在线数据。"""
    if net_ok and imu["status"] in ("stale", "no_data"):
        return OBSERVATION_NET_NO_DATA
    if (not net_ok) and imu["status"] == "online":
        return "IMU 观测仍新鲜，但已超过 %.0fs 无板端心跳（网络可能中断）" % DEVICE_ONLINE_WINDOW
    return None


def _current_observation(now=None):
    """当前观测快照（week10 核心）：每个源谁的、多旧的，全部摆在明面上。"""
    now = time.time() if now is None else now
    imu = _imu_state(now)
    cam = _camera_state(now)
    loc = _location_state(now)
    dev = _device_state(now)
    return {
        "server_time": _ms_str(int(now * 1000)),
        "server_time_ms": int(now * 1000),
        "imu": imu,
        "camera": cam,
        "location": loc,
        "device": dev,
        "imu_state": imu,            # 任务书命名的别名（外部脚本/前端都能直接取）
        "camera_state": cam,
        "location_state": loc,
        "device_state": dev,
        "network_ok": dev["online"],
        "network_but_no_data": _sensor_role_hints(dev["online"], imu),
        "policy": _freshness_policy(),
    }


def _display_public():
    """displayed_at 回执（前端展示刷新时间）快照。"""
    with sensor_lock:
        d = dict(imu_display)
    return {
        "displayed_at": None if not d.get("displayed_at_ms") else _ms_str(d["displayed_at_ms"]),
        "displayed_at_ms": d.get("displayed_at_ms"),
        "received_at": None if not d.get("received_at_ms") else _ms_str(d["received_at_ms"]),
        "received_at_ms": d.get("received_at_ms"),
        "request_id": d.get("request_id"),
        "photo": d.get("photo"),
        "source": d.get("source"),
        "client_time_ms": d.get("client_time_ms"),
        "clock_skew_ms": d.get("clock_skew_ms"),
        "server_age_sec": None if not d.get("received_at_ms")
        else round((time.time() * 1000 - d["received_at_ms"]) / 1000.0, 1),
    }


def _observation_public(now=None):
    """给 /api/status 与 /api/observation 的观测快照（含三种时间的来源说明 + 展示回执）。"""
    obs = _current_observation(now)
    obs["timing"] = {
        "collected_at": "传感器采集时间：仅板端上报才有（没上报就是 null，绝不用接收时间冒充）",
        "received_at": "服务器接收时间：权威时间戳，用于算传输/入库延迟",
        "displayed_at": "前端展示刷新时间：由浏览器 POST /api/display_ack 上报",
        "measured_latency": "接收时间 - 采集时间（端到端延迟；采集时间缺失时为 null 并标注原因）",
        "skew": "浏览器时间 - 服务器时间（时钟偏差，用于解释展示时间与接收时间的差）",
    }
    obs["display"] = _display_public()
    obs["no_fix_note"] = OBSERVATION_NO_FIX_NOTE
    obs["network_but_no_data_note"] = OBSERVATION_NET_NO_DATA
    return obs


def _photo_observation(entry, obs=None):
    """单张照片的“采集 / 接收 / 展示”三时间对照 + 本次位置观测（week10 对照面板用）。

    每个时间差都给出【来源】，让“为什么慢”可归因：
      upload_latency_ms  = received_at - collected_at（传输/入库；采集时间缺失 → null 并说明）
      display_latency_ms = displayed_at - received_at（页面刷新/浏览器渲染）
      skew_ms            = 浏览器时间 - 服务器时间（时钟偏差，解释上面两个差值）
    """
    obs = obs or _observation_public()
    disp = obs.get("display") or {}
    if not entry:
        return {"available": False, "note": "暂无可对照的照片（先抓拍一次）",
                "display": disp, "timing": obs.get("timing"),
                "no_fix_note": OBSERVATION_NO_FIX_NOTE}
    collected_ms = entry.get("collected_at_ms")
    received_ms = entry.get("received_at_ms") or entry.get("ts_ms")
    displayed_ms = entry.get("displayed_at_ms")
    if entry.get("name") and disp.get("photo") == entry.get("name"):
        displayed_ms = disp.get("displayed_at_ms") or displayed_ms
    loc_fix = bool(entry.get("location_fix"))
    has_hist = bool(entry.get("location_historical_available"))
    return {
        "available": True,
        "name": entry.get("name"),
        "request_id": entry.get("request_id"),
        "url": ("/photos/" + entry["name"]) if entry.get("name") else None,
        "status": entry.get("status"),
        "trigger": entry.get("trigger"),
        # ---- 三种时间（缺谁就是 null，绝不互相冒充） ----
        "collected_at": entry.get("collected_at"),
        "collected_at_ms": collected_ms,
        "collected_at_source": entry.get("collected_at_source") or "unknown",
        "received_at": entry.get("received_at") or _ms_str(received_ms),
        "received_at_ms": received_ms,
        "received_at_source": entry.get("received_at_source") or "server",
        "displayed_at": None if not displayed_ms else _ms_str(displayed_ms),
        "displayed_at_ms": displayed_ms,
        "upload_latency_ms": (int(received_ms) - int(collected_ms)) if collected_ms else None,
        "display_latency_ms": (int(displayed_ms) - int(received_ms)) if displayed_ms else None,
        "skew_ms": disp.get("clock_skew_ms"),
        # ---- 推理耗时（week8 结果，不属于传感器观测，单独列） ----
        "ai": {
            "cost_ms": entry.get("ai_cost_ms"),
            "status": "success" if entry.get("ai_corrected") else entry.get("ai_status"),
            "label": (entry.get("ai_label_manual") if entry.get("ai_corrected")
                      else entry.get("ai_label")),
            "score": entry.get("ai_score"),
            "ts_ms": entry.get("ai_ts_ms"),
            "engine": entry.get("ai_engine"),
        },
        # ---- 位置观测：本次无位置 → fix=False + latitude/longitude=None ----
        "location_fix": loc_fix,
        "location": {
            "fix": loc_fix,
            "collected_at": entry.get("location_collected_at"),
            "collected_at_ms": entry.get("location_collected_at_ms"),
            "received_at_ms": entry.get("location_received_at_ms"),
            "latitude": entry.get("location_latitude"),
            "longitude": entry.get("location_longitude"),
            "satellites": entry.get("location_satellites"),
            "request_id": entry.get("location_request_id"),
            "note": entry.get("location_note") or (None if loc_fix else OBSERVATION_NO_FIX_NOTE),
            "historical_location": {
                "available": has_hist,
                "latitude": entry.get("location_historical_latitude"),
                "longitude": entry.get("location_historical_longitude"),
                "collected_at": entry.get("location_historical_collected_at"),
                "collected_at_ms": entry.get("location_historical_collected_at_ms"),
                "note": entry.get("location_historical_note"),
            } if has_hist else None,
        },
        "location_note": entry.get("location_note") or (None if loc_fix else OBSERVATION_NO_FIX_NOTE),
        "no_fix_note": OBSERVATION_NO_FIX_NOTE,
        "timing_note": {
            "collected_at": ("板端上报的拍摄时刻" if collected_ms
                             else "板端未上报采集时间 → null（未用接收时间冒充）"),
            "received_at": "服务器收到图片并写入台账的时刻（权威）",
            "displayed_at": "浏览器刷新展示这张图的时刻（前端 /api/display_ack 上报）",
            "upload_latency_ms": "接收时间 - 采集时间（传输 + 入库延迟）",
            "display_latency_ms": "展示时间 - 接收时间（页面刷新 / 渲染延迟）",
        },
        "display": disp,
        "timing": obs.get("timing"),
    }


def _record_display_ack(request_id=None, photo=None, source="front",
                        displayed_at_ms=None, client_time_ms=None):
    """记录一次“前端已刷新展示”（displayed_at）。浏览器时间与服务器时间都留档，便于对照。"""
    now_ms = int(time.time() * 1000)
    with sensor_lock:
        if displayed_at_ms:
            imu_display["displayed_at_ms"] = int(displayed_at_ms)
            imu_display["displayed_at"] = int(displayed_at_ms) / 1000.0
        if client_time_ms:
            imu_display["client_time_ms"] = int(client_time_ms)
            imu_display["clock_skew_ms"] = int(client_time_ms) - now_ms
        imu_display["received_at_ms"] = now_ms
        imu_display["received_at"] = now_ms / 1000.0
        imu_display["request_id"] = request_id or imu_display.get("request_id")
        imu_display["photo"] = photo or imu_display.get("photo")
        imu_display["source"] = source
    return _display_public()


def _set_imu_fail(active, detail=None):
    """板端 IMU 故障的上报/清除（只在真收到板端报文时调用，不猜）。"""
    with sensor_lock:
        imu_fail["active"] = bool(active)
        if active:
            imu_fail["detail"] = detail or "板端上报 IMU 故障"
            imu_fail["since_ms"] = int(time.time() * 1000)
            print(f"【IMU 故障】{imu_fail['detail']}")
        else:
            imu_fail["detail"] = None
            imu_fail["since_ms"] = None
            print("【IMU 故障】板端恢复上报 → 故障标志清除")


def _record_location(payload, received_ms=None):
    """记录一条定位观测（真收到板端上报才调用）。fix=false 表示“本次无位置”。

    边界用例①（有历史位置但本次 no_fix）在这里就被严格区分：fix=false 时
    location_state["fix"] 保持 False，历史位置只留在 last_fix_* 里，绝不回填。
    """
    received_ms = int(time.time() * 1000) if received_ms is None else int(received_ms)
    fix = bool(payload.get("fix"))
    fail = bool(payload.get("fail"))
    collected_ms = payload.get("collected_at_ms")
    with sensor_lock:
        location_state["count"] = location_state.get("count", 0) + 1
        location_state["fix"] = fix
        location_state["fail"] = fail
        location_state["fail_detail"] = payload.get("detail") if fail else None
        location_state["fail_since_ms"] = received_ms if fail else None
        location_state["received_at_ms"] = received_ms
        location_state["received_at"] = received_ms / 1000.0
        location_state["collected_at_ms"] = collected_ms if fix else None
        location_state["latitude"] = payload.get("latitude") if fix else None
        location_state["longitude"] = payload.get("longitude") if fix else None
        location_state["satellites"] = payload.get("satellites") if fix else None
        location_state["accuracy_m"] = payload.get("accuracy_m") if fix else None
        location_state["request_id"] = payload.get("request_id") if fix else None
        if fix:
            # 成功定位 → 更新“历史位置”（仅作参考，绑定那次采集的 request_id）
            location_state["last_fix_ms"] = collected_ms or received_ms
            location_state["last_fix_received_at_ms"] = received_ms
            location_state["last_fix_latitude"] = payload.get("latitude")
            location_state["last_fix_longitude"] = payload.get("longitude")
            location_state["last_fix_satellites"] = payload.get("satellites")
            location_state["last_fix_request_id"] = payload.get("request_id")
    if fix:
        print("【定位】本次有位置解 request_id=%s lat=%s lon=%s" % (
            payload.get("request_id"), payload.get("latitude"), payload.get("longitude")))
    elif fail:
        print("【定位】板端上报定位故障：%s" % payload.get("detail"))
    else:
        print("【定位】本次无位置数据（no_fix）→ 不使用历史位置顶替")


def _observation_link(entry, request_id=None, received_ms=None):
    """把定位观测绑到某次采集上：只有【同一次采集】的位置才算本次的位置。

    返回 (location_fix, location_note, location_public)：
      * 有绑定   → (True, None, {...本次位置...})；
      * 无绑定但有历史位置 → (False, "照片已拍，但本次无位置数据（保留历史位置）",
                              {"historical_location": {...} + 明示“不代表本次”})；
      * 都没有   → (False, "本次无位置数据（设备未上报定位，且无历史位置）", {...})

    边界用例①就在这里被彻底处理：历史位置只出现在 historical_location 字段里，
    本次的 latitude/longitude 一律为 None —— 前端不可能“拿旧位置顶替”。
    """
    rid = request_id or (entry or {}).get("request_id")
    with sensor_lock:
        loc = dict(location_state)
    same_task = bool(rid) and (loc.get("request_id") == rid)
    if loc.get("fix") and loc.get("collected_at_ms") and same_task:
        return True, None, {
            "fix": True,
            "collected_at": _ms_str(loc["collected_at_ms"]),
            "collected_at_ms": loc["collected_at_ms"],
            "received_at": None if not loc.get("received_at_ms") else _ms_str(loc["received_at_ms"]),
            "received_at_ms": loc.get("received_at_ms"),
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "satellites": loc.get("satellites"),
            "accuracy_m": loc.get("accuracy_m"),
            "request_id": loc.get("request_id"),
            "historical": False,
            "note": "位置与本次采集同源（request_id 一致）",
        }
    has_hist = bool(loc.get("last_fix_ms"))
    note = OBSERVATION_NO_FIX_NOTE if has_hist else "本次无位置数据（设备未上报定位，且无历史位置）"
    return False, note, {
        "fix": False,
        "collected_at": None,
        "collected_at_ms": None,
        "received_at": None if not loc.get("received_at_ms") else _ms_str(loc["received_at_ms"]),
        "received_at_ms": loc.get("received_at_ms"),
        "latitude": None,          # 本次没有位置 → 严格为 None（绝不填历史值）
        "longitude": None,
        "satellites": None,
        "accuracy_m": None,
        "request_id": None,
        "historical": True,
        "note": note,
        "historical_location": {
            "available": has_hist,
            "collected_at": None if not has_hist else _ms_str(loc["last_fix_ms"]),
            "collected_at_ms": loc.get("last_fix_ms"),
            "received_at_ms": loc.get("last_fix_received_at_ms"),
            "latitude": loc.get("last_fix_latitude"),
            "longitude": loc.get("last_fix_longitude"),
            "satellites": loc.get("last_fix_satellites"),
            "request_id": loc.get("last_fix_request_id"),
            "note": "历史位置仅作参考，不代表本次采集时的位置" if has_hist else None,
        },
    }


# ================== week11: 多源证据 → 派生状态 → 交互上下文（规则引擎 v1.0） ==================
# 三层解释链（任务书 Week11 核心）：
#   ① observations        —— 原始证据：谁、在什么时刻、观测到了什么（IMU 三轴 / 画面灰度 /
#                            AI 标签与置信度 / 板端心跳 / 定位解），每行都带来源与采集时间戳；
#   ② derived_states      —— 由下面这张【有限规则表 CTX_RULES】推导出的状态，每条都回指
#                            rule_id + evidence_ids（可审计：为什么这么说，一眼看得到）；
#   ③ interaction_context —— 最终呈现给用户的解释：已知事实（可溯源）+ 待确认信息 + 歧义追问（选择题）。
# 硬性纪律（与 week7~week10 一脉相承）：
#   * 硬件观测(origin=sensor) 与 模型推断(origin=model/inference) 分层标注，UI 不得混装；
#   * 证据不足就如实说“证据不足”，绝不用别的源的“新鲜”掩盖本源的缺失；
#   * “摄像头被挡”只推出“画面不可用/疑似遮挡”，不得升格成“设备损坏”（must_not_claim 强制列出）；
#   * 运动（IMU 剧烈震动）与“视野正常”（AI 判正常画面）可以同时成立 → 冲突解决为
#     “设备正在移动，但视野正常”：既不升级为危险、也不判定为故障；
#   * 唯一允许说“故障”的情形是板端自报 IMU fail（规则 R2）。
RULE_VERSION = "v1.0"

# ---- 规则参数（集中一处；前端一律从 /api/context.policy 取，绝不写死任何阈值）----
CTX_MOTION_WINDOW_SAMPLES = 12      # 运动判定窗口：最近 N 条【真实】采样（SIM 预热点不参与）
CTX_MOTION_STILL_RATIO = 0.06       # Δ|a| / 基线 ≤ 6%  → 基本静止
CTX_MOTION_SHAKE_RATIO = 0.18       # Δ|a| / 基线 > 18% → 剧烈震动（中间区间为“明显移动/晃动”）
CTX_MOTION_MIN_SAMPLES = 2          # 少于 2 条真实采样 → 不给运动结论（证据不足就明说）
CTX_VISION_VALID_SEC = 120.0        # 画面结论有效期：超时只能说“没有新画面”，不得引用旧结论
CTX_ANSWER_SILENCE_SEC = 300.0      # 同一追问已作答后 5 分钟内不再问（用户已经给过答案）
CTX_SNOOZE_SEC = 60.0               # 用户选“不确定/稍后”后的静默期
CTX_QUESTION_MIN_INTERVAL = 60.0    # 同一追问重复弹窗的最小间隔（防 2s 轮询刷屏）
CTX_MAX_QUESTIONS = 2               # 一次最多呈现 2 条待确认追问（避免连环打扰）
CTX_ANSWER_KEEP = 40                # 保留最近 N 次用户澄清（谁、什么时候确认的，可追溯）
CTX_VISION_LABEL_ORIGIN_MANUAL = "用户人工纠正"
print(f"【上下文规则】引擎版本 {RULE_VERSION} · 运动判定窗口 {CTX_MOTION_WINDOW_SAMPLES} 条真实采样 · "
      f"静止 ≤{CTX_MOTION_STILL_RATIO:.0%} / 剧烈 >{CTX_MOTION_SHAKE_RATIO:.0%} · 画面结论有效期 {CTX_VISION_VALID_SEC:.0f}s")


def _ctx_clock(ms):
    """epoch 毫秒 → 'HH:MM:SS'（证据行的时间戳前缀，与前端本地时钟同一时区口径）。"""
    if not ms:
        return None
    return time.strftime('%H:%M:%S', time.localtime(int(ms) / 1000.0))


def _imu_motion(now=None):
    """从【最近的真实采样】推导运动程度（只读，不改变任何状态）。

    * metric   = |a| = sqrt(ax²+ay²+az²)；判定只看【相对波动】，单位无关：
                 baseline = 窗口内 |a| 的中位数（兼容板端 g / m²·s⁻¹ 两种口径），
                 Δ|a| = 窗口内相对基线的最大偏离，ratio = Δ|a| / baseline；
    * level    = still（ratio ≤ 6%）/ moving（6%~18%）/ shaking（>18%）/ no_data（真实采样不足）；
    * 只统计 source == "device" 的点：板端没在传时 SIM 预热点绝不会被当成“设备在动”。
    """
    now = time.time() if now is None else now
    with imu_lock:
        win = [dict(s) for s in imu_history
               if s.get("source") == "device" and None not in (s.get("ax"), s.get("ay"), s.get("az"))]
    win = win[-CTX_MOTION_WINDOW_SAMPLES:]
    latest = dict(win[-1]) if win else None
    if latest:
        latest["collected_at"] = _ms_str(latest.get("collected_at_ms"))
        latest["received_at"] = _ms_str(latest.get("received_at_ms"))
        latest["time"] = _ctx_clock(latest.get("received_at_ms"))
    base = {
        "level": "no_data", "label": "无运动证据（未收到真实采样）",
        "samples": len(win), "window_samples": CTX_MOTION_WINDOW_SAMPLES,
        "baseline": None, "deviation": None, "ratio": None, "span_sec": None,
        "latest": latest, "source": "device", "exclude_simulated": True,
        "thresholds": {"still_ratio": CTX_MOTION_STILL_RATIO,
                       "shake_ratio": CTX_MOTION_SHAKE_RATIO,
                       "min_samples": CTX_MOTION_MIN_SAMPLES},
        "metric": "|a| = √(ax²+ay²+az²)；Δ|a| = 窗口内相对基线的最大偏离；ratio = Δ|a| / 基线（无量纲）",
        "note": "运动量只能说明“设备在动”，不能说明是谁在动、也不能说明设备损坏 —— 证据不足时不给结论。",
    }
    if len(win) < CTX_MOTION_MIN_SAMPLES:
        return base
    mags = [math.sqrt(s["ax"] ** 2 + s["ay"] ** 2 + s["az"] ** 2) for s in win]
    srt = sorted(mags)
    baseline = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2.0
    dev = max(abs(m - baseline) for m in mags)
    ratio = (dev / baseline) if baseline > 1e-6 else 0.0
    if ratio <= CTX_MOTION_STILL_RATIO:
        level, label = "still", "基本静止"
    elif ratio <= CTX_MOTION_SHAKE_RATIO:
        level, label = "moving", "明显移动/晃动"
    else:
        level, label = "shaking", "剧烈震动"
    base.update({
        "level": level, "label": label,
        "baseline": round(baseline, 4), "deviation": round(dev, 4), "ratio": round(ratio, 4),
        "span_sec": round(float(win[-1].get("t", 0)) - float(win[0].get("t", 0)), 1),
        "first_time": _ctx_clock(win[0].get("received_at_ms")),
        "last_time": _ctx_clock(win[-1].get("received_at_ms")),
        "age_sec": _obs_age_sec(win[-1].get("received_at_ms") / 1000.0, now),
    })
    return base


def _vision_evidence(request_id=None, filename=None, now=None):
    """画面这一路的证据（硬件灰度与模型标签分开标注，绝不混装）。

    verdict（判定权 100% 来自 week7 遮挡初判 + week8 置信度，本函数不新增任何猜测）：
      ok          —— 有可用画面且 AI 判为常规场景（置信度达标）
      obstructed  —— 画面不可用（平均灰度 < BLOCKED_MAX_GRAY / <500B 空帧 / 板端报错）
      uncertain   —— AI 置信度 < INFER_LOW_CONF：模型自己说“不确定”
      no_frame    —— 本次采集失败（空帧/超时/设备错误），根本没有画面
      error       —— 图片无法解码 / 原图已清理
      expired     —— 有历史结论但超过 CTX_VISION_VALID_SEC：只能说“没有新画面”
      unknown     —— 还没有任何画面证据
    """
    now = time.time() if now is None else now
    entry = (ledger_find_any(request_id=request_id or None, filename=filename or None)
             if (request_id or filename) else ledger_find_any())
    v = _prompt_verdict(entry=entry)
    rid = v.get("request_id") or request_id or (entry or {}).get("request_id")
    received_ms = (entry or {}).get("received_at_ms") or (entry or {}).get("ts_ms")
    collected_ms = (entry or {}).get("collected_at_ms")
    age = _obs_age_sec(None if received_ms is None else received_ms / 1000.0, now)
    fresh = (age is not None) and (age <= CTX_VISION_VALID_SEC)
    corrected = bool((entry or {}).get("ai_corrected"))
    reason = v.get("reason")
    if not entry or not received_ms:
        verdict = "unknown"
    elif not fresh:
        verdict = "expired"
    elif reason == "blocked":
        verdict = "obstructed"
    elif reason == "no_image":
        verdict = "no_frame"
    elif reason == "low_confidence":
        verdict = "uncertain"
    elif reason == "infer_error":
        verdict = "error"
    else:
        verdict = "ok"
    verdict_label = {
        "ok": "画面可用（AI 判为常规场景）",
        "obstructed": "画面不可用（疑似镜头遮挡/过暗）",
        "uncertain": "模型无法确定画面内容（置信度不足）",
        "no_frame": "本次未收到有效画面（空帧/采集失败）",
        "error": "画面无法解码（原图可能已清理）",
        "expired": "没有新画面（历史结论已过有效期 %ds，不作当前结论）" % int(CTX_VISION_VALID_SEC),
        "unknown": "尚无画面证据（还没抓拍过）",
    }[verdict]
    return {
        "verdict": verdict,
        "verdict_label": verdict_label,
        "usable": verdict == "ok",
        "fresh": fresh,
        "age_sec": None if age is None else round(age, 1),
        "valid_sec": CTX_VISION_VALID_SEC,
        "request_id": rid,
        "filename": v.get("filename") or (entry or {}).get("name"),
        "capture_state": v.get("task_state"),
        # ---- 硬件观测（sensor）----
        "gray": v.get("avg_gray"),
        "gray_threshold": BLOCKED_MAX_GRAY,
        "dark_ratio": (entry or {}).get("dark_ratio"),
        "size": (entry or {}).get("size"),
        "size_threshold": BLOCKED_MIN_SIZE,
        "blocked": bool(v.get("blocked")),
        "collected_at": None if not collected_ms else _ms_str(collected_ms),
        "collected_at_ms": collected_ms,
        "collected_at_source": (entry or {}).get("collected_at_source") or "unknown",
        "received_at": None if not received_ms else _ms_str(received_ms),
        "received_at_ms": received_ms,
        "time": _ctx_clock(received_ms),
        # ---- 模型推断（model）----
        "label": v.get("label"),
        "label_origin": CTX_VISION_LABEL_ORIGIN_MANUAL if corrected else "week8-lite-vision 模型",
        "score": v.get("score"),
        "score_is_accuracy": False,       # 硬性约束：未校准分数绝不写成准确率
        "low_conf_threshold": INFER_LOW_CONF,
        "ai_status": "success" if corrected else (entry or {}).get("ai_status"),
        "engine": (entry or {}).get("ai_engine") or INFER_ENGINE,
        "corrected": corrected,
        "alt_label": (v.get("alt") or {}).get("label") if v.get("alt") else None,
        "reason": reason,
        "detail": v.get("detail"),
        "note": "灰度/尺寸/遮挡初判是【硬件观测】；AI 标签与置信度是【模型推断】—— 两者在 UI 上必须分开呈现。",
    }


def _ctx_evidence_items(obs, motion, vision, now):
    """① 第一层：原始证据行。每行 = 来源 + 采集时间 + 观测值 + origin(sensor/model)。

    origin=sensor → 硬件/链路观测；origin=model → 视觉模型推断（带置信度，标注未校准）。
    板上没有的字段一律不编造：没有采样就不出 IMU 行，没有画面就不出灰度行。
    """
    items = []
    lats = motion.get("latest") or {}
    if lats:
        items.append({
            "id": "imu_latest", "layer": "observation", "origin": "sensor",
            "origin_label": "硬件观测", "source": "IMU 加速度计（QMA6100P）",
            "ts_ms": lats.get("received_at_ms"), "time": lats.get("time"),
            "collected_at": lats.get("collected_at"), "received_at": lats.get("received_at"),
            "text": "%s - ACC X=%s Y=%s Z=%s（板端采集 %s）" % (
                lats.get("time") or "--:--:--", lats.get("ax"), lats.get("ay"), lats.get("az"),
                lats.get("collected_at") or "未上报采集时间"),
        })
    thr = motion.get("thresholds") or {}
    if motion.get("level") != "no_data":
        items.append({
            "id": "imu_motion", "layer": "observation", "origin": "sensor",
            "origin_label": "硬件观测",
            "source": "IMU 加速度计（%d 条真实采样推导）" % motion.get("samples", 0),
            "ts_ms": lats.get("received_at_ms"), "time": motion.get("last_time"),
            "text": "%s - 运动量 Δ|a|=%s / 基线 %s = %s（静止 ≤%s%% · 剧烈 >%s%%）→ %s" % (
                motion.get("last_time") or "--:--:--", motion.get("deviation"), motion.get("baseline"),
                ("%.1f%%" % (motion["ratio"] * 100)) if motion.get("ratio") is not None else "--",
                int(thr.get("still_ratio", 0) * 100), int(thr.get("shake_ratio", 0) * 100),
                motion.get("label")),
        })
    if vision.get("gray") is not None:
        items.append({
            "id": "vision_gray", "layer": "observation", "origin": "sensor",
            "origin_label": "硬件观测", "source": "摄像头画面统计（灰度 / 尺寸）",
            "ts_ms": vision.get("received_at_ms"), "time": vision.get("time"),
            "collected_at": vision.get("collected_at"), "received_at": vision.get("received_at"),
            "text": "%s - 画面平均灰度 %s（阈值 <%s → 画面不可用）· 尺寸 %s B（<%s B 视为空帧）%s" % (
                vision.get("time") or "--:--:--", vision.get("gray"), vision.get("gray_threshold"),
                vision.get("size"), vision.get("size_threshold"),
                "→ 遮挡初判成立" if vision.get("blocked") else "（无遮挡初判）"),
        })
    if vision.get("label") or vision.get("verdict") in ("uncertain", "no_frame", "expired"):
        items.append({
            "id": "vision_label", "layer": "observation", "origin": "model",
            "origin_label": "模型推断", "source": "视觉 AI 引擎 %s" % vision.get("engine"),
            "ts_ms": vision.get("received_at_ms"), "time": vision.get("time"),
            "text": "%s - AI 标签「%s」置信度 %s（来源 %s · 未校准分数≠准确率）%s" % (
                vision.get("time") or "--:--:--", vision.get("label") or "—（拒识，未给标签）",
                ("%.2f" % vision["score"]) if vision.get("score") is not None else "--",
                vision.get("label_origin"),
                ("；模型最接近的描述「%s」" % vision["alt_label"]) if vision.get("alt_label") else ""),
        })
    cam = obs.get("camera_state") or {}
    items.append({
        "id": "camera_link", "layer": "observation", "origin": "sensor",
        "origin_label": "硬件观测", "source": "摄像头 / 抓拍链路",
        "ts_ms": cam.get("last_time_ms"), "time": _ctx_clock(cam.get("last_time_ms")),
        "text": "%s - 最近成功画面 %s（%s）· 最近失败 %s%s" % (
            _ctx_clock(cam.get("last_time_ms")) or "--:--:--",
            cam.get("last_time") or "从未",
            "从未收到" if cam.get("age_sec") is None else ("%.1fs 前" % cam["age_sec"]),
            cam.get("last_fail_time") or "无",
            ("（原因 %s）" % cam.get("fail_reason")) if cam.get("fail_reason") else ""),
    })
    dev = obs.get("device_state") or {}
    items.append({
        "id": "network_heartbeat", "layer": "observation", "origin": "sensor",
        "origin_label": "硬件观测", "source": "板端心跳 / HTTP 轮询",
        "ts_ms": dev.get("last_seen_ms"), "time": _ctx_clock(dev.get("last_seen_ms")),
        "text": "%s - 板端心跳 %s（窗口 %ss）→ %s · IP %s · RSSI %s" % (
            _ctx_clock(dev.get("last_seen_ms")) or "--:--:--",
            "从未收到" if dev.get("age_sec") is None else ("%.1fs 前" % dev["age_sec"]),
            dev.get("window_sec"),
            "网络在线" if dev.get("online") else "网络不通/设备离线",
            dev.get("ip") or "--", dev.get("rssi")),
    })
    loc = obs.get("location_state") or {}
    hist = loc.get("historical") or {}
    items.append({
        "id": "location_fix", "layer": "observation", "origin": "sensor",
        "origin_label": "硬件观测", "source": "定位 GNSS",
        "ts_ms": loc.get("collected_at_ms") or loc.get("received_at_ms"),
        "time": _ctx_clock(loc.get("collected_at_ms")),
        "text": ("%s - 本次位置解 %s,%s（采集 %s）" % (
            _ctx_clock(loc.get("collected_at_ms")) or "--:--:--",
            loc.get("latitude"), loc.get("longitude"), loc.get("collected_at")))
        if loc.get("fix") else
        ("%s - 本次无位置解（no_fix，严格为空）；历史位置 %s（仅参考，不代表本次）" % (
            _ctx_clock(loc.get("received_at_ms") or hist.get("collected_at_ms")) or "--:--:--",
            ("%s,%s @ %s" % (hist.get("latitude"), hist.get("longitude"), hist.get("collected_at")))
            if hist.get("available") else "无")),
    })
    return items


# ---- 有限规则表（② 第二层）：每条规则 = 条件(when) → 派生状态(derived) + 严重度 ----
# 规则是【有限且可审计】的：/api/context 会把实际命中的规则整条回传，前端直接展示“为什么这么说”。
CTX_RULES = [
    {"rule_id": "R1", "name": "设备心跳中断",
     "when": "距上次板端心跳 > %.0fs（DEVICE_ONLINE_WINDOW）" % DEVICE_ONLINE_WINDOW,
     "derived": "device_offline", "severity": "warning"},
    {"rule_id": "R2", "name": "板端自报 IMU 故障（唯一允许说“故障”的规则）",
     "when": "imu_state.status == 'fail'", "derived": "imu_hardware_fault", "severity": "warning"},
    {"rule_id": "R3", "name": "运动 + 视野正常（冲突解决：运动 ≠ 危险 ≠ 故障）",
     "when": "IMU 运动 level ∈ {moving, shaking} 且 画面 verdict == 'ok'（证据新鲜）",
     "derived": "device_moving_vision_ok", "severity": "info"},
    {"rule_id": "R4", "name": "运动 + 画面不可用",
     "when": "IMU 运动 level ∈ {moving, shaking} 且 画面 verdict ∈ {obstructed, uncertain, no_frame, error}",
     "derived": "moving_with_unusable_frame", "severity": "notice"},
    {"rule_id": "R5", "name": "仅画面一路不可用（其它源正常）",
     "when": "网络在线 且 IMU 未判运动 且 画面 verdict ∈ {obstructed, uncertain}",
     "derived": "camera_obstructed_but_system_ok", "severity": "notice"},
    {"rule_id": "R6", "name": "定位观测陈旧 / 本次无位置解",
     "when": "location_state.status ∈ {stale, offline, no_data, fail} 或 fix == False",
     "derived": "stale_location", "severity": "notice"},
    {"rule_id": "R7", "name": "网络在线但传感器无新数据",
     "when": "网络在线 且 imu_state.status ∈ {stale, no_data, offline}",
     "derived": "network_ok_but_no_sensor_data", "severity": "notice"},
    {"rule_id": "R8", "name": "各源新鲜且画面正常",
     "when": "网络在线 且 IMU online 且 画面 verdict == 'ok'",
     "derived": "system_ok", "severity": "info"},
    {"rule_id": "R9", "name": "证据不足",
     "when": "没有任何可用的源观测（未抓拍 / 未收到采样 / 未心跳）",
     "derived": "insufficient_evidence", "severity": "info"},
]
CTX_RULE_MAP = {r["rule_id"]: r for r in CTX_RULES}
CTX_SEVERITY_RANK = {"warning": 3, "notice": 2, "info": 1}
CTX_SEVERITY_LABEL = {"warning": "需处理", "notice": "待确认", "info": "信息"}


def _ctx_state(rule_id, status_label, explanation, evidence_ids, confidence, confidence_note,
               not_claimed=None, source_note=None):
    """② 第二层单条派生状态：一律标注 origin=inference（推断），并回指命中规则与证据 id。"""
    rule = CTX_RULE_MAP[rule_id]
    return {
        "id": rule["derived"],
        "rule_id": rule_id,
        "rule_name": rule["name"],
        "rule_when": rule["when"],
        "rule_version": RULE_VERSION,
        "layer": "derived",
        "origin": "inference",
        "origin_label": "系统推断",
        "severity": rule["severity"],
        "severity_label": CTX_SEVERITY_LABEL.get(rule["severity"], rule["severity"]),
        "status_label": status_label,
        "explanation": explanation,
        "evidence_ids": list(evidence_ids),
        "confidence": confidence,                 # high / medium / low（依据新鲜度，不是概率）
        "confidence_note": confidence_note,
        "not_claimed": list(not_claimed or []),   # 明示“本结论【没有】断言什么”
        "source_note": source_note,
    }


def _ctx_ratio_txt(motion):
    return ("%.1f%%" % (motion["ratio"] * 100)) if motion.get("ratio") is not None else "--"


def _ctx_derive(obs, motion, vision, now=None):
    """② 第二层：按【有限规则表 CTX_RULES】把多源观测推导成派生状态。

    * 允许【多条状态同时成立】（例：“设备正在移动”+“定位陈旧”），不做单一状态的粗暴覆盖；
    * 返回 (states, applied_rules, primary)：primary = 严重度最高的一条（同级取先命中的）。
    """
    imu = obs.get("imu_state") or {}
    dev = obs.get("device_state") or {}
    loc = obs.get("location_state") or {}
    level, v = motion.get("level"), vision.get("verdict")
    moving, v_fresh, net_ok = level in ("moving", "shaking"), bool(vision.get("fresh")), bool(dev.get("online"))
    m_num = "Δ|a|=%s / 基线 %s = %s" % (motion.get("deviation"), motion.get("baseline"),
                                         _ctx_ratio_txt(motion))
    v_txt = "%s（阈值：灰度 <%s 或 <%s B 空帧 → 不可用；置信度 <%s → 拒识）" % (
        vision.get("verdict_label"), vision.get("gray_threshold"), vision.get("size_threshold"),
        vision.get("low_conf_threshold"))
    states = []

    if not net_ok:
        age = dev.get("age_sec")
        states.append(_ctx_state(
            "R1", "设备心跳中断（网络不通）",
            "距上次板端心跳%s（窗口 %ss）→ 网络这一路证据已失效。此时其余源显示的都是【历史观测】："
            "IMU 最近采样 %s、最近成功画面 %s —— 历史值只作参考，绝不冒充当前状态。"
            % ("从未收到" if age is None else " %.1fs" % age, dev.get("window_sec"),
               imu.get("last_time") or "从未", (obs.get("camera_state") or {}).get("last_time") or "从未"),
            ["network_heartbeat", "imu_latest", "camera_link"], "high",
            "链路层事实（心跳超窗），无需模型参与判定",
            not_claimed=["不是设备损坏", "未收到心跳期间的数据一律按历史观测量对待"]))
    if imu.get("status") == "fail":
        states.append(_ctx_state(
            "R2", "IMU 板端自报故障",
            "板端明确上报 IMU 故障（%s，自 %s）。这是【板端自己的判断】，不是云端推断 —— "
            "上下文里只有这一条允许出现“故障”字样，其余源的状态不受影响。"
            % (imu.get("fail_detail") or "未给出原因", _ctx_clock(imu.get("fail_since_ms")) or "--:--:--"),
            ["imu_motion", "imu_latest"], "high", "来源为设备自述（板端上报即事实）",
            not_claimed=["不是整机损坏", "不代表摄像头或网络两路异常"]))
    if moving and v == "ok" and v_fresh:
        states.append(_ctx_state(
            "R3", "设备正在移动，但视野正常",
            "IMU 观测到%s（%s，窗口 %s 条真实采样）；同时最近一张画面被 AI 判为常规场景"
            "（标签「%s」置信度 %s，采集时间 %s）。两条证据【并不冲突】："
            "运动只说明设备在动，既不是危险、也不是摄像头故障；画面正常说明视野没有被遮挡。"
            % (motion.get("label"), m_num, motion.get("samples"),
               vision.get("label") or "—",
               ("%.2f" % vision["score"]) if vision.get("score") is not None else "--",
               vision.get("time") or "--:--:--"),
            ["imu_motion", "vision_label", "vision_gray"], "high" if net_ok else "medium",
            "两个源都新鲜时判为高置信（IMU 观测年龄 %ss，画面证据年龄 %ss）"
            % (imu.get("age_sec"), vision.get("age_sec")),
            not_claimed=["不是危险状态", "不是设备故障", "不需要维修或更换硬件"],
            source_note="冲突解决：运动证据与“视野正常”同时成立 → 只描述为“正在移动”，不升级告警"))
    if moving and v_fresh and v in ("obstructed", "uncertain", "no_frame", "error"):
        states.append(_ctx_state(
            "R4", "设备在移动，且画面这一路不可用",
            "IMU 观测到%s（%s）；画面这一路给出不可用结论：%s。"
            "两个源同时异常时，优先按“移动中视角被挡 / 画面未取到”解释，而不是一次性判定整机故障。"
            % (motion.get("label"), m_num, v_txt),
            ["imu_motion", "vision_gray", "vision_label"], "medium",
            "画面证据年龄 %ss（有效期内）；IMU 观测年龄 %ss" % (vision.get("age_sec"), imu.get("age_sec")),
            not_claimed=["不是设备损坏", "画面里具体被什么挡住无法从灰度/特征判断"]))
    if net_ok and level in ("still", "no_data") and v in ("obstructed", "uncertain"):
        states.append(_ctx_state(
            "R5", "摄像头这一路不可用，但系统其余部分正常",
            "网络在线、IMU %s，只有画面这一路给出不可用结论：%s。"
            "这是【单一证据源异常】—— 按“视角 / 光照 / 对焦问题”解释，不包装成“设备损坏”。"
            % ("无运动异常（%s）" % motion.get("label") if level == "still" else "暂无运动证据", v_txt),
            ["vision_gray", "vision_label", "network_heartbeat"] + (["imu_motion"] if level == "still" else []),
            "medium", "画面证据年龄 %ss（来源：%s）" % (vision.get("age_sec"), vision.get("label_origin")),
            not_claimed=["不是设备损坏/硬件故障", "不是整机离线", "画面里有什么物体、被什么挡住均未判定"],
            source_note="示例派生状态：camera_obstructed_but_system_ok（任务书 Week11 用词）"))
    loc_status = loc.get("status")
    if loc_status in ("stale", "offline", "no_data", "fail") or not loc.get("fix"):
        hist = loc.get("historical") or {}
        states.append(_ctx_state(
            "R6", "定位观测陈旧 / 本次无位置解（stale_location）",
            "定位源状态 %s：%s。本次 latitude/longitude 严格为空，界面不得用历史位置冒充当前位置"
            "（历史位置 %s，仅作参考）。"
            % (loc_status or "unknown", loc.get("hint") or "本次无位置解",
               ("%s,%s @ %s" % (hist.get("latitude"), hist.get("longitude"), hist.get("collected_at")))
               if hist.get("available") else "无"),
            ["location_fix"], "high" if loc_status in ("no_data", "fail") else "medium",
            "定位观测年龄 %s（阈值：%ss 陈旧 / %ss 丢失）"
            % ("无（从未有过位置解）" if loc.get("age_sec") is None else "%.1fs" % loc["age_sec"],
               loc.get("stale_threshold_sec"), loc.get("offline_threshold_sec")),
            not_claimed=["当前位置", "设备是否移动过位置（无位置解时无法判断）"]))
    if net_ok and imu.get("status") in ("stale", "no_data", "offline"):
        states.append(_ctx_state(
            "R7", "网络在线，但传感器没有新数据",
            "板端心跳正常（%.1fs 前），但 IMU 状态为 %s（%s）—— 网络连通不等于传感器有数据，"
            "二者必须分开陈述。"
            % (dev.get("age_sec") or 0.0, imu.get("status"), imu.get("hint") or "无新采样"),
            ["network_heartbeat", "imu_latest"], "high",
            "心跳与 IMU 采样是两个独立事实（心跳新鲜、采样陈旧）",
            not_claimed=["不是网络故障", "不是 IMU 硬件故障（除非板端自报 fail）"]))
    if not states:
        if net_ok and imu.get("status") == "online" and v == "ok" and v_fresh:
            states.append(_ctx_state(
                "R8", "各源新鲜且画面正常",
                "网络在线、IMU 数据新鲜（%ss 前）、最近画面被 AI 判为常规场景（置信度 %s）—— 当前无需处理。"
                % (imu.get("age_sec"), ("%.2f" % vision["score"]) if vision.get("score") is not None else "--"),
                ["imu_motion", "vision_label", "network_heartbeat"], "high",
                "三个独立源同时新鲜且结论一致",
                not_claimed=["不代表画面内容一定正确（置信度≠准确率）", "不代表没有未被观测到的风险"]))
        else:
            states.append(_ctx_state(
                "R9", "证据不足，暂不下结论",
                "当前没有可用的源观测：未抓拍到画面 / 未收到 IMU 采样 / 未收到板端心跳 —— "
                "证据不足时只如实陈述“没有数据”，绝不编造状态。",
                ["network_heartbeat"], "low", "缺少观测，任何结论都不可靠",
                not_claimed=["设备状态", "画面内容", "是否在线"]))
    applied = [{"rule_id": s["rule_id"], "name": s["rule_name"], "when": s["rule_when"],
                "derived": s["id"], "severity": s["severity"]} for s in states]
    primary = max(states, key=lambda s: CTX_SEVERITY_RANK.get(s["severity"], 1))
    return states, applied, primary


def _ctx_conflicts(obs, motion, vision, states, now=None):
    """冲突解决（任务书要点②）：把“看起来互相矛盾”的两条证据显式列出，并给出统一口径。

    核心纪律：运动 ≠ 危险 ≠ 故障；“摄像头被挡” ≠ “设备损坏”。
    返回 [{id, between, wording, resolution, resolved_by, claimed_fault, requires_user_input}]。
    """
    imu = obs.get("imu_state") or {}
    dev = obs.get("device_state") or {}
    loc = obs.get("location_state") or {}
    level, v = motion.get("level"), vision.get("verdict")
    ids = {s["id"] for s in states}
    out = []
    if level in ("moving", "shaking") and v == "ok" and vision.get("fresh"):
        out.append({
            "id": "motion_vs_vision",
            "between": ["imu_motion=%s" % level, "vision=%s" % v],
            "wording": "设备正在移动，但视野正常",
            "resolution": "IMU 判运动（Δ|a|/基线 = %s）与“画面正常”是两条独立证据，并不矛盾："
                          "运动只说明设备在动，画面正常说明视野没被挡住 —— 因此既不升级为危险、"
                          "也不判定为故障（规则 R3）。" % _ctx_ratio_txt(motion),
            "resolved_by": "R3",
            "claimed_fault": False,
            "requires_user_input": level == "shaking",
        })
    if not dev.get("online") and (imu.get("status") in ("online", "stale") or vision.get("fresh")):
        out.append({
            "id": "offline_but_fresh",
            "between": ["network_heartbeat=offline", "imu/vision=fresh"],
            "wording": "网络中断，但最后一帧观测仍新鲜",
            "resolution": "心跳已超出 %ss 窗口 → 网络不可达；此时 IMU / 画面显示的是【历史观测】。"
                          "两者同时成立时只按“历史值”呈现，禁止当作当前状态（规则 R1）。"
                          % dev.get("window_sec"),
            "resolved_by": "R1",
            "claimed_fault": False,
            "requires_user_input": False,
        })
    if imu.get("status") == "fail" and v == "ok" and vision.get("fresh"):
        out.append({
            "id": "imu_fault_vs_vision_ok",
            "between": ["imu_state=fail", "vision=ok"],
            "wording": "IMU 报故障，但画面正常",
            "resolution": "故障来自板端自述，只作用于 IMU 这一路；画面与网络两路证据不受影响，"
                          "因此不能说“整机损坏”（规则 R2）。",
            "resolved_by": "R2",
            "claimed_fault": True,
            "requires_user_input": True,
        })
    if "stale_location" in ids and "device_moving_vision_ok" in ids:
        out.append({
            "id": "moving_vs_location",
            "between": ["imu_motion=%s" % level, "location=%s" % (loc.get("status") or "unknown"),
                        "vision=%s" % v],
            "wording": "设备在移动、视野正常，但当前位置未知",
            "resolution": "移动与“视野正常”不冲突；位置未知是定位这一路没有新解，与设备是否损坏无关"
                          " —— 只陈述“无位置解”，不用历史位置顶替（规则 R6）。",
            "resolved_by": "R6",
            "claimed_fault": False,
            "requires_user_input": False,
        })
    return out


# ---- ③ 第三层：交互追问（选择题）状态：记录“问过几次 / 用户怎么答的”，防重复打扰 ----
context_lock = threading.Lock()
context_answers = deque(maxlen=CTX_ANSWER_KEEP)     # 最近 N 次用户澄清（可追溯“是谁确认的”）
context_quest_state = {}                            # question_id -> {asked_count, last_ask_ms, choice, choice_ms, snooze_until_ms}


def _ctx_question(question_id, kind, question, because, evidence_ids, options,
                  request_id=None, filename=None):
    """③ 第三层单条追问：一定是【选择题】（用户一次点击即可消解歧义），并绑定证据与对象。"""
    return {
        "question_id": question_id,
        "kind": kind,
        "layer": "interaction",
        "question": question,
        "because": because,
        "evidence_ids": list(evidence_ids),
        "options": [dict(o) for o in options],
        "request_id": request_id,
        "filename": filename,
        "answer_endpoint": "/api/context_answer",
        "answer_body": {"question_id": question_id, "choice": "<options[].value>", "request_id": request_id},
        "rule_version": RULE_VERSION,
    }


def _ctx_questions(obs, motion, vision, states, conflicts, now=None):
    """③ 第三层：只在【存在歧义】时才追问；已作答 / 已 snooze / 超额的会被压制成 ask=False
    （但依然回传，前端能看到“为什么这次没再问”，与 week9 的冷却口径一致）。"""
    now_ms = int((time.time() if now is None else now) * 1000)
    dev = obs.get("device_state") or {}
    loc = obs.get("location_state") or {}
    ids = {s["id"] for s in states}
    level, v = motion.get("level"), vision.get("verdict")
    qs = []
    # ① 遮挡歧义（任务书给定示例）
    if v in ("obstructed", "uncertain") and vision.get("fresh"):
        qs.append(_ctx_question(
            "q_occlusion_%s" % (vision.get("filename") or vision.get("request_id") or "latest"),
            "occlusion",
            "摄像头似乎被挡住了，是您有意遮挡还是需要重新抓拍？",
            "画面这一路给出不可用结论：%s。但“被挡住/过暗”可能是有意遮挡，也可能只是光照、对焦或视角问题 —— "
            "灰度与像素特征无法区分，只有您能确认（规则 R5/R4）。" % vision.get("verdict_label"),
            ["vision_gray", "vision_label"],
            [
                {"value": "intentional_occlusion", "label": "🙈 是我有意遮挡的", "effect": "note_only",
                 "description": "记录为“人工确认的有意遮挡”：不再追问，也不再建议重拍"},
                {"value": "need_retake", "label": "📷 需要重新抓拍", "effect": "retake",
                 "description": "立即发起一次新采集（retake_of = 本次 request_id，可逐条溯源）"},
                {"value": "not_sure", "label": "🤔 我先检查一下", "effect": "snooze",
                 "description": "%d 秒内不再追问（黄条与徽标照旧显示，不打扰）" % int(CTX_SNOOZE_SEC)},
            ],
            request_id=vision.get("request_id"), filename=vision.get("filename")))
    # ② 运动歧义：运动量只有“多大”，没有“为什么”
    if level in ("moving", "shaking"):
        qs.append(_ctx_question(
            "q_motion_none", "motion",
            "检测到设备%s（Δ|a|/基线 = %s），是您在有意移动设备吗？"
            % (motion.get("label"), _ctx_ratio_txt(motion)),
            "IMU 只告诉我们运动量，无法识别主体与原因：可能是有人搬动，也可能是桌面震动或无意碰撞"
            "（规则 R3）。画面结论与它无关，绝不会因此判定为故障。",
            ["imu_motion"],
            [
                {"value": "intentional_move", "label": "🚶 是我在有意搬动/移动", "effect": "note_only",
                 "description": "记录为“人工确认的移动”，后续动作不受影响"},
                {"value": "accidental_bump", "label": "💥 无意碰撞/放置抖动", "effect": "note_only",
                 "description": "记录为“非预期扰动”，建议下一次评估时让设备静止"},
                {"value": "not_sure", "label": "🤔 不确定", "effect": "snooze",
                 "description": "%d 秒内不再追问" % int(CTX_SNOOZE_SEC)},
            ]))
    # ③ 定位歧义：这次到底有没有位置数据
    if "stale_location" in ids:
        qs.append(_ctx_question(
            "q_location_none", "location",
            "本次没有可用的位置数据（定位状态 %s）。是否把这次记录标记为“本次无位置”？"
            % (loc.get("status") or "unknown"),
            "定位这一路：%s。历史位置只作参考，界面不会用它冒充本次位置（规则 R6）。"
            % (loc.get("hint") or "无新定位观测"),
            ["location_fix"],
            [
                {"value": "accept_no_fix", "label": "📍 确认“本次无位置”", "effect": "note_only",
                 "description": "记录为“用户已确认本次无位置”，不再追问"},
                {"value": "retry_location", "label": "📡 等下次采集再上报", "effect": "retry_location",
                 "description": "等待下一次抓拍时由板端重新上报定位（绝不使用历史位置顶替）"},
                {"value": "not_sure", "label": "🤔 稍后再说", "effect": "snooze",
                 "description": "%d 秒内不再追问" % int(CTX_SNOOZE_SEC)},
            ]))
    # ④ 心跳中断：只能确认“收不到心跳”，不能断言断电
    if "device_offline" in ids:
        qs.append(_ctx_question(
            "q_device_offline_none", "device_offline",
            "已 %s 未收到板端心跳，需要我做什么吗？" % (
                "从未收到心跳" if dev.get("age_sec") is None else ("%.1f 秒" % dev["age_sec"])),
            "心跳中断只说明【收不到心跳】（网络不可达 / 板端未工作），不能断言设备断电或损坏（规则 R1）。",
            ["network_heartbeat"],
            [
                {"value": "check_device", "label": "🔌 我去检查电源与网络", "effect": "note_only",
                 "description": "记录为“人工去核查”，入站数据一到即自动恢复正常"},
                {"value": "retry_later", "label": "⏳ 稍后自动重试", "effect": "snooze",
                 "description": "%d 秒内不再追问（心跳恢复后告警会立刻消失）" % int(CTX_SNOOZE_SEC)},
                {"value": "not_sure", "label": "🤔 不确定", "effect": "snooze",
                 "description": "%d 秒内不再追问" % int(CTX_SNOOZE_SEC)},
            ]))
    return _ctx_question_gate(qs, now_ms)


def _ctx_question_gate(qs, now_ms):
    """追问闸门（防打扰）：已作答(CTX_ANSWER_SILENCE_SEC) / 已 snooze / 超出 CTX_MAX_QUESTIONS 的
    一律压制成 ask=False，但条目依然回传 —— 前端能看到“这次为什么没再问”。

    week12 增加一层【用户偏好】闸门：偏好 never_ask（关闭主动提醒）时，所有追问一律 ask=False
    （依然回传问题与 suppressed_reason：只展示、不打扰，绝不静默吞掉歧义）。

    返回 (questions, answers)：answers = 最近 CTX_ANSWER_KEEP 次用户澄清记录。
    """
    with context_lock:
        st = {k: dict(x) for k, x in context_quest_state.items()}
        answers = [dict(a) for a in context_answers]
    pref = pref_current()                  # week12：追问之前同样先查偏好（唯一权威在后端）
    pref_off = not pref["popup"]
    pref_why = "用户偏好「关闭主动提醒」（%s）→ 只展示不追问" % _pref_tag(pref)
    out, rank = [], 0
    for q in qs:
        rec = st.get(q["question_id"]) or {}
        asked_n, last_ask = int(rec.get("asked_count") or 0), rec.get("last_ask_ms")
        if pref_off:
            ask, why = False, pref_why
        elif rec.get("choice") and rec.get("choice_ms") \
                and (now_ms - int(rec["choice_ms"])) < int(CTX_ANSWER_SILENCE_SEC * 1000):
            left = int((CTX_ANSWER_SILENCE_SEC * 1000 - (now_ms - int(rec["choice_ms"]))) / 1000)
            ask, why = False, "已作答「%s」（%d 秒内不再追问）" % (rec.get("choice_label") or rec["choice"], left)
        elif rec.get("snooze_until_ms") and int(rec["snooze_until_ms"]) > now_ms:
            ask, why = False, "用户选择稍后再问（剩余 %.0f 秒）" % ((int(rec["snooze_until_ms"]) - now_ms) / 1000.0)
        elif rank >= CTX_MAX_QUESTIONS:
            ask, why = False, "本次最多同时追问 %d 条，该问题排队等待" % CTX_MAX_QUESTIONS
        else:
            ask, why = True, None
        if ask and (not last_ask or (now_ms - int(last_ask)) >= int(CTX_QUESTION_MIN_INTERVAL * 1000)):
            last_ask, asked_n = now_ms, asked_n + 1
            with context_lock:
                st2 = context_quest_state.setdefault(q["question_id"], {})
                st2["last_ask_ms"], st2["asked_count"] = last_ask, asked_n
        if ask:
            rank += 1
        q.update({
            "ask": ask,
            "suppressed_reason": why,
            "asked_count": asked_n,
            "last_asked_at": _ctx_clock(last_ask) if last_ask else None,
            "answered_choice": rec.get("choice"),
            "answered_choice_label": rec.get("choice_label"),
            "answered_at": _ctx_clock(rec["choice_ms"]) if rec.get("choice_ms") else None,
            "dedup_key": "%s|%s" % (q["question_id"], (q["because"] or "")[:24]),
        })
        out.append(q)
    out.sort(key=lambda x: (not x["ask"], x["kind"]))     # 需要用户回答的排在最前面
    return out, answers


def _ctx_fact(fid, text, origin, evidence_ids, ts_ms=None, rule_id=None, severity=None):
    """③ 第三层“已知事实”单条：一句人话 + origin（谁说的）+ 可回溯的证据 id 与时间。"""
    return {
        "id": fid, "text": text, "origin": origin,
        "origin_label": {"sensor": "硬件观测", "model": "模型推断", "inference": "系统推断"}.get(origin, origin),
        "evidence_ids": list(evidence_ids),
        "time": _ctx_clock(ts_ms), "ts_ms": ts_ms,
        "rule_id": rule_id, "severity": severity,
    }


def _ctx_unknowns(obs, motion, vision, states):
    """③ 第三层“待确认信息”：把【本次没有被观测到】的东西明确列出来，避免用户以为是已知的。"""
    imu = obs.get("imu_state") or {}
    loc = obs.get("location_state") or {}
    ids = {s["id"] for s in states}
    unknown = []
    if motion.get("level") in ("moving", "shaking"):
        unknown.append("谁在移动设备（IMU 只给运动量，不识别主体，也不解释原因）")
    if vision.get("verdict") in ("obstructed", "uncertain", "no_frame", "error"):
        unknown.append("画面里被什么挡住 / 画面内容（灰度与像素特征都无法判定）")
    if "stale_location" in ids or not loc.get("fix"):
        unknown.append("本次设备位置（本次无位置解；历史位置只是参考，不代表本次）")
    if "device_offline" in ids:
        unknown.append("板端是否真的断电（只能确认“收不到心跳”）")
    if imu.get("status") == "fail":
        unknown.append("IMU 故障的具体成因（仅板端自述，云端无法进一步定位）")
    if not unknown:
        unknown.append("是否还有未被当前传感器覆盖的情况（各源结论一致，但不等于没有被观测到的风险）")
    return unknown


def _ctx_known_facts(obs, motion, vision, states):
    """③ 第三层：已知事实 = 硬件观测 + 模型推断 + 系统推断，三类分开标注、逐条可回溯。"""
    imu = obs.get("imu_state") or {}
    dev = obs.get("device_state") or {}
    loc = obs.get("location_state") or {}
    facts = []
    # ---- 硬件/链路观测（sensor）----
    if motion.get("level") == "no_data":
        facts.append(_ctx_fact("fact_motion", "IMU：还没有可用的真实采样，运动程度不下结论", "sensor",
                               ["imu_motion"]))
    else:
        facts.append(_ctx_fact("fact_motion", "IMU：%s（Δ|a|/基线 = %s，窗口 %s 条真实采样）" % (
            motion.get("label"), _ctx_ratio_txt(motion), motion.get("samples")), "sensor", ["imu_motion"],
            (motion.get("latest") or {}).get("received_at_ms")))
    if vision.get("verdict") == "unknown":
        facts.append(_ctx_fact("fact_gray", "画面：本次没有任何画面证据（未抓拍 / 未入库）", "sensor",
                               ["vision_gray"]))
    else:
        facts.append(_ctx_fact(
            "fact_gray", "画面（硬件观测）：平均灰度 %s（阈值 <%s 判遮挡）、尺寸 %s B（阈值 <%s）、采集源 %s" % (
                vision.get("gray"), vision.get("gray_threshold"), vision.get("size"),
                vision.get("size_threshold"), vision.get("collected_at_source")),
            "sensor", ["vision_gray"], vision.get("collected_at_ms")))
    facts.append(_ctx_fact("fact_net", "链路：板端心跳%s（窗口 %ss）" % (
        ("从未收到" if dev.get("age_sec") is None else "%.1fs 前，%s" % (
            dev["age_sec"], "在线" if dev.get("online") else "已超出窗口判定为不通")), dev.get("window_sec")),
        "sensor", ["network_heartbeat"], dev.get("last_seen_ms")))
    facts.append(_ctx_fact("fact_loc", "定位：状态 %s，本次位置 %s" % (
        loc.get("status") or "unknown",
        ("%s, %s" % (loc.get("latitude"), loc.get("longitude"))) if loc.get("fix") else "无位置解（不使用历史位置）"),
        "sensor", ["location_fix"], loc.get("collected_at_ms")))
    # ---- 模型推断（model）----
    if vision.get("label"):
        facts.append(_ctx_fact("fact_label", "模型（%s）：标签「%s」，分数 %s（未校准，不等于准确率）" % (
            vision.get("label_origin"), vision.get("label"),
            ("%.2f" % vision["score"]) if vision.get("score") is not None else "--"),
            "model", ["vision_label"], vision.get("received_at_ms")))
    else:
        facts.append(_ctx_fact("fact_label", "模型：本次没有可用标签（%s）" % (
            vision.get("detail") or vision.get("verdict_label")), "model", ["vision_label"]))
    # ---- 系统推断（inference）：原样带 rule_id，前端一眼看到“结论出自哪条规则”----
    for s in states:
        facts.append(_ctx_fact("fact_" + s["id"], "【推断 %s】%s" % (s["rule_id"], s["status_label"]),
                               "inference", s["evidence_ids"], rule_id=s["rule_id"], severity=s["severity"]))
    return {"items": facts, "count": len(facts),
            "counts": {"sensor": len([f for f in facts if f["origin"] == "sensor"]),
                       "model": len([f for f in facts if f["origin"] == "model"]),
                       "inference": len([f for f in facts if f["origin"] == "inference"])},
            "note": "已知事实按来源分层：硬件观测 / 模型推断 / 系统推断 —— 三者不得混装成一句结论。"}


def _ctx_policy():
    """规则参数与规则表：/api/context.policy 的统一出口（前端一律从这里取，绝不写死阈值）。"""
    return {
        "rule_version": RULE_VERSION,
        "evidence_layers": ["observations", "derived_states", "interaction_context"],
        "motion": {
            "window_samples": CTX_MOTION_WINDOW_SAMPLES,
            "still_ratio": CTX_MOTION_STILL_RATIO,
            "shake_ratio": CTX_MOTION_SHAKE_RATIO,
            "min_samples": CTX_MOTION_MIN_SAMPLES,
            "scope": "只统计 source=device 的真实采样；SIM 预热点不参与（避免把预热点当成“设备在动”）",
            "metric": "Δ|a| / median(|a|)：无量纲，兼容板端 g 与 m/s² 两种单位",
            "note": "运动量只说明“设备在动”，不说明是谁在动、也不代表危险或故障。",
        },
        "vision_evidence": {
            "valid_sec": CTX_VISION_VALID_SEC,
            "gray_block_max": BLOCKED_MAX_GRAY,
            "empty_frame_bytes": BLOCKED_MIN_SIZE,
            "low_conf": INFER_LOW_CONF,
            "engine": INFER_ENGINE,
            "score_is_accuracy": False,
            "note": "灰度/尺寸/遮挡初判是硬件观测；标签与分数是模型推断（未校准，≠ 准确率）。",
        },
        "heartbeat": {"window_sec": DEVICE_ONLINE_WINDOW,
                      "note": "网络在线 ≠ 传感器有数据，二者分开陈述（规则 R7）。"},
        "imu_freshness": {"stale_sec": IMU_STALE_SECONDS, "offline_sec": IMU_OFFLINE_SECONDS},
        "location": {"stale_sec": LOCATION_STALE_SECONDS, "offline_sec": LOCATION_OFFLINE_SECONDS,
                     "note": "本次无位置解时严格给 null，历史位置只作参考（规则 R6）。"},
        "questions": {
            "answer_silence_sec": CTX_ANSWER_SILENCE_SEC,
            "snooze_sec": CTX_SNOOZE_SEC,
            "min_interval_sec": CTX_QUESTION_MIN_INTERVAL,
            "max_questions": CTX_MAX_QUESTIONS,
            "answer_keep": CTX_ANSWER_KEEP,
            "style": "只问选择题（一次点击即可消解歧义）；选项 effect ∈ {note_only, retake, retry_location, snooze}",
            "note": "已作答 / 已 snooze / 超出上限的追问会被压制，但仍回传 ask=False + suppressed_reason。",
            "preference": pref_current(),          # week12: 追问的第 0 层闸门（用户偏好）
            "preference_note": "week12：never_ask ⇒ 所有追问 ask=False（只展示、不打扰）；其余偏好照旧受静默期与去重约束。",
        },
        "severity_rank": CTX_SEVERITY_RANK,
        "severity_label": CTX_SEVERITY_LABEL,
        "must_not_claim": [
            "摄像头遮挡/画面不可用 ⇒ 不得说成“设备损坏 / 硬件故障”（规则 R5）",
            "只有板端自报 IMU fail 时才允许出现“故障”字样（规则 R2）",
            "AI 置信度不是准确率（score_is_accuracy = False）",
            "运动量不说明主体与原因，也不代表危险（规则 R3）",
            "网络在线 ≠ 传感器有数据（规则 R7）",
            "历史值（位置 / 画面结论 / 采样）不得冒充当前状态",
        ],
        "rules": [{"rule_id": r["rule_id"], "name": r["name"], "when": r["when"], "derived": r["derived"],
                   "severity": r["severity"], "severity_label": CTX_SEVERITY_LABEL.get(r["severity"])}
                  for r in CTX_RULES],
    }


def _ctx_summary(primary, states, motion, vision, conflicts):
    """③ 交互上下文的一句话总结：主结论 → 运动/画面两路口径 → 其他同时成立的状态 → 冲突解决。"""
    parts = ["主结论：%s（规则 %s · %s）" % (primary["status_label"], primary["rule_id"], primary["severity_label"])]
    parts.append("IMU 运动口径：%s" % motion.get("label"))
    parts.append("画面口径：%s" % vision.get("verdict_label"))
    others = [s["status_label"] for s in states if s["id"] != primary["id"]]
    if others:
        parts.append("同时成立的其他状态：%s" % "；".join(others))
    if conflicts:
        parts.append("冲突解决：%s" % "；".join(c["wording"] for c in conflicts))
    return "。".join(parts) + "。"


def _ctx_build(request_id=None, filename=None, now=None):
    """三层上下文快照（/api/context 与 /api/context_answer 共用同一份口径，避免两处规则跑偏）。

    * 默认绑定【最近一次采集】；也可指定 request_id / filename 精确到某一次（可溯源）；
    * 只读：本函数不修改任何传感器状态，只汇总（追问计数在 _ctx_question_gate 内维护）。
    """
    now = time.time() if now is None else now
    obs = _current_observation(now)
    motion = _imu_motion(now)
    vision = _vision_evidence(request_id=request_id, filename=filename, now=now)
    items = _ctx_evidence_items(obs, motion, vision, now)
    states, applied, primary = _ctx_derive(obs, motion, vision, now)
    conflicts = _ctx_conflicts(obs, motion, vision, states, now)
    questions, answers = _ctx_questions(obs, motion, vision, states, conflicts, now)
    facts = _ctx_known_facts(obs, motion, vision, states)
    unknown = _ctx_unknowns(obs, motion, vision, states)
    askable = [q for q in questions if q["ask"]]
    ms = int(now * 1000)
    return {
        "status": "ok",
        "rule_version": RULE_VERSION,
        "generated_at": _ms_str(ms),
        "generated_at_ms": ms,
        "server_time": _ms_str(ms),
        "topic": {
            "request_id": vision.get("request_id") or request_id,
            "filename": vision.get("filename") or filename,
            "bound_to_latest": not (request_id or filename),
            "note": "上下文默认绑定最近一次采集；可用 ?request_id= / ?filename= 精确指定某一次（week11 可溯源要求）。",
        },
        "layers": [
            {"key": "observations", "title": "① 原始证据",
             "desc": "硬件观测与模型推断分开列：每行 = 来源 + 采集/接收时间 + 观测值 + origin"},
            {"key": "derived_states", "title": "② 系统推理",
             "desc": "按有限规则表（R1~R9）推导：每条都回指 rule_id 与 evidence_ids，可审计"},
            {"key": "interaction_context", "title": "③ 交互上下文",
             "desc": "已知事实（分层标注）+ 待确认信息 + 歧义追问（选择题，可 snooze）"},
        ],
        "observations": {
            "items": items,
            "count": len(items),
            "counts": {"sensor": len([i for i in items if i["origin"] == "sensor"]),
                       "model": len([i for i in items if i["origin"] == "model"])},
            "imu_motion": motion,
            "vision": vision,
            "imu_state": obs.get("imu_state"),
            "camera_state": obs.get("camera_state"),
            "location_state": obs.get("location_state"),
            "device_state": obs.get("device_state"),
            "freshness_policy": obs.get("policy"),
            "sensor_role_hint": obs.get("network_but_no_data"),
            "note": "本层只陈述“谁、在什么时刻、观测到什么”；AI 标签属模型推断，必须标 origin=model。",
        },
        "derived_states": {
            "items": states,
            "count": len(states),
            "applied_rules": applied,
            "primary": primary,
            "primary_id": primary["id"],
            "primary_label": primary["status_label"],
            "primary_severity": primary["severity"],
            "primary_severity_label": primary["severity_label"],
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "rule_version": RULE_VERSION,
            "note": "派生状态一律 origin=inference；允许多条同时成立，不做单一状态覆盖；severity 只表达“是否需要人处理”，不是危险等级。",
        },
        "interaction_context": {
            "headline": primary["status_label"],
            "headline_id": primary["id"],
            "headline_severity": primary["severity"],
            "headline_severity_label": primary["severity_label"],
            "summary": _ctx_summary(primary, states, motion, vision, conflicts),
            "known_facts": facts,
            "pending_confirmation": unknown,
            "questions": questions,
            "askable_count": len(askable),
            "answers": answers,
            "answer_count": len(answers),
            "preference": pref_current(),                 # week12: 这份快照是在哪份偏好下生成的（可追溯）
            "preference_note": "追问闸门第 0 层就是用户偏好：never_ask ⇒ 所有追问 ask=False（只展示、不打扰）。",
            "must_not_claim": [
                "摄像头遮挡/画面不可用 ⇒ 不是设备损坏（只说“画面这一路不可用，疑似遮挡”）",
                "运动 ⇒ 不是危险、不是故障（运动量只说明设备在动）",
                "AI 置信度 ⇒ 不是准确率",
                "网络在线 ⇒ 不等于传感器有数据",
                "历史观测（位置 / 画面结论 / 采样）⇒ 不等于当前状态",
            ],
            "note": "只问选择题；已作答 / 已静默 / 超过上限的追问会被压制成 ask=False，但依然回传并可解释原因。",
        },
        "policy": _ctx_policy(),
    }


def record_context_answer(question_id, choice, note=None, request_id=None, filename=None):
    """登记用户对追问的回答（唯一入口；沿用 week9 纪律：回答必须绑定【当前仍待确认】的追问）。

    返回 dict：status ∈ {ok, expired, bad_choice}；ok 时附 next_action 供前端执行（重拍等）。
    """
    snap = _ctx_build(request_id=request_id, filename=filename)
    qs = (snap.get("interaction_context") or {}).get("questions") or []
    q = next((x for x in qs if x.get("question_id") == question_id), None)
    if q is None:
        return {"status": "expired", "question_id": question_id, "questions": qs,
                "msg": "该追问在当前上下文中已不存在（证据已变化）—— 请以最新上下文为准，本次回答不记录。",
                "rule_version": RULE_VERSION}
    if q.get("answered_choice"):
        return {"status": "expired", "question_id": question_id, "questions": qs,
                "msg": "该追问已作答（%s）—— 不重复记录。" % (q.get("answered_choice_label") or q["answered_choice"]),
                "rule_version": RULE_VERSION}
    opt = next((o for o in (q.get("options") or []) if o.get("value") == choice), None)
    if opt is None:
        return {"status": "bad_choice", "question_id": question_id, "questions": qs,
                "msg": "选项非法：%s（合法选项 %s）" % (choice, [o["value"] for o in (q.get("options") or [])]),
                "rule_version": RULE_VERSION}
    now_ms = int(time.time() * 1000)
    rec = {"question_id": question_id, "kind": q.get("kind"), "question": q.get("question"),
           "choice": choice, "choice_label": opt.get("label"), "effect": opt.get("effect"),
           "note": note, "request_id": q.get("request_id"), "filename": q.get("filename"),
           "answered_at": _ms_str(now_ms), "answered_at_ms": now_ms, "rule_version": RULE_VERSION}
    with context_lock:
        st = context_quest_state.setdefault(question_id, {})
        st["choice"], st["choice_ms"], st["choice_label"] = choice, now_ms, opt.get("label")
        st["asked_count"] = st.get("asked_count") or 0
        if opt.get("effect") == "snooze":
            st["snooze_until_ms"] = now_ms + int(CTX_SNOOZE_SEC * 1000)
        context_answers.append(rec)
    # week12: 记住用户这次澄清了什么（写进决策日志；与 week9 的动作同属“用户偏好/记忆”一条时间线）
    _eff_note = {"snooze": "%d 秒内不再追问该问题" % int(CTX_SNOOZE_SEC),
                 "retake": "需要重拍这张画面", "retry_location": "等下一次采集重新上报定位",
                 "note_only": "已记录为已知事实（不再追问）"}
    pref_remember("answered_question",
                  "用户回答了追问「%s」→ 系统记住：%s" % (q.get("question"), opt.get("label")),
                  detail="effect=%s（%s）· request_id=%s"
                         % (opt.get("effect"), _eff_note.get(opt.get("effect"), "已记录"),
                            q.get("request_id") or "-"),
                  request_id=q.get("request_id"), save=(opt.get("effect") != "snooze"))
    if opt.get("effect") == "snooze":
        pref_remember("snoozed",
                      "系统已记住：用户选择“稍后再说”（%d 秒内不再追问该问题）" % int(CTX_SNOOZE_SEC),
                      detail="question_id=%s" % question_id, request_id=q.get("request_id"), save=True)
    entry = ledger_find_any(request_id=q.get("request_id"), filename=q.get("filename")) \
        if (q.get("request_id") or q.get("filename")) else None
    if entry:
        # 只给已有记录【追加字段】（ledger_save_ai 语义：绝不新建记录）
        ledger_save_ai(entry, ctx_question_id=question_id, ctx_question=q.get("question"),
                       ctx_choice=choice, ctx_choice_label=opt.get("label"), ctx_effect=opt.get("effect"),
                       ctx_note=note, ctx_answered_at=_ms_str(now_ms), ctx_answered_at_ms=now_ms,
                       ctx_rule_version=RULE_VERSION)
    next_action = None
    if opt.get("effect") == "retake":
        next_action = {"type": "retake", "command": "triggerCapture",
                       "request_id": q.get("request_id"), "filename": q.get("filename"),
                       "note": "前端发起一次新采集（retake_of = 原 request_id），不复用旧画面、不覆盖旧记录"}
    elif opt.get("effect") == "retry_location":
        next_action = {"type": "retry_location", "command": None,
                       "note": "无需前端动作：下一次采集时由板端重新上报定位；本次仍严格显示“无位置解”"}
    print(f"【上下文追问】{_ctx_clock(now_ms)} {question_id} → {choice}（{opt.get('label')}，effect={opt.get('effect')}）")
    return {"status": "ok", "question_id": question_id, "choice": choice, "choice_label": opt.get("label"),
            "effect": opt.get("effect"), "answered_at": _ms_str(now_ms), "answered_at_ms": now_ms,
            "next_action": next_action, "answer": rec, "ledger_updated": bool(entry),
            "msg": "已记录用户确认：%s" % opt.get("label"), "rule_version": RULE_VERSION,
            "context": _ctx_build(request_id=request_id, filename=filename)}


@app.route("/api/context")
def api_context():
    """week11: 三层上下文快照（只读）——原始证据 / 派生状态 / 交互上下文 + 规则参数 policy。

    默认绑定最近一次采集；?request_id= 或 ?filename= 可精确指定某一次（可溯源）。
    """
    return jsonify(_ctx_build(request_id=request.args.get("request_id") or None,
                              filename=request.args.get("filename") or None))


@app.route("/api/context_answer", methods=["POST"])
def api_context_answer():
    """week11: 登记用户对追问的回答（选择题）。过期/非法 ⇒ 409/400，绝不“记一次假回答”。"""
    data = request.get_json(silent=True) or request.form or {}
    qid = (data.get("question_id") or "").strip()
    choice = (data.get("choice") or "").strip()
    if not qid or not choice:
        return jsonify({"status": "bad_request", "rule_version": RULE_VERSION,
                        "msg": "question_id 与 choice 均为必填（追问只接受选择题答案）"}), 400
    res = record_context_answer(
        qid, choice, note=data.get("note"),
        request_id=data.get("request_id") or request.args.get("request_id"),
        filename=data.get("filename") or request.args.get("filename"))
    code = 200 if res["status"] == "ok" else (409 if res["status"] == "expired" else 400)
    return jsonify(res), code


# ================== week12: 用户交互提醒偏好（记住用户选择 · 版本化 · 可回退） ==================
# 任务书要点与本文件的口径：
#   * 偏好 = 用户对“何时可以打扰我”的显式选择，不是模型参数 → 单独持久化（server/preferences.json）、
#     单独版本化；（台账 photos_ledger.json 是“一次采集一条”的记录，放全局配置会让 week7/8/10/11 的
#     台账消费者多出特判，故不整合进台账）
#   * 每次【内容变化】都会产生一个新的 config_version（v2、v3…），历史版本一律保留 → 既能“撤回上一次修改”，
#     也能“回退到任意历史版本”；
#   * v1 永远是【系统默认配置】= low_conf_only：点“恢复默认” = 偏好回到 low_conf_only 且 config_version 重置为 v1；
#   * 后端唯一权威：/api/prompt_check 在【弹窗之前】必须先查这份偏好 —— never_ask ⇒ 只亮黄条 + 后台记录，绝不弹窗；
#   * 与既有防打扰策略的关系写死在 payload.effect 里：never_ask 连 week11 追问也一并关闭（只展示不追问），
#     always_ask 不受 week9 的 30s 静默期与“同一张画面去重”限制（用户明确要求“总提醒”）。
PREF_FILE = os.path.join(_BASE_DIR, 'preferences.json')   # 独立配置文件（原子写入，重启不丢）
PREF_VERSION_DEFAULT = "v1"                               # 默认配置的版本号（“恢复默认”回到这里）
PREF_DEFAULT = "low_conf_only"                            # 默认偏好：仅在置信度极低时提醒
PREF_KEEP_EVENTS = 80                                     # 决策日志持久化上限
PREF_SHOW_EVENTS = 24                                     # 面板一次展示多少条
PREF_KEEP_HISTORY = 30                                    # 版本历史保留条数

# 三个选项：popup=False 表示“只亮黄条 + 后台记录”，而【不是】“不再检测”。
PREF_CHOICES = {
    "always_ask": {
        "value": "always_ask", "label": "🔔 总是提醒",
        "short": "总是提醒（不受静默期限制）",
        "desc": "只要判定“画面不可用”（遮挡 / 置信度极低 / 空帧）就弹窗询问是否重拍；"
                "不受“用户刚点忽略”的 30s 静默期与同一张画面去重限制（用户明确要求总提醒）。",
        "popup": True, "respect_cooldown": False,
    },
    "low_conf_only": {
        "value": "low_conf_only", "label": "🟡 仅在置信度极低时提醒（默认）",
        "short": "仅在置信度极低时提醒",
        "desc": "只在“真的看不清画面”时才弹窗（疑似遮挡 / 置信度 < 0.4 拒识 / 空帧 / 解码失败）；"
                "week9 的静默期与同一张画面去重照旧生效 —— 不做多余打扰。",
        "popup": True, "respect_cooldown": True,
    },
    "never_ask": {
        "value": "never_ask", "label": "🔕 关闭主动提醒（只亮黄条）",
        "short": "关闭主动提醒",
        "desc": "任何情况下都不弹窗，也不发起 week11 追问；判定结果照旧展示（黄条 + 徽章 + 后台记录），"
                "并保留“这次为什么不打扰”的原因，绝不静默吞掉结论。",
        "popup": False, "respect_cooldown": False,
    },
}
PREF_ORDER = ["always_ask", "low_conf_only", "never_ask"]
PREF_MEMORY_KEYS = [
    ("saved", "保存偏好次数"),
    ("restore_default", "恢复默认次数"),
    ("undo", "撤回上一次修改次数"),
    ("rollback", "回退到历史版本次数"),
    ("confirmed_retake", "用户确认重拍"),
    ("ignored_retake", "用户忽略重拍建议"),
    ("corrected_label", "人工纠正 AI 标签"),
    ("answered_question", "回答 week11 追问"),
    ("snoozed", "选择“稍后再说”"),
    ("ask", "实际弹窗次数"),
    ("blocked_by_pref", "因偏好被压制的打扰"),
]
PREF_SOURCE_LABEL = {"default": "系统默认", "user": "用户保存", "restore_default": "恢复默认",
                     "undo": "撤回上一次修改", "rollback": "回退历史版本"}

pref_lock = threading.Lock()
pref_state = {
    "config_version": PREF_VERSION_DEFAULT,
    "preference": PREF_DEFAULT,
    "source": "default",
    "updated_at": None,
    "updated_at_ms": None,
    "note": "系统默认配置（v1）：仅在置信度极低时提醒",
    "rev": 0,                 # 修改序号（单调递增；日志与历史按它排序）
    "history": [],            # [{"rev","config_version","preference","previous_*","changed_at","source","note"}]
    "events": [],             # 决策日志（用户修改 / 触发判定 / 查偏好 / 决定 / 用户动作）
    "memory": {},             # 见 PREF_MEMORY_KEYS
    "last_decision": None,    # 最近一次“要不要打扰”的完整链路（含 chain，可逐条回放）
    "writes": 0,              # 落盘次数（持久化可见性，供面板显示）
}


def pref_current():
    """决策前必查的最小快照（每次现读，绝不用缓存值判断“要不要打扰用户”）。"""
    with pref_lock:
        v = pref_state["preference"]
        c = PREF_CHOICES.get(v) or PREF_CHOICES[PREF_DEFAULT]
        return {
            "preference": v,
            "config_version": pref_state["config_version"],
            "label": c["label"], "short": c["short"], "desc": c["desc"],
            "popup": bool(c["popup"]), "respect_cooldown": bool(c["respect_cooldown"]),
            "source": pref_state["source"], "updated_at": pref_state["updated_at"],
            "is_default": v == PREF_DEFAULT, "note": pref_state["note"],
            "rev": pref_state["rev"],
        }


def _pref_tag(pref):
    """一句可审计的偏好口径：「配置 v2『关闭主动提醒』（来源：用户保存 · 12:01:33）」。"""
    src = PREF_SOURCE_LABEL.get(pref.get("source"), pref.get("source") or "未知")
    c = PREF_CHOICES.get(pref.get("preference")) or PREF_CHOICES[PREF_DEFAULT]
    extra = (" · " + pref["updated_at"]) if pref.get("updated_at") else ""
    return "配置 %s「%s」（来源：%s%s）" % (pref.get("config_version"), c["short"], src, extra)


def _pref_public_choice(v):
    c = dict(PREF_CHOICES[v])
    c["is_default"] = (v == PREF_DEFAULT)
    return c


def _pref_trim_locked():
    if len(pref_state["events"]) > PREF_KEEP_EVENTS:
        pref_state["events"] = pref_state["events"][-PREF_KEEP_EVENTS:]
    if len(pref_state["history"]) > PREF_KEEP_HISTORY:
        pref_state["history"] = pref_state["history"][-PREF_KEEP_HISTORY:]


def _pref_event_locked(kind, text, detail=None, request_id=None, dedup_key=None, **extra):
    """登记一条日志（须持锁）。同一 dedup_key 3s 内重复 ⇒ 只留第一条（2s 轮询不刷屏）。"""
    ts = int(time.time() * 1000)
    if dedup_key:
        for e in pref_state["events"][-4:]:
            if e.get("dedup_key") == dedup_key and (ts - int(e.get("ts_ms") or 0)) < 3000:
                return e
    ev = {"id": "pe_%d_%d" % (ts, len(pref_state["events"])), "ts_ms": ts, "time": _ctx_clock(ts),
          "kind": kind, "text": text, "detail": detail, "request_id": request_id, "dedup_key": dedup_key,
          "config_version": pref_state["config_version"], "preference": pref_state["preference"]}
    ev.update(extra)
    pref_state["events"].append(ev)
    _pref_trim_locked()
    return ev


def _pref_remember_locked(key, text=None, detail=None, request_id=None, **extra):
    """记住用户的选择（台账式计数）+ 记一条“系统记住了什么”（须持锁）。"""
    mem = pref_state["memory"]
    mem[key] = int(mem.get(key) or 0) + 1
    ev = None
    if text:
        ev = _pref_event_locked("user_action", text, detail=detail, request_id=request_id,
                                memory_key=key, memory_count=mem[key], **extra)
    return {"key": key, "count": mem[key], "event": ev}


def pref_remember(key, text=None, detail=None, request_id=None, save=True, **extra):
    """对外入口：记住一次用户选择（可选立即落盘）。"""
    with pref_lock:
        r = _pref_remember_locked(key, text=text, detail=detail, request_id=request_id, **extra)
        if save:
            _pref_save_locked()
        return r


def _pref_payload_locked():
    """要落盘的内容（schema_version 是文件格式版本；config_version 是偏好配置版本，两者不同）。"""
    return {
        "schema_version": 1,
        "config_version": pref_state["config_version"],
        "preference": pref_state["preference"],
        "source": pref_state["source"],
        "updated_at": pref_state["updated_at"],
        "updated_at_ms": pref_state["updated_at_ms"],
        "note": pref_state["note"],
        "rev": pref_state["rev"],
        "history": [dict(h) for h in pref_state["history"]],
        "events": [dict(e) for e in pref_state["events"]],
        "memory": dict(pref_state["memory"]),
        "last_decision": json.loads(json.dumps(pref_state["last_decision"])),
        "saved_at": _ctx_clock(int(time.time() * 1000)),
    }


def _pref_save_locked():
    """原子落盘（临时文件 + os.replace）：偏好必须“重启/断电也不丢”。须持锁调用。"""
    tmp = PREF_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pref_payload_locked(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, PREF_FILE)
        pref_state["writes"] = int(pref_state.get("writes") or 0) + 1
        return True
    except Exception as e:                     # 磁盘只读 / 路径异常绝不拖垮主流程
        print("【偏好·落盘失败】%s（本次偏好仅存内存，重启会丢）" % e)
        return False


def pref_save():
    with pref_lock:
        return _pref_save_locked()


def _pref_load():
    """启动时载入偏好；文件缺失/损坏 ⇒ 用默认 v1 并写出一份（默认配置同样可持久化）。"""
    if os.path.exists(PREF_FILE):
        try:
            with open(PREF_FILE, encoding="utf-8") as f:
                d = json.load(f)
            with pref_lock:
                v = str(d.get("preference") or "")
                if v in PREF_CHOICES:
                    pref_state["preference"] = v
                pref_state["config_version"] = str(d.get("config_version") or PREF_VERSION_DEFAULT)
                pref_state["source"] = str(d.get("source") or "default")
                pref_state["updated_at"] = d.get("updated_at")
                pref_state["updated_at_ms"] = d.get("updated_at_ms")
                pref_state["note"] = str(d.get("note") or ("已从 %s 载入" % os.path.basename(PREF_FILE)))
                pref_state["rev"] = int(d.get("rev") or 0)
                pref_state["history"] = [h for h in (d.get("history") or [])
                                         if isinstance(h, dict)][-PREF_KEEP_HISTORY:]
                pref_state["events"] = [e for e in (d.get("events") or [])
                                        if isinstance(e, dict)][-PREF_KEEP_EVENTS:]
                pref_state["memory"] = {str(k): int(x or 0) for k, x in dict(d.get("memory") or {}).items()}
                pref_state["last_decision"] = (d.get("last_decision")
                                               if isinstance(d.get("last_decision"), dict) else None)
                _pref_trim_locked()
                ver, cur, n = pref_state["config_version"], pref_state["preference"], len(pref_state["history"])
            print("【用户偏好】已载入 %s：配置 %s · 偏好 %s · 历史 %d 个版本"
                  % (PREF_FILE, ver, cur, n))
            return True
        except Exception as e:
            print("【用户偏好·读取失败】%s → 改用默认配置 %s（并覆盖该文件）" % (e, PREF_VERSION_DEFAULT))
    with pref_lock:
        _pref_save_locked()
    print("【用户偏好】未找到 %s → 建立默认配置 %s（%s）" % (PREF_FILE, PREF_VERSION_DEFAULT, PREF_DEFAULT))
    return False


def _pref_next_version_locked():
    """下一个配置版本号 = 历史（含当前）里最大的 vN + 1；v1 只属于默认配置，修改一律 ≥ v2。"""
    n = 0
    for h in list(pref_state["history"]) + [{"config_version": pref_state["config_version"]}]:
        cv = str(h.get("config_version") or "")
        if cv[:1] == "v" and cv[1:].isdigit():
            n = max(n, int(cv[1:]))
    return "v%d" % max(2, n + 1)


def _pref_apply_locked(new_pref, source, note):
    """把偏好改成 new_pref：生成【新】config_version + 历史版本（须持锁）。返回 (entry, 旧版本号)。"""
    cur, old_ver = pref_state["preference"], pref_state["config_version"]
    new_ver = (PREF_VERSION_DEFAULT if (source == "restore_default" and new_pref == PREF_DEFAULT)
               else _pref_next_version_locked())
    ts = int(time.time() * 1000)
    pref_state["rev"] = int(pref_state.get("rev") or 0) + 1
    pref_state["config_version"] = new_ver
    pref_state["preference"] = new_pref
    pref_state["source"] = source
    pref_state["updated_at"], pref_state["updated_at_ms"] = _ctx_clock(ts), ts
    pref_state["note"] = note
    entry = {"rev": pref_state["rev"], "config_version": new_ver, "preference": new_pref,
             "previous_preference": cur, "previous_config_version": old_ver,
             "changed_at": _ctx_clock(ts), "changed_at_ms": ts, "source": source,
             "source_label": PREF_SOURCE_LABEL.get(source, source), "note": note}
    pref_state["history"].append(entry)
    _pref_trim_locked()
    return entry, old_ver


def _pref_previous_locked():
    """最近一次“与当前偏好【内容不同】”的历史版本（撤回 / 回退的目标）。"""
    cur = pref_state["preference"]
    for h in reversed(pref_state["history"]):
        if h.get("preference") != cur and h.get("preference") in PREF_CHOICES:
            return h
    return None


def _pref_effect_block(c):
    """把“这个偏好会怎么改变打扰行为”写清楚（前端直接显示，不自己解释）。"""
    return {
        "popup": bool(c["popup"]),
        "respect_cooldown": bool(c["respect_cooldown"]),
        "against_week9": ("弹窗之前必查本配置" if c["popup"] else "绝不弹窗：只亮黄条 / 徽章 + 后台记录"),
        "against_week11": ("照旧追问（仍受静默期与去重约束）" if c["popup"]
                           else "一并关闭：追问照旧展示，但 ask 一律为 False（只展示、不打扰）"),
    }


def pref_public(compact=False):
    """偏好快照（GET/POST 同一份口径；compact=True 供 /api/status 用，不带完整日志）。"""
    with pref_lock:
        cur = pref_state["preference"]
        c = PREF_CHOICES.get(cur) or PREF_CHOICES[PREF_DEFAULT]
        hist = [dict(h) for h in pref_state["history"]]
        events = [] if compact else [dict(e) for e in pref_state["events"][-PREF_SHOW_EVENTS:]]
        mem = dict(pref_state["memory"])
        ld = json.loads(json.dumps(pref_state["last_decision"])) if pref_state["last_decision"] else None
        prev = _pref_previous_locked()
        ts = int(time.time() * 1000)
        return {
            "status": "ok", "ts_ms": ts,
            "config_version": pref_state["config_version"],
            "preference": cur, "preference_label": c["label"], "preference_short": c["short"],
            "desc": c["desc"], "is_default": cur == PREF_DEFAULT,
            "default_preference": PREF_DEFAULT, "default_config_version": PREF_VERSION_DEFAULT,
            "source": pref_state["source"],
            "source_label": PREF_SOURCE_LABEL.get(pref_state["source"], pref_state["source"]),
            "updated_at": pref_state["updated_at"], "updated_at_ms": pref_state["updated_at_ms"],
            "note": pref_state["note"], "rev": pref_state["rev"],
            "effect": _pref_effect_block(c),
            "choices": [_pref_public_choice(v) for v in PREF_ORDER],
            "memory": mem,
            "memory_labels": dict(PREF_MEMORY_KEYS),
            "memory_total": sum(int(x or 0) for x in mem.values()),
            "history": list(reversed(hist))[:PREF_KEEP_HISTORY],
            "history_count": len(hist),
            "events": events, "event_count": len(pref_state["events"]),
            "last_decision": ld,
            "undo": {
                "available": bool(prev),
                "target_version": prev["config_version"] if prev else None,
                "target_preference": prev["preference"] if prev else None,
                "target_label": PREF_CHOICES[prev["preference"]]["short"] if prev else None,
                "note": "撤回 = 回到上一次“内容不同”的配置，并生成一个【新】版本（历史一律保留，可再回退）",
            },
            "storage": {
                "file": os.path.basename(PREF_FILE), "path": PREF_FILE, "format": "json",
                "exists": os.path.exists(PREF_FILE), "writes": int(pref_state.get("writes") or 0),
                "atomic": "临时文件 + os.replace（崩溃/断电不会写坏配置）",
                "note": "偏好单独存 %s：台账 photos_ledger.json 是“一次采集一条”的记录，不适合放全局配置。"
                        % os.path.basename(PREF_FILE),
            },
            "scope": "只影响本机这份偏好文件：不改变任何传感器判定与 week7/8/10/11 的结论，只改变“要不要打扰用户”。",
        }


def pref_set(preference, note=None, source="user"):
    """保存偏好。幂等：与当前完全相同 ⇒ changed=False（不新建版本、不写盘）。返回 (payload, http_code)。"""
    v = str(preference or "").strip()
    if v not in PREF_CHOICES:
        return {"status": "bad_request",
                "msg": "非法偏好：%s（合法值 %s）" % (v or "(空)", PREF_ORDER),
                "choices": [_pref_public_choice(x) for x in PREF_ORDER]}, 400
    with pref_lock:
        cur = pref_state["preference"]
        if v == cur:
            info = {"changed": False, "already": True,
                    "msg": "当前偏好已经是「%s」（配置 %s），无需保存"
                           % (PREF_CHOICES[cur]["short"], pref_state["config_version"])}
        else:
            c = PREF_CHOICES[v]
            entry, old_ver = _pref_apply_locked(v, source, note or "用户在面板保存偏好")
            _pref_event_locked(
                "pref_change", "用户修改了偏好为「%s」" % c["short"],
                detail="配置版本 %s → %s（%s）· 影响：此后每次弹窗之前都要先查这份偏好"
                       % (old_ver, entry["config_version"], c["desc"]),
                changed_by=source, from_preference=entry["previous_preference"], to_preference=v)
            _pref_remember_locked(
                "saved", "系统已记住：以后「%s」（%s）—— 每次打扰前都会先查这份偏好，随时可改、可撤回"
                         % (c["short"], entry["config_version"]),
                detail=note or "用户在面板保存偏好")
            saved = _pref_save_locked()
            info = {"changed": True, "already": False, "persisted": saved,
                    "previous_config_version": old_ver,
                    "previous_preference": entry["previous_preference"],
                    "msg": "已保存偏好「%s」→ 配置版本 %s（上一版 %s 已进历史，可撤回 / 可回退）"
                           % (c["short"], entry["config_version"], old_ver)}
    payload = pref_public()
    payload.update(info)
    print("【用户偏好】%s" % info["msg"])
    return payload, 200


def pref_restore_default(note=None):
    """恢复默认：偏好回到 low_conf_only，且 config_version 重置为 v1（任务书要求）。返回 (payload, code)。"""
    with pref_lock:
        cur, cur_ver = pref_state["preference"], pref_state["config_version"]
        if cur == PREF_DEFAULT and cur_ver == PREF_VERSION_DEFAULT:
            info = {"changed": False, "already": True,
                    "msg": "当前已经是默认配置 v1（%s），无需恢复" % PREF_CHOICES[PREF_DEFAULT]["short"]}
        else:
            entry, old_ver = _pref_apply_locked(PREF_DEFAULT, "restore_default", note or "用户点击【恢复默认】")
            _pref_event_locked(
                "pref_change", "用户点击【恢复默认】→ 偏好回到「%s」" % PREF_CHOICES[PREF_DEFAULT]["short"],
                detail="配置版本 %s → v1（默认配置的版本号就是 v1）· 历史版本 %s 仍保留，可回退" % (old_ver, old_ver),
                changed_by="restore_default", from_preference=entry["previous_preference"],
                to_preference=PREF_DEFAULT)
            _pref_remember_locked("restore_default",
                                  "系统已记住：用户会回到默认偏好（配置重置为 v1）", detail=note)
            saved = _pref_save_locked()
            info = {"changed": True, "already": False, "persisted": saved,
                    "previous_config_version": old_ver,
                    "previous_preference": entry["previous_preference"],
                    "msg": "已恢复默认：偏好「%s」，配置版本重置为 v1（原配置 %s 仍在历史里，可回退）"
                           % (PREF_CHOICES[PREF_DEFAULT]["short"], old_ver)}
    payload = pref_public()
    payload.update(info)
    print("【用户偏好】%s" % info["msg"])
    return payload, 200


def pref_undo(note=None):
    """撤回上一次修改：回到上一次“内容不同”的配置，并生成一个【新】版本。返回 (payload, code)。"""
    with pref_lock:
        target = _pref_previous_locked()
        if target is None:
            info = {"changed": False, "already": True,
                    "msg": "没有可撤回的修改（当前仍是系统默认配置 v1）"}
        else:
            short = PREF_CHOICES[target["preference"]]["short"]
            entry, old_ver = _pref_apply_locked(
                target["preference"], "undo", note or "用户撤回上一次修改 → 回到配置 %s" % target["config_version"])
            _pref_event_locked(
                "pref_change", "用户撤回上一次修改 → 偏好回到「%s」" % short,
                detail="目标历史版本 %s（%s · %s）→ 本次生成新版本 %s；历史版本一律保留"
                       % (target["config_version"], target["changed_at"],
                          PREF_SOURCE_LABEL.get(target.get("source"), target.get("source")),
                          entry["config_version"]),
                changed_by="undo", from_preference=entry["previous_preference"],
                to_preference=target["preference"], undo_of=target["config_version"])
            _pref_remember_locked("undo", "系统已记住：用户会撤回不满意的修改（本次回到「%s」· 配置 %s）"
                                          % (short, entry["config_version"]), detail=note)
            saved = _pref_save_locked()
            info = {"changed": True, "already": False, "persisted": saved,
                    "previous_config_version": old_ver,
                    "previous_preference": entry["previous_preference"],
                    "undo_of": target["config_version"],
                    "msg": "已撤回上一次修改：偏好「%s」，新配置版本 %s（历史版本 %s 仍可回退）"
                           % (short, entry["config_version"], target["config_version"])}
    payload = pref_public()
    payload.update(info)
    print("【用户偏好】%s" % info["msg"])
    return payload, 200


def pref_rollback(version, note=None):
    """回退到指定历史版本（版本号取自 history；不存在的版本 ⇒ 404 + 现有版本列表）。"""
    v = str(version or "").strip()
    with pref_lock:
        target = None
        for h in reversed(pref_state["history"]):
            if str(h.get("config_version")) == v and h.get("preference") in PREF_CHOICES:
                target = h
                break
        if target is None:
            info = {"changed": False, "status": "not_found",
                    "msg": "历史里没有配置版本 %s（现有版本 %s）"
                           % (v or "(空)", [h.get("config_version") for h in pref_state["history"]][-8:] or ["v1"])}
        elif target["preference"] == pref_state["preference"]:
            info = {"changed": False, "already": True,
                    "msg": "历史版本 %s 的偏好与当前一致（%s），无需回退"
                           % (v, PREF_CHOICES[target["preference"]]["short"])}
        else:
            short = PREF_CHOICES[target["preference"]]["short"]
            entry, old_ver = _pref_apply_locked(target["preference"], "rollback",
                                                note or "用户回退到历史版本 %s" % v)
            _pref_event_locked(
                "pref_change", "用户回退到历史版本 %s → 偏好「%s」" % (v, short),
                detail="配置版本 %s → %s（回退目标 %s · %s）；历史版本一律保留"
                       % (old_ver, entry["config_version"], v, target["changed_at"]),
                changed_by="rollback", from_preference=entry["previous_preference"],
                to_preference=target["preference"], rollback_to=v)
            _pref_remember_locked("rollback", "系统已记住：用户回退到 %s 的偏好「%s」（本次配置 %s）"
                                              % (v, short, entry["config_version"]), detail=note)
            saved = _pref_save_locked()
            info = {"changed": True, "already": False, "persisted": saved,
                    "previous_config_version": old_ver,
                    "previous_preference": entry["previous_preference"],
                    "rollback_to": v,
                    "msg": "已回退到历史版本 %s：偏好「%s」，新配置版本 %s"
                           % (v, short, entry["config_version"])}
    payload = pref_public()
    payload.update(info)
    print("【用户偏好】%s" % info["msg"])
    return payload, (404 if info.get("status") == "not_found" else 200)


def pref_note_prompt_cycle(pref, rid, verdict_reason, action, decision_text, cooldown_remaining=None,
                           memory_key=None, memory_text=None, memory_detail=None, stage="front",
                           persist=True):
    """week9 决策落账（register=True 时调用）：① 触发判定 ② 查偏好 ③ 决定 —— 三步全部写日志，
    并生成可逐条回放的链路（chain / chain_text），回答“为什么这次没弹窗”。"""
    with pref_lock:
        if memory_key and memory_text:
            _pref_remember_locked(memory_key, memory_text, detail=memory_detail, request_id=rid)
        v_ev = _pref_event_locked("capture_verdict", "触发抓拍失败/画面不可用（%s）" % verdict_reason,
                                  detail="request_id=%s" % (rid or "-"), request_id=rid,
                                  dedup_key="verdict|%s|%s" % (rid or "-", verdict_reason))
        c_ev = _pref_event_locked("pref_check", "系统检查偏好：%s" % _pref_tag(pref),
                                  detail="弹窗之前必查：preference=%s → popup=%s"
                                         % (pref["preference"], "是" if pref["popup"] else "否（只亮黄条 + 后台记录）"),
                                  request_id=rid, dedup_key="check|%s" % (rid or "-"))
        d_ev = _pref_event_locked("decision", decision_text, request_id=rid, action=action,
                                  detail=("静默期剩余 %.1fs" % cooldown_remaining) if cooldown_remaining else None,
                                  dedup_key="dec|%s|%s" % (rid or "-", action))
        change = None
        for e in reversed(pref_state["events"][:-3]):
            if e.get("kind") == "pref_change":
                change = e
                break
        chain = [{"time": change["time"], "text": change["text"]} if change
                 else {"time": pref.get("updated_at") or _ctx_clock(int(time.time() * 1000)),
                       "text": "沿用系统默认配置 v1（无修改记录）"}]
        chain.append({"time": v_ev["time"], "text": v_ev["text"]})
        chain.append({"time": c_ev["time"], "text": c_ev["text"]})
        if cooldown_remaining:
            chain.append({"time": d_ev["time"],
                          "text": "静默期剩余 %.1fs（week9 冷却，本次未覆盖）" % cooldown_remaining})
        chain.append({"time": d_ev["time"], "text": decision_text})
        pref_state["last_decision"] = {
            "ts_ms": d_ev["ts_ms"], "time": d_ev["time"], "request_id": rid, "stage": stage,
            "verdict_reason": verdict_reason, "action": action,
            "config_version": pref["config_version"], "preference": pref["preference"],
            "preference_short": PREF_CHOICES[pref["preference"]]["short"],
            "decision": decision_text,
            "chain": chain,
            "chain_text": " → ".join("%s %s" % (x["time"], x["text"]) for x in chain),
        }
        if persist:
            _pref_save_locked()
        return json.loads(json.dumps(pref_state["last_decision"]))


@app.route("/api/preferences", methods=["GET", "POST"])
def api_preferences():
    """week12: 用户交互提醒偏好（GET 读取 / POST 修改），带 config_version 且持久化。

    GET  → 当前偏好 + config_version + 三个可选项 + 影响说明 + 版本历史 + 决策日志 + 已记住的用户选择
    POST → {"action": "save" | "restore_default" | "undo" | "rollback",
            "preference": "always_ask" | "low_conf_only" | "never_ask",   # action=save 时必填
            "version": "v2",                                              # action=rollback 时必填
            "note": "可选备注"}
           省略 action 但带 preference 时按 save 处理；每次内容变化都会产生【新】config_version。
    """
    if request.method == "GET":
        return jsonify(pref_public())
    d = request.get_json(silent=True) or request.form or {}
    action = str(d.get("action") or ("save" if d.get("preference") else "")).strip().lower()
    note = (str(d.get("note") or "").strip()[:120]) or None
    if action in ("save", "set", "update"):
        payload, code = pref_set(d.get("preference"), note=note, source="user")
    elif action in ("restore_default", "default", "reset"):
        payload, code = pref_restore_default(note=note)
    elif action in ("undo", "revert"):
        payload, code = pref_undo(note=note)
    elif action in ("rollback", "history"):
        payload, code = pref_rollback(d.get("version") or d.get("config_version"), note=note)
    else:
        return jsonify({"status": "bad_request", "rule_version": RULE_VERSION,
                        "msg": "action 必须是 save / restore_default / undo / rollback（收到：%s）"
                               % (action or "(空)")}), 400
    return jsonify(payload), code


_pref_load()                        # 启动即载入（不存在则写出默认 v1）；此后每次修改都会立即落盘


# ================== week14: 断网感知 · 本地缓存队列 · 恢复后补传 ==================
# 任务书要求“断网时明确告知，恢复后正确补传”，核心是【双重状态】的如实表达：
#     本地已保存（本机 server/ 落盘成功）  ⇄  远端待接收（上游云端投递通道不可达）
# 实现纪律（与 week7~week12 一脉相承，任何一条都不许糊弄）：
#   * 模拟的是【远端投递通道】，不是“板端 ↔ 本机”的 LAN：断网时板端照常上传、图片照常落盘、
#     week7~week12 的遮挡判定 / 主动询问 / 偏好闸门一个字都不变 —— 否则“本地已保存”根本不成立；
#   * 断网期间每一条采集结果（成功与失败）都进本地队列并标记 pending_upload，
#     每条都带 request_id / 原始采集时间 / 排队时间 / 重试次数，可逐条审计；
#   * 恢复网络由【后台线程】按【原始采集时间】升序逐条补传，并逐条记录补传时间；
#     板端没上报采集时间的条目用“本地排队时间”兜底排序，同时如实标注 order_source（绝不冒充采集时间）；
#   * 队列独立存 server/offline_queue.json（与台账 / 偏好分开），写盘用临时文件 + os.replace；
#     已送达历史只保留最近 NET_QUEUE_HISTORY 条，待发送的一条都不丢。
NETWORK_SIMULATED_OFFLINE = os.environ.get("NETWORK_SIMULATED_OFFLINE", "").strip().lower() in \
    ("1", "true", "yes", "on")     # 系统变量：启动即视为断网（也可用 /api/network/toggle 手动切换）
QUEUE_FILE = os.path.join(_BASE_DIR, 'offline_queue.json')
NET_QUEUE_HISTORY = 50            # 已送达记录保留上限（防队列文件无限增长）
NET_QUEUE_PREVIEW = 20            # /api/network/status 最多回传多少条待发送明细
NET_RESTORE_STEP_SECONDS = 0.25   # 每条补传之间的间隔（前端看得见“补传中... X/Y”）
NET_EVENT_KEEP = 40               # 事件日志保留上限
NET_DELIVERY_LABEL = {            # 三种投递状态（前后端共用同一份文案，前端不写死）
    "delivered": "✅ 已接收",
    "pending_upload": "⏳ 待远端接收",
    "missing_local": "⚠️ 未送达（本地副本已清理）",
}
NET_ORDER_SRC_LABEL = {"device": "板端上报的采集时间",
                       "queue_time": "本地排队时间（板端未上报采集时间 → 兜底排序）"}

network_lock = threading.Lock()
network_state = {
    "since_ms": None,             # 本次断网开始时刻（毫秒）
    "offline_rounds": 0,          # 断网轮次（人工模拟）
    "restore_rounds": 0,          # 补传轮次
    "writes": 0,                  # 队列写盘次数
    "seq": 0,                     # 排队序号（同刻采集时间的稳定排序依据）
    "ev_id": 0,                   # 事件序号
    "pending": [],                # 待发送明细（本地已保存 → 远端待接收）
    "delivered": [],              # 已送达明细（含恢复后补传），保留最近 NET_QUEUE_HISTORY 条
    "events": [],                 # 事件日志（断网 / 进队 / 补传 / 失败）
    "counters": {"queued": 0, "delivered": 0, "resent": 0, "restore_fail": 0, "direct": 0},
    "restore": {                  # 补传进度（前端据 running 显示“补传中...”）
        "running": False, "trigger": None, "total": 0, "done": 0, "sent": 0, "failed": 0,
        "started_ms": None, "started_at": None, "finished_ms": None, "finished_at": None,
        "order": [], "order_note": None, "current": None, "last_result": None,
    },
}
_net_restore_thread = None


def net_is_offline():
    """远端投递通道是否不可达（True = 断网：板端仍能上传、本地照常落盘，新采集进本地队列）。"""
    return bool(NETWORK_SIMULATED_OFFLINE)


def _net_ms():
    return int(time.time() * 1000)


def _net_effective_ms(item):
    """补传排序用的时间去哪儿了：板端上报过采集时间就用它（source=device）；没上报 → 本地排队时间兜底。
    绝不用“接收时间”冒充采集时间，也绝不让前端自己猜（来源逐条回传给前端显示）。"""
    cms = item.get("collected_at_ms")
    if cms:
        return int(cms), "device"
    return int(item.get("queued_at_ms") or item.get("received_at_ms") or 0), "queue_time"


def _net_order(items):
    """按【原始采集时间】升序（同一时刻用排队序号保证稳定）；缺失采集时间的用排队时间兜底参与同一轮排序。"""
    return sorted(items, key=lambda it: (_net_effective_ms(it)[0], int(it.get("seq") or 0)))


def _net_event_locked(kind, text, detail=None, request_id=None, dedup_key=None):
    """写一条网络/补传事件（调用方需持有 network_lock）。dedup_key 相同 → 只累加次数不重复堆日志。"""
    now_ms = _net_ms()
    if dedup_key:
        for e in network_state["events"]:
            if e.get("dedup_key") == dedup_key:
                e.update({"ts_ms": now_ms, "time": _ms_str(now_ms), "text": text, "detail": detail,
                          "count": int(e.get("count") or 1) + 1, "offline": net_is_offline()})
                return e
    network_state["ev_id"] = int(network_state.get("ev_id") or 0) + 1
    ev = {"id": network_state["ev_id"], "kind": kind, "text": text, "detail": detail,
          "request_id": request_id, "dedup_key": dedup_key, "count": 1, "offline": net_is_offline(),
          "ts_ms": now_ms, "time": _ms_str(now_ms)}
    network_state["events"].append(ev)
    if len(network_state["events"]) > NET_EVENT_KEEP * 3:
        network_state["events"] = network_state["events"][-NET_EVENT_KEEP * 3:]
    return ev


def net_event(kind, text, detail=None, request_id=None, dedup_key=None):
    with network_lock:
        return _net_event_locked(kind, text, detail, request_id, dedup_key)


def _net_item_public(item, now_ms=None, idx=None, total=None):
    """一条待发送/已送达记录的对外视图（前端只渲染这些原文，不做任何推测）。"""
    now_ms = int(now_ms or _net_ms())
    eff, src = _net_effective_ms(item)
    status = item.get("status") or "pending_upload"
    name = item.get("filename")
    doc = {
        "queue_id": item.get("queue_id"), "seq": item.get("seq"),
        "request_id": item.get("request_id"), "filename": name,
        "kind": item.get("kind"), "size": item.get("size"), "trigger": item.get("trigger"),
        "status": status, "status_label": NET_DELIVERY_LABEL.get(status, status),
        "attempts": int(item.get("attempts") or 0),
        "queued_at_ms": item.get("queued_at_ms"), "queued_at": item.get("queued_at"),
        "collected_at_ms": item.get("collected_at_ms"), "collected_at": item.get("collected_at"),
        "collected_at_source": item.get("collected_at_source") or "unknown",
        "order_at_ms": eff, "order_at": _ms_str(eff), "order_source": src,
        "order_source_label": NET_ORDER_SRC_LABEL.get(src, src),
        "waited_ms": max(0, now_ms - int(item.get("queued_at_ms") or now_ms)),
        "uploaded_at_ms": item.get("uploaded_at_ms"), "uploaded_at": item.get("uploaded_at"),
        "resent_bytes": item.get("resent_bytes"), "resent_count": item.get("resent_count"),
        "mode": item.get("mode"), "reason": item.get("reason"), "detail": item.get("detail"),
        "error": item.get("last_error"),
        "local_exists": (not name) or os.path.exists(os.path.join(UPLOAD_FOLDER, name)),
    }
    if idx is not None:
        doc["order_index"] = idx
    if total is not None:
        doc["order_total"] = total
    return doc

def _net_queue_payload_locked():
    """队列落盘内容：pending = 本地已保存 · 远端待接收；delivered = 已送达（含恢复后补传）。"""
    now_ms = _net_ms()
    return {"version": 1, "seq": int(network_state["seq"]),
            "saved_at_ms": now_ms, "saved_at": _ms_str(now_ms),
            "simulated_offline": net_is_offline(),
            "note": "本地缓存队列（week14）：pending 待远端接收 / delivered 已送达；按原始采集时间补传",
            "pending": list(network_state["pending"]),
            "delivered": list(network_state["delivered"])[-NET_QUEUE_HISTORY:]}


def _net_queue_save_locked():
    """原子写盘（临时文件 + os.replace）—— 崩溃/断电不会把队列写坏。"""
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_net_queue_payload_locked(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE_FILE)
        network_state["writes"] += 1
        return True
    except Exception as e:
        print(f"【告警】本地队列保存失败: {e}")
        return False


def net_save():
    with network_lock:
        return _net_queue_save_locked()


def net_load():
    """启动读回队列：断网期间排队的数据绝不因为重启而丢。"""
    with network_lock:
        network_state["pending"] = []
        network_state["delivered"] = []
        doc = None
        if os.path.exists(QUEUE_FILE):
            try:
                with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
                    doc = json.load(f)
            except Exception as e:
                print(f"【告警】本地队列读取失败（按空队列启动）: {e}")
        if isinstance(doc, dict):
            network_state["pending"] = [it for it in (doc.get("pending") or []) if isinstance(it, dict)]
            network_state["delivered"] = [it for it in (doc.get("delivered") or [])
                                         if isinstance(it, dict)][-NET_QUEUE_HISTORY:]
            network_state["seq"] = max([int(doc.get("seq") or 0)]
                                       + [int(it.get("seq") or 0) for it in network_state["pending"]])
        elif isinstance(doc, list):
            network_state["pending"] = [it for it in doc if isinstance(it, dict)]
            network_state["seq"] = len(network_state["pending"])
        for it in network_state["pending"]:        # 重启后重算“原始采集时间”排序字段（来源照旧如实标注）
            eff, src = _net_effective_ms(it)
            it["order_at_ms"], it["order_source"] = eff, src
            it.setdefault("status", "pending_upload")
        for it in network_state["delivered"]:
            it.setdefault("status", "delivered")
            it.setdefault("mode", "restore")
        if not os.path.exists(QUEUE_FILE):
            _net_queue_save_locked()               # 首次启动即落盘：文件真实存在，不是内存里的幻觉
        return {"pending": len(network_state["pending"]), "delivered": len(network_state["delivered"]),
                "writes": network_state["writes"]}


def _net_enqueue_locked(entry):
    """把一条【本地已保存】的采集结果放进待发送队列（同一 request_id 只保留一条，重入只累加次数）。"""
    rid, name = entry.get("request_id"), entry.get("name")
    key = rid or name or ("ts_%s" % entry.get("ts_ms"))
    now_ms = _net_ms()
    for it in network_state["pending"]:
        if it.get("key") == key:
            it["attempts"] = int(it.get("attempts") or 0) + 1
            it["last_seen_ms"] = now_ms
            return it, False
    network_state["seq"] += 1
    item = {
        "key": key, "seq": network_state["seq"],
        "queue_id": "q_%d_%s" % (now_ms, uuid.uuid4().hex[:6]),
        "request_id": rid, "filename": name,
        "kind": entry.get("kind") or ("photo" if name else "failed"),
        "size": int(entry.get("size") or 0), "trigger": entry.get("trigger"),
        "status": "pending_upload", "attempts": 1,
        "queued_at_ms": now_ms, "queued_at": _ms_str(now_ms),
        "collected_at_ms": entry.get("collected_at_ms"), "collected_at": entry.get("collected_at"),
        "collected_at_source": entry.get("collected_at_source") or "unknown",
        "received_at_ms": entry.get("received_at_ms"),
        "reason": entry.get("reason"), "detail": entry.get("detail"),
    }
    eff, src = _net_effective_ms(item)
    item["order_at_ms"], item["order_source"] = eff, src
    network_state["pending"].append(item)
    network_state["counters"]["queued"] += 1
    _net_queue_save_locked()
    return item, True


def _net_record_delivered_locked(item, at_ms, mode, resent_bytes=0, note=None):
    """登记一条【已送达】记录（mode：direct=在线直传 / restore=恢复后补传），并保留补传时间。"""
    rec = dict(item)
    rec.update({"status": "delivered", "mode": mode, "uploaded_at_ms": at_ms,
                "uploaded_at": _ms_str(at_ms), "resent_bytes": resent_bytes, "note": note})
    network_state["delivered"].append(rec)
    if len(network_state["delivered"]) > NET_QUEUE_HISTORY:
        network_state["delivered"] = network_state["delivered"][-NET_QUEUE_HISTORY:]
    network_state["counters"]["delivered"] += 1
    network_state["counters"]["resent" if mode == "restore" else "direct"] += 1
    return rec

def _net_fields_direct(now_ms):
    """在线直传的投递字段（台账写入）。"""
    return {
        "delivery_status": "delivered", "delivery_label": NET_DELIVERY_LABEL["delivered"],
        "delivery_mode": "online_direct", "delivery_stage": "远端已接收（在线直传）",
        "delivered_at_ms": now_ms, "delivered_at": _ms_str(now_ms),
        "queue_id": None, "queued_at_ms": None, "queued_at": None,
        "delivery_note": ("在线直传：本地落盘与远端投递同时完成（delivered_at = received_at）；"
                          "demo 的远端通道为模拟通道，时间戳与顺序全部真实记录，绝不假装"),
    }


def _net_fields_pending(item):
    """断网缓存的投递字段（台账写入）：本地已保存，远端待接收，等恢复后按原始采集时间补传。"""
    return {
        "delivery_status": "pending_upload", "delivery_label": NET_DELIVERY_LABEL["pending_upload"],
        "delivery_mode": "offline_cache", "delivery_stage": "本地已保存 · 远端待接收",
        "queue_id": item.get("queue_id"), "queued_at_ms": item.get("queued_at_ms"),
        "queued_at": item.get("queued_at"), "delivered_at_ms": None, "delivered_at": None,
        "delivery_note": ("本地已保存（图片已落盘、台账已记录、判定照常），但远端投递通道断网 → "
                          "已进本地队列第 %s 条，恢复后按原始采集时间依次补传" % item.get("seq")),
    }


def net_track_delivery(entry):
    """week14 核心钩子：给一条刚入库的采集记录打【投递状态】。

    断网 → pending_upload（本地已保存 · 远端待接收，进本地队列，同一 request_id 不重复计数）；
    在线 → delivered（直传远端，投递时间 = 服务器接收时间）。
    返回写入台账的字段；entry 为空返回 None（绝不伪造记录）。"""
    if not entry:
        return None
    now_ms = _net_ms()
    # week14: 在线直传的“投递时间” = 服务器接收时间（delivered_at == received_at，不留凭空多出的几毫秒）
    recv_ms = int(entry.get("received_at_ms") or entry.get("ts_ms") or now_ms)
    if net_is_offline():
        with network_lock:
            item, created = _net_enqueue_locked(entry)
            still_pending = (item.get("status") == "pending_upload")
            item_pub = dict(item)
        if still_pending:
            fields = _net_fields_pending(item_pub)
        else:                                   # 极端情况：排队瞬间就被补传线程送达
            fields = _net_fields_direct(int(item_pub.get("uploaded_at_ms") or recv_ms))
            fields["delivery_mode"] = "restore"
            fields["delivery_stage"] = "远端已接收（排队瞬间已被补传线程送达）"
            fields["queue_id"] = item_pub.get("queue_id")
        net_event("queue_enter",
                  ("断网：本地已保存，远端待接收（第 %s 条进本地队列）" % item_pub.get("seq")) if created
                  else "断网：同一次采集重复入库 → 队列不重复计数（只累加遇到次数）",
                  detail=("图片已落盘、台账已记录、week7~week12 判定不变；远端投递等网络恢复后"
                          "按原始采集时间升序补传"),
                  request_id=entry.get("request_id"), dedup_key="queue:%s" % item_pub.get("key"))
    else:
        with network_lock:
            _net_record_delivered_locked(
                {"key": entry.get("request_id") or entry.get("name") or ("ts_%s" % entry.get("ts_ms")),
                 "queue_id": "d_%d_%s" % (now_ms, uuid.uuid4().hex[:6]), "seq": 0,
                 "request_id": entry.get("request_id"), "filename": entry.get("name"),
                 "kind": entry.get("kind") or "photo", "size": int(entry.get("size") or 0),
                 "trigger": entry.get("trigger"), "status": "delivered",
                 "collected_at_ms": entry.get("collected_at_ms"),
                 "collected_at": entry.get("collected_at"),
                 "collected_at_source": entry.get("collected_at_source") or "unknown",
                 "queued_at_ms": recv_ms, "queued_at": _ms_str(recv_ms)},
                recv_ms, "direct", resent_bytes=int(entry.get("size") or 0),
                note="在线直传：服务器接收即视为远端已接收（delivered_at = received_at）")
            _net_queue_save_locked()
        fields = _net_fields_direct(recv_ms)
    with ledger_lock:
        entry.update(fields)
        ledger_save()
    return fields


def _net_item_send_locked(item, at_ms):
    """把一条待发送记录补传到远端（调用方需持有 network_lock）。

    真实地从【本地副本】读回字节（照片还会校验 JPEG 头），本地副本缺失/损坏 → 如实失败：
    记录保留、状态标为 missing_local、绝不假装送达（也绝不静默丢弃）。"""
    name = item.get("filename")
    payload, err = b"", None
    if name:
        path = os.path.join(UPLOAD_FOLDER, name)
        if not os.path.exists(path):
            err = "本地副本已不存在（可能被“只保留 50 张”策略清理）：%s" % name
        else:
            try:
                with open(path, 'rb') as f:
                    payload = f.read()
            except Exception as e:
                err = "本地副本读取失败：%s" % e
            else:
                if item.get("kind") == "photo" and payload[:2] != b'\xff\xd8':
                    err = "本地副本不是有效 JPEG（完整性校验失败）"
    item["attempts"] = int(item.get("attempts") or 0) + 1
    if err:
        item["status"] = "missing_local"
        item["last_error"] = err
        item["failed_at_ms"] = at_ms
        network_state["counters"]["restore_fail"] += 1
        return False, err
    item["status"] = "delivered"
    item["mode"] = "restore"
    item["uploaded_at_ms"] = at_ms
    item["uploaded_at"] = _ms_str(at_ms)
    item["resent_bytes"] = len(payload) if name else 0
    item["resent_count"] = int(item.get("resent_count") or 0) + 1
    return True, None


def _net_write_entry_delivery(item, at_ms, ok, detail=None):
    """把补传结果回写台账（按 request_id / 文件名定位；台账已轮转则如实返回 False，不新建记录）。"""
    entry = ledger_find_any(request_id=item.get("request_id"), filename=item.get("filename"))
    if not entry:
        return False
    with ledger_lock:
        if ok:
            entry["delivery_status"] = "delivered"
            entry["delivery_label"] = NET_DELIVERY_LABEL["delivered"]
            entry["delivery_mode"] = "restore"
            entry["delivery_stage"] = "远端已接收（恢复网络后补传）"
            entry["delivered_at_ms"] = at_ms
            entry["delivered_at"] = _ms_str(at_ms)
            entry["delivery_note"] = ("恢复网络后补传完成：从本地副本重读 %s B 并按原始采集时间顺序重传"
                                      % (item.get("resent_bytes") or 0))
        else:
            entry["delivery_status"] = "missing_local"
            entry["delivery_label"] = NET_DELIVERY_LABEL["missing_local"]
            entry["delivery_mode"] = "restore_failed"
            entry["delivery_stage"] = "未送达（本地副本已清理）"
            entry["delivery_note"] = detail or "本地副本不存在，无法补传（记录保留，绝不假装送达）"
        ledger_save()
    return True

def _net_restore_begin_locked(trigger, total):
    now_ms = _net_ms()
    network_state["restore"].update({
        "running": True, "trigger": trigger, "total": total, "done": 0, "sent": 0, "failed": 0,
        "started_ms": now_ms, "started_at": _ms_str(now_ms), "finished_ms": None, "finished_at": None,
        "order": [], "order_note": None, "current": None, "last_result": None})
    network_state["restore_rounds"] += 1


def _net_restore_worker(trigger="toggle", paced=True):
    """后台补传线程主体：按【原始采集时间】升序逐条补传，并逐条记录补传时间。

    规则（硬性）：
      * 顺序 = 原始采集时间（collected_at）升序；板端没上报采集时间的条目用本地排队时间兜底，
        且本轮 order_note 与每条 order_source 都如实回传（绝不把排队时间冒充成采集时间）；
      * 每条补传都写 uploaded_at_ms（补传时间）+ 从本地副本重读的字节数；
      * 补传途中若又被切回断网 → 立即停手，剩余条目留在队列里（绝不丢）；
      * 本地副本已清理 → 如实失败（missing_local），记录保留。"""
    try:
        with network_lock:
            todo = _net_order([it for it in network_state["pending"] if it.get("status") == "pending_upload"])
            _net_restore_begin_locked(trigger, len(todo))
            if not todo:
                now_ms = _net_ms()
                network_state["restore"].update({"running": False, "finished_ms": now_ms,
                                                 "finished_at": _ms_str(now_ms), "last_result": "empty"})
                _net_event_locked("restore_skip", "恢复网络：本地队列为空，无需补传",
                                  dedup_key="restore_empty")
                _net_queue_save_locked()
                return "empty"
            fallback = [_net_effective_ms(it)[1] == "queue_time" for it in todo]
            note = ("按原始采集时间（collected_at）升序补传" if not any(fallback) else
                    "按原始采集时间升序补传；其中 %d 条缺失板端采集时间 → 用本地排队时间兜底排序"
                    "（逐条标注 order_source，绝不冒充采集时间）" % sum(1 for f in fallback if f))
            network_state["restore"]["order_note"] = note
            _net_event_locked("restore_start", "恢复网络：后台线程开始补传（共 %d 条）" % len(todo),
                              detail=note + "；每条都会记录补传时间（uploaded_at_ms）",
                              dedup_key="restore_start")
        for idx, item in enumerate(todo, 1):
            if net_is_offline():          # 补传途中又被切回离线 → 立刻停手，剩下的留在队列里
                with network_lock:
                    now_ms = _net_ms()
                    network_state["restore"].update({"running": False, "current": None,
                                                     "finished_ms": now_ms, "finished_at": _ms_str(now_ms),
                                                     "last_result": "offline_again"})
                    _net_event_locked("restore_stop",
                                      "补传中途切回断网 → 立即停止（剩余 %d 条留在本地队列）"
                                      % max(0, len(todo) - idx + 1), dedup_key="restore_stop")
                    _net_queue_save_locked()
                return "offline_again"
            with network_lock:
                network_state["restore"]["current"] = _net_item_public(item, idx=idx, total=len(todo))
                network_state["restore"]["order"].append(item.get("queue_id"))
            at_ms = _net_ms()
            with network_lock:
                ok, err = _net_item_send_locked(item, at_ms)
                sent_item = dict(item)
                if ok:
                    network_state["pending"] = [x for x in network_state["pending"] if x is not item]
                    _net_record_delivered_locked(sent_item, at_ms, "restore",
                                                 resent_bytes=int(sent_item.get("resent_bytes") or 0),
                                                 note="恢复网络后补传（按原始采集时间顺序）")
                rest = network_state["restore"]
                rest["done"] = idx
                rest["sent" if ok else "failed"] = int(rest["sent" if ok else "failed"]) + 1
                rest["current"] = _net_item_public(sent_item, idx=idx, total=len(todo))
                _net_event_locked("restore_item",
                                  "第 %d/%d 条补传%s：%s" % (idx, len(todo), "成功" if ok else "失败",
                                                             sent_item.get("filename")
                                                             or sent_item.get("request_id") or "--"),
                                  detail=("补传时间 %s · 从本地副本重读 %s B"
                                          % (_ms_str(at_ms), sent_item.get("resent_bytes") or 0)) if ok
                                  else ("未送达：%s（记录保留）" % err),
                                  request_id=sent_item.get("request_id"),
                                  dedup_key="restore_item:%s" % sent_item.get("queue_id"))
                _net_queue_save_locked()
            _net_write_entry_delivery(sent_item, at_ms, ok, err)
            if paced and idx < len(todo):
                time.sleep(NET_RESTORE_STEP_SECONDS)
        with network_lock:
            now_ms = _net_ms()
            rest = network_state["restore"]
            rest.update({"running": False, "current": None, "finished_ms": now_ms,
                         "finished_at": _ms_str(now_ms),
                         "last_result": ("done" if not rest["failed"] else "partial")})
            _net_event_locked("restore_done",
                              "补传完成：%d 条已送达%s"
                              % (rest["sent"], ("，%d 条未送达（本地副本已清理）" % rest["failed"])
                                 if rest["failed"] else ""),
                              detail="顺序：%s" % rest["order_note"], dedup_key="restore_done")
            _net_queue_save_locked()
        return "done"
    except Exception as e:
        print(f"【补传异常】后台补传线程出错: {e}")
        with network_lock:
            now_ms = _net_ms()
            network_state["restore"].update({"running": False, "current": None, "finished_ms": now_ms,
                                             "finished_at": _ms_str(now_ms), "last_result": "error: %s" % e})
            _net_event_locked("restore_fail", "补传线程异常：%s" % e, dedup_key="restore_error")
            _net_queue_save_locked()
        return "error"


def net_start_restore(trigger="toggle", paced=True):
    """启动后台补传线程（恢复网络时调用）。返回 (started, msg)。"""
    global _net_restore_thread
    with network_lock:
        if network_state["restore"]["running"]:
            return False, "补传已在进行中（trigger=%s）" % (network_state["restore"].get("trigger") or "--")
    if net_is_offline():
        return False, "当前仍是断网（模拟）状态：先恢复网络再补传"
    t = threading.Thread(target=_net_restore_worker, args=(trigger, paced),
                         name="net-restore", daemon=True)
    _net_restore_thread = t
    t.start()
    return True, None


def net_restore_wait(timeout=8.0):
    """等后台补传线程结束（前端/测试/关停用）。"""
    t = _net_restore_thread
    if t is None:
        return True
    t.join(timeout)
    return not t.is_alive()


def net_backfill_sync(trigger="sync"):
    """同步补传（人工触发 / 测试用）——与后台线程完全同一份逻辑。"""
    return _net_restore_worker(trigger, paced=False)


def net_set_offline(offline, reason="api"):
    """切换（模拟）网络状态。返回本次调用前的状态。"""
    global NETWORK_SIMULATED_OFFLINE
    offline = bool(offline)
    prev = net_is_offline()
    NETWORK_SIMULATED_OFFLINE = offline
    if offline == prev:
        with network_lock:
            return prev
    now_ms = _net_ms()
    with network_lock:
        waiting = len([it for it in network_state["pending"] if it.get("status") == "pending_upload"])
        if offline:
            network_state["since_ms"] = now_ms
            network_state["offline_rounds"] += 1
            _net_event_locked("offline_enter", "模拟断网开启：远端投递通道不可达 → 新采集进本地队列",
                              detail=("板端 ↔ 本机的 LAN 不受影响：图片照常落盘（本地已保存）、"
                                      "week7~week12 判定不变；队列里已有 %d 条待发送" % waiting),
                              dedup_key="offline_enter")
        else:
            network_state["since_ms"] = None
            _net_event_locked("offline_exit", "模拟断网关闭：远端投递通道恢复 → 后台线程按原始采集时间补传",
                              detail="待发送 %s 条（触发：%s）" % (waiting, reason),
                              dedup_key="offline_exit")
        _net_queue_save_locked()
    print("【week14】远端投递通道 → %s（触发：%s，待发送 %d 条）"
          % ("断网（本地已保存 · 远端待接收）" if offline else "在线（直传远端）", reason, waiting))
    return prev


def net_startup_check():
    """启动自检：如实汇报队列现状；若上次运行遗留待发送数据而当前在线 → 自动启动后台补传。"""
    with network_lock:
        waiting = len([it for it in network_state["pending"] if it.get("status") == "pending_upload"])
        stuck = len([it for it in network_state["pending"] if it.get("status") != "pending_upload"])
    print("【week14】本地缓存队列：待发送 %d 条 / 未送达 %d 条（队列文件 %s）"
          % (waiting, stuck, os.path.basename(QUEUE_FILE)))
    info = {"offline": net_is_offline(), "pending": waiting, "stuck": stuck, "restore_started": False}
    if waiting and not net_is_offline():
        started, msg = net_start_restore("startup")
        info["restore_started"] = started
        print("【week14】启动时发现 %d 条待发送（应为上次断网遗留）→ %s"
              % (waiting, "已启动后台补传线程" if started else ("未启动：%s" % msg)))
    return info

def net_policy():
    """week14 策略参数与口径说明（前端一律从这里取，绝不写死）。"""
    return {
        "env_var": "NETWORK_SIMULATED_OFFLINE",
        "simulated_offline": net_is_offline(),
        "queue_file": os.path.basename(QUEUE_FILE),
        "step_seconds": NET_RESTORE_STEP_SECONDS,
        "history_keep": NET_QUEUE_HISTORY,
        "preview": NET_QUEUE_PREVIEW,
        "sort_by": ("collected_at_ms（原始采集时间）升序；板端未上报时用本地排队时间兜底，"
                    "并逐条标注 order_source"),
        "delivery_states": dict(NET_DELIVERY_LABEL),
        "order_source_labels": dict(NET_ORDER_SRC_LABEL),
        "enqueue_on": ["/api/device/upload_photo 成功", "/api/device/upload_photo 失败（含超时 / 空帧 / 板端报错）"],
        "simulate_note": ("模拟的是【远端投递通道】：断网时板端 ↔ 本机 LAN 不受影响 —— 图片照常落盘"
                          "（本地已保存）、week7~week12 的遮挡判定 / 主动询问 / 偏好闸门完全不变，"
                          "只有“远端是否已接收”变成待接收"),
        "restore_note": ("恢复网络时由后台线程 net-restore 按原始采集时间升序补传，逐条记录补传时间"
                         "（uploaded_at_ms）；本地副本缺失则如实报 missing_local 并保留记录，绝不假装送达"),
    }


def _net_restore_public_locked():
    """补传进度快照（调用方需持有 network_lock）。"""
    rest = network_state["restore"]
    total, done = int(rest.get("total") or 0), int(rest.get("done") or 0)
    res = rest.get("last_result")
    if rest.get("running"):
        label = "🔄 补传中... %d/%d" % (done, total)
    elif res == "empty":
        label = "队列为空，无需补传"
    elif res == "offline_again":
        label = "补传中途切回断网 → 已停止（剩余条目留在队列）"
    elif res == "done":
        label = "✅ 补传完成：%d 条已送达（补传时间已记录）" % int(rest.get("sent") or 0)
    elif res == "partial":
        label = ("⚠️ 补传完成：%d 条已送达 / %d 条未送达（本地副本已清理）"
                 % (int(rest.get("sent") or 0), int(rest.get("failed") or 0)))
    elif res and str(res).startswith("error"):
        label = "❌ 补传异常：%s" % res
    else:
        label = "尚未补传（idle）"
    doc = dict(rest)
    doc.update({"remaining": max(0, total - done),
                "percent": (int(done * 100 / total) if total else 0), "label": label})
    return doc


def net_public(compact=False):
    """网络 / 补传对外视图。compact=True → /api/status 用的轻量快照（不带明细列表，且只读不写盘）。"""
    now_ms = _net_ms()
    with network_lock:
        pending = list(network_state["pending"])
        delivered = list(network_state["delivered"])
        events = list(network_state["events"])
        restore = _net_restore_public_locked()
        counters = dict(network_state["counters"])
        writes = network_state["writes"]
        since_ms = network_state["since_ms"]
        offline_rounds, restore_rounds = network_state["offline_rounds"], network_state["restore_rounds"]
    waiting = [it for it in pending if (it.get("status") or "pending_upload") == "pending_upload"]
    stuck = [it for it in pending if (it.get("status") or "pending_upload") != "pending_upload"]
    online = not net_is_offline()
    doc = {
        "status": "ok",
        "online": online, "offline": not online, "simulated_offline": net_is_offline(),
        "env_var": "NETWORK_SIMULATED_OFFLINE",
        "mode": "online_direct" if online else "offline_cache",
        "mode_label": "🟢 在线（直传远端）" if online else "🔴 离线（本地已保存 · 远端待接收）",
        "pill_label": ("🟢 在线" if online else ("🔴 离线（待发送 %d 条）" % len(waiting))),
        "pending_count": len(waiting), "stuck_count": len(stuck),
        "pending_bytes": sum(int(it.get("size") or 0) for it in waiting),
        "offline_since_ms": None if online else since_ms,
        "offline_since": None if (online or not since_ms) else _ms_str(since_ms),
        "offline_rounds": offline_rounds, "restore_rounds": restore_rounds,
        "restore": restore, "counters": counters,
        "storage": {"file": os.path.basename(QUEUE_FILE), "path": QUEUE_FILE,
                    "exists": os.path.exists(QUEUE_FILE), "writes": writes, "format": "JSON（version 1）",
                    "atomic": "临时文件 + os.replace（崩溃/断电不会写坏队列）",
                    "note": ("队列独立存放：photos_ledger.json 是采集记录（一次采集一条），"
                             "offline_queue.json 是投递进度（待发送 / 已送达）")},
        "policy": net_policy(),
        "server_time_ms": now_ms,
    }
    if compact:
        doc["recent_delivered"] = len(delivered)
        doc["latest_event"] = events[-1] if events else None
        return doc
    doc["pending"] = [_net_item_public(it, now_ms=now_ms, idx=i, total=len(waiting))
                      for i, it in enumerate(waiting[:NET_QUEUE_PREVIEW], 1)]
    doc["stuck"] = [_net_item_public(it, now_ms=now_ms) for it in stuck[:NET_QUEUE_PREVIEW]]
    doc["recent_delivered"] = [_net_item_public(it, now_ms=now_ms) for it in delivered[-10:][::-1]]
    doc["events"] = events[-NET_EVENT_KEEP:][::-1]
    doc["event_count"] = len(events)
    return doc

@app.route('/api/network/status', methods=['GET'])
def api_network_status():
    """week14: 网络与补传快照（在线/离线、待发送数量与明细、补传进度与记录、事件日志、策略参数）。

    ?compact=1 → 只回轻量快照（与 /api/status.network 同一份口径）。"""
    compact = (request.args.get("compact") or "").strip().lower() in ("1", "true", "yes")
    return jsonify(net_public(compact=compact)), 200


@app.route('/api/network/toggle', methods=['POST'])
def api_network_toggle():
    """week14: 手动模拟断网 / 恢复网络。

    入参 JSON（可省略）：{"offline": true|false} 或 {"action": "offline"|"online"}；
    都不给 → 直接翻转当前状态。恢复网络时自动启动后台补传线程（按原始采集时间升序）。"""
    d = request.get_json(silent=True) or {}
    raw = d.get("offline", request.args.get("offline"))
    action = str(d.get("action") or request.args.get("action") or "").strip().lower()
    if raw is None and action:
        if action in ("offline", "off", "simulate_offline", "disconnect"):
            raw = True
        elif action in ("online", "on", "recover", "connect"):
            raw = False
        else:
            return jsonify({"status": "error", "ok": False, "action": action,
                            "msg": "action 只支持 offline / online（收到：%s）" % action}), 400
    if raw is None:
        target = (not net_is_offline())
    elif isinstance(raw, str):
        target = raw.strip().lower() in ("1", "true", "yes", "on", "offline")
    else:
        target = bool(raw)
    prev = net_set_offline(target, reason="api/toggle")
    changed = (prev != target)
    started, rmsg = False, None
    if changed and not target:
        with network_lock:
            waiting = len([it for it in network_state["pending"] if it.get("status") == "pending_upload"])
        if waiting:
            started, rmsg = net_start_restore("toggle")
    payload = net_public()
    payload["changed"] = changed
    payload["restore_started"] = started
    payload["restore_msg"] = rmsg
    payload["msg"] = ("已切到模拟断网：新采集将进本地队列（本地已保存 · 远端待接收）" if target else
                      ("已恢复网络：远端投递通道可用"
                       + ("，后台补传线程已启动" if started else "（队列为空，无需补传）")))
    return jsonify(payload), 200


@app.route('/api/network/restore', methods=['POST'])
def api_network_restore():
    """week14: 人工触发补传（仅在线时有效）。默认起后台线程；{"sync": true} 或 ?sync=1 走同步补传。"""
    if net_is_offline():
        return jsonify({"status": "error", "ok": False, "offline": True,
                        "msg": "当前是断网（模拟）状态：先恢复网络再补传"}), 409
    d = request.get_json(silent=True) or {}
    sync = bool(d.get("sync")) or (request.args.get("sync") or "").lower() in ("1", "true", "yes")
    if sync:
        result = net_backfill_sync("api")
        payload = net_public()
        payload.update({"ok": True, "sync": True, "result": result, "restore_started": True,
                        "msg": "同步补传结束：%s" % result})
        return jsonify(payload), 200
    started, msg = net_start_restore("api", paced=bool(d.get("paced", True)))
    payload = net_public()
    payload.update({"ok": True, "sync": False, "restore_started": started, "restore_msg": msg,
                    "msg": ("后台补传线程已启动（按原始采集时间升序）" if started else (msg or "补传未启动"))})
    return jsonify(payload), 200


net_load()      # 启动即读回本地队列（不存在则立刻写出一份空队列）；断网期间排队的数据重启不丢


def check_timeout():
    """推进 week7 单张采集状态机（/api/status 与设备/AI 路径都会调用）。
      ACCEPTED --(板端 2s 冷却结束)--> COLLECTING（“采集中...”）
      PENDING/ACCEPTED/COLLECTING --(超过 10s 未收到图片)--> TIMEOUT（采集失败）
    超时只记账一次：进入终态后 fail_reason 已写入，后续轮询直接跳过。
    """
    expired = None
    with lock:
        st = task_state["status"]
        now = time.time()
        if (st == "ACCEPTED" and task_state.get("accepted_at")
                and now - task_state["accepted_at"] >= COLLECTING_AFTER_SECONDS):
            task_state["status"] = "COLLECTING"
            task_state["last_update"] = now
            print("【状态变更】板端已取走指令并开始抓帧 → COLLECTING（采集中...）")
            st = "COLLECTING"
        if st in ("PENDING", "ACCEPTED", "COLLECTING") and not task_state.get("fail_reason"):
            deadline = task_state.get("deadline") or (task_state["last_update"] + TIMEOUT_SECONDS)
            if now > deadline:
                task_state["status"] = "TIMEOUT"
                task_state["fail_reason"] = "timeout"
                task_state["last_update"] = now
                expired = dict(task_state)
    if expired:
        print(f"【状态变更】任务超时！超过 {CAPTURE_TIMEOUT_SECONDS:.0f}s 未收到图片 → 采集失败")
        _capture_failure("timeout",
                         f"设备超时：{CAPTURE_TIMEOUT_SECONDS:.0f} 秒内未收到图片",
                         expired.get("request_id"), expired.get("task_id"),
                         expired.get("trigger_source") or "web",
                         expired.get("created_at_ms"))

def touch_device():
    with lock:
        device_info["ip"] = request.remote_addr
        device_info["last_seen"] = time.time()

# ---------------- week7 fix(Bug2): 设备在线判定（以板端心跳为准） ----------------
def device_seen_age():
    """距最近一次板端请求（2s 轮询 / 1Hz IMU 上传 / 抓拍上报）的秒数；从未上线返回 None。"""
    with lock:
        seen = device_info.get("last_seen")
    if not seen:
        return None
    return round(max(0.0, time.time() - seen), 1)

def device_online():
    """板端是否在线：DEVICE_ONLINE_WINDOW 秒内收到过任意板端心跳即视为在线。
    离线时后端必须停止注入模拟 IMU 数据、前端必须冻结波形（week7 Bug2）。"""
    age = device_seen_age()
    return age is not None and age < DEVICE_ONLINE_WINDOW

# ---------------- week7: 采集 request_id / 门控 / 失败记账 ----------------
def _mint_capture_id(kind="cap"):
    """每次真实硬件采集的唯一 id（毫秒时间 + 随机后缀，便于与台账逐条对应）。"""
    return f"{kind}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

def _capture_gate_state():
    """抓拍按钮门控状态：锁定到期后自动解锁（惰性判定，不需要后台线程）。"""
    with capture_lock:
        now = time.time()
        if capture_gate["locked_until"] and now >= capture_gate["locked_until"]:
            capture_gate["locked_until"] = 0.0
            capture_gate["consecutive_fails"] = 0
            print("【抓拍解锁】锁定时间已到 → 抓拍按钮自动解锁")
        remaining = max(0.0, capture_gate["locked_until"] - now)
        return {
            "locked": remaining > 0,
            "locked_remaining": round(remaining, 1),
            "consecutive_fails": capture_gate["consecutive_fails"],
            "lock_seconds": COLLECT_LOCK_SECONDS,
            "lock_fails": COLLECT_LOCK_FAILS,
            "last_fail": capture_gate["last_fail"],
        }

def _capture_failure(reason, detail, request_id=None, task_id=None, trigger="web",
                     ts_ms=None, unusable=False, collected_ms=None):
    """记一次失败的采集：写台账（含 request_id + 毫秒时间戳）→ 连续失败计数 →
    连续 2 次失败即锁定抓拍 30s。返回台账条目。

    week10: 失败记录同样区分三种时间（collected_at 只在板端上报时才有），
    并同样经过 no_fix 位置绑定规则 —— 失败卡片上也不会出现“上次的位置”。"""
    global camera_last_fail_ms
    ms = int(ts_ms if ts_ms is not None else time.time() * 1000)
    loc_fix, loc_note, loc_public = _observation_link(None, request_id=request_id, received_ms=ms)
    entry = ledger_add_attempt(request_id, task_id, trigger, ok=False, reason=reason,
                               detail=detail, unusable=unusable, ts_ms=ms,
                               state="FAIL", collected_ms=collected_ms, received_ms=ms,
                               location_fix=loc_fix, location_note=loc_note, location=loc_public)
    with sensor_lock:
        camera_last_fail_ms = ms        # week10: 记录“最后一次采集失败”的时间（与成功时间分开）
    # 板端报错（含 <500B 空帧）也要立即结束本次采集：否则 10s 到点后还会再记一次
    # 超时失败 → 同一次采集被计两次失败（也会把按钮误锁）。
    with lock:
        if (request_id and task_state.get("request_id") == request_id
                and task_state["status"] not in ("IDLE", "COMPLETED", "TIMEOUT", "FAIL")):
            task_state["status"] = "FAIL"
            task_state["fail_reason"] = reason
            task_state["last_update"] = time.time()
            # week7 fix(Bug1 补强)：板端报错/空帧（<500B、非 JPEG）同样是“本次采集作废”，
            # /api/status.capture 必须带上 unusable/blocked/detail，前端黄色遮挡告警
            # 才能与 photos_ledger.json 里的判定完全一致地显式渲染出来。
            task_state["result"] = {
                "photo": None, "size": 0,
                "blocked": bool(unusable), "unusable": bool(unusable),
                "avg_gray": None, "dark_ratio": None,
                "detail": detail, "ts_ms": ts_ms, "reason": reason,
            }
            task_state["latest_photo"] = None      # 本次无有效画面 → 绝不显示旧图
            print(f"【状态变更】板端上报错误 → FAIL（{reason}），本次采集立即终止")
    fails, locked = 0, 0.0
    with capture_lock:
        capture_gate["consecutive_fails"] += 1
        capture_gate["last_fail"] = {"reason": reason, "detail": detail,
                                     "ts_ms": entry["ts_ms"]}
        fails = capture_gate["consecutive_fails"]
        if fails >= COLLECT_LOCK_FAILS:
            capture_gate["locked_until"] = time.time() + COLLECT_LOCK_SECONDS
            capture_gate["consecutive_fails"] = 0
            locked = COLLECT_LOCK_SECONDS
    print(f"【采集失败】request_id={request_id or '-'} 原因={reason} | {detail} | 连续失败={fails}")
    if locked:
        print(f"【抓拍锁定】连续 {COLLECT_LOCK_FAILS} 次未取得有效采集 → "
              f"锁定抓拍按钮 {COLLECT_LOCK_SECONDS:.0f}s（前端提示：设备离线，抓拍已锁定）")
    return entry

def _capture_public():
    """给前端的采集状态快照（/api/status -> capture 字段）。"""
    with lock:
        ts = dict(task_state)
    gate = _capture_gate_state()
    res = ts.get("result") or {}
    now = time.time()
    remaining = None
    if ts["status"] in ("PENDING", "ACCEPTED", "COLLECTING") and ts.get("deadline"):
        remaining = max(0.0, round(ts["deadline"] - now, 1))
    pub = {
        "state": ts["status"],
        "request_id": ts.get("request_id"),
        "task_id": ts.get("task_id"),
        "trigger": ts.get("trigger_source"),
        "photo": ts.get("latest_photo"),
        "ok": ts["status"] == "COMPLETED",
        "fail_reason": ts.get("fail_reason"),
        "detail": res.get("detail"),
        "blocked": bool(res.get("blocked")),
        # week7 fix(Bug1): 前端除了 blocked 还要能识别 unusable（板端上报 <500B 空帧时为 True）
        "unusable": bool(res.get("blocked") or res.get("unusable")),
        "avg_gray": res.get("avg_gray"),
        "dark_ratio": res.get("dark_ratio"),
        "size": res.get("size"),
        "ts_ms": res.get("ts_ms"),
        "remaining_s": remaining,
        "timeout_s": CAPTURE_TIMEOUT_SECONDS,
        "locked": gate["locked"],
        "lock_remaining": gate["locked_remaining"],
        "lock_seconds": gate["lock_seconds"],
        "consecutive_fails": gate["consecutive_fails"],
        # week9: 本次采集是否由【主动询问】建议的重拍而来（溯源，防串台）
        "retake_of": ts.get("retake_of"),
        "prompt_id": ts.get("prompt_id"),
    }
    # week14: 本次采集的【投递状态】—— 在线直传（delivered）/ 断网待远端接收（pending_upload）/ 未送达。
    # 前端采集面板据此如实显示，绝不自己猜“远端到底收到没有”。
    ent = None
    if ts.get("request_id") or ts.get("latest_photo"):
        ent = ledger_find_any(request_id=ts.get("request_id"), filename=ts.get("latest_photo"))
    pub.update({
        "delivery_status": (ent or {}).get("delivery_status"),
        "delivery_label": (ent or {}).get("delivery_label"),
        "delivery_stage": (ent or {}).get("delivery_stage"),
        "delivered_at_ms": (ent or {}).get("delivered_at_ms"),
        "delivered_at": (ent or {}).get("delivered_at"),
        "queued_at_ms": (ent or {}).get("queued_at_ms"),
        "queued_at": (ent or {}).get("queued_at"),
    })
    return pub

def photo_avg_gray(path):
    """平均灰度（0-255）与暗像素占比 —— 用于“镜头遮挡/画面不可用”初判。
    用 256-bin 直方图求均值，无需解码整张大图，开销极小。
    Pillow 缺失时返回 (None, None)，遮挡初判自动降级为“仅按文件大小”。"""
    if not _PIL_AVAILABLE:
        return None, None
    try:
        with _PILImage.open(path) as im:
            gray = im.convert("L").resize((GRAY_SAMPLE, GRAY_SAMPLE))
            hist = gray.histogram()          # 256 个 bin，稳定 API
        total = sum(hist) or 1
        mean = sum(i * c for i, c in enumerate(hist)) / total
        dark = sum(hist[:DARK_LEVEL]) / total    # week7 fix: 灰度 <64 视为“暗像素”（诊断用）
        return round(mean, 2), round(dark, 4)
    except Exception as e:
        print(f"【告警】灰度分析失败（遮挡初判跳过）: {e}")
        return None, None


# ---------------- week8: 真实像素特征提取（无任何预训练权重 / 不依赖外部模型文件） ----------------
def _rgb_to_hsv(arr):
    """arr: HxWx3 float32 (0-255, RGB) → (hue°, sat, val)，NumPy 向量化实现。
    公式与 cv2.COLOR_RGB2HSV 一致：H 单位是度（0-360），S/V 归一化到 0-1。
    这是“没装 OpenCV 时的等价替代”，不是近似玩具实现。"""
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    v = arr.max(axis=2)
    mn = arr.min(axis=2)
    c = v - mn
    s = _np.where(v > 1e-6, c / _np.maximum(v, 1e-6), 0.0)
    h = _np.zeros_like(v)
    nz = c > 1e-6
    m = nz & (v == r)                        # 红色为主：H = 60°·((G-B)/C mod 6)
    if bool(m.any()):
        h[m] = (60.0 * ((g[m] - b[m]) / c[m])) % 360.0     # 只在掩码内做除法（避免 0/0 警告）
    m = nz & (v == g) & (v != r)             # 绿色为主：H = 60°·((B-R)/C + 2)
    if bool(m.any()):
        h[m] = 60.0 * (((b[m] - r[m]) / c[m]) + 2.0) % 360.0
    m = nz & (v == b) & (v != r) & (v != g)  # 蓝色为主：H = 60°·((R-G)/C + 4)
    if bool(m.any()):
        h[m] = (60.0 * (((r[m] - g[m]) / c[m]) + 4.0)) % 360.0
    return h, s, v


def extract_visual_features(path):
    """对一张真实图片做像素统计特征提取，返回 dict（无法解码返回 None）。
    特征：平均亮度/对比度/暗亮像素占比/饱和度/彩色度/通道占比/色相分布/纹理梯度。"""
    if not (_PIL_AVAILABLE and _NP_AVAILABLE):
        return None
    try:
        with _PILImage.open(path) as im:
            small = im.convert("RGB").resize((INFER_SAMPLE, INFER_SAMPLE), _PIL_RESAMPLE)
            arr = _np.asarray(small, dtype=_np.float32)
    except Exception as e:
        print(f"【视觉推理】图像解码失败（无法推理）: {e}")
        return None

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b          # 亮度（ITU-R BT.601 权重）
    total = float(gray.size) or 1.0
    gray_mean = float(gray.mean())
    gray_std = float(gray.std())                      # 对比度
    dark_ratio = float((gray < DARK_LEVEL).sum()) / total
    bright_ratio = float((gray > 200).sum()) / total

    # HSV：优先 OpenCV，缺失时用上面的等价值
    backend = "opencv" if _CV2_AVAILABLE else "pillow+numpy"
    if _CV2_AVAILABLE:
        hsv = _cv2.cvtColor(arr.astype(_np.uint8), _cv2.COLOR_RGB2HSV).astype(_np.float32)
        hue = hsv[..., 0] * 2.0                       # OpenCV 的 H 是 0-179 → 还原成 0-360°
        sat = hsv[..., 1] / 255.0
        val = hsv[..., 2]
    else:
        hue, sat, val = _rgb_to_hsv(arr)

    chroma = sat * val / 255.0                        # 0-1 色度权重（灰/白像素≈0，不参与色相统计）
    lit = val > 20.0
    sat_mean = float(sat[lit].mean()) if bool(lit.any()) else 0.0

    # Hasler & Süsstrunk (2003) colorfulness：公开的彩色度量（不是私有魔数）
    rg = r - g
    yb = 0.5 * (r + g) - b
    colorfulness = float(_np.sqrt(rg.std() ** 2 + yb.std() ** 2)
                         + 0.3 * _np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))

    pix_sum = float(arr.sum()) or 1.0
    r_share = float(r.sum()) / pix_sum
    g_share = float(g.sum()) / pix_sum
    b_share = float(b.sum()) / pix_sum

    # 色相直方图：12 个 30° bin，按色度加权（判断色偏方向 + 单一色相集中度）
    hh = hue[lit] if bool(lit.any()) else hue.ravel()
    ww = chroma[lit] if bool(lit.any()) else chroma.ravel()
    hist, _ = _np.histogram(hh, bins=12, range=(0.0, 360.0), weights=ww)
    hist = hist / (float(hist.sum()) or 1.0)
    hue_warm = float(hist[0] + hist[1] + hist[10] + hist[11])   # 红/橙/黄：0-60° 与 300-360°
    hue_green = float(hist[2] + hist[3] + hist[4] + hist[5])    # 绿/青：60-180°
    hue_blue = float(hist[6] + hist[7] + hist[8] + hist[9])     # 蓝/紫：180-300°
    hue_conc = float(hist.max())                                # 最大单 bin 占比

    # 纹理：梯度幅值均值 + 明显边缘占比（区分“平坦墙面/桌面”与“细节丰富的近景”）
    gy, gx = _np.gradient(gray)
    mag = _np.sqrt(gx ** 2 + gy ** 2)
    texture = float(mag.mean()) / 255.0
    edge_ratio = float((mag > 12.0).mean())

    return {
        "backend": backend,
        "sample": f"{INFER_SAMPLE}x{INFER_SAMPLE}",
        "gray_mean": round(gray_mean, 2),        # 平均亮度 0-255
        "gray_std": round(gray_std, 2),          # 对比度
        "dark_ratio": round(dark_ratio, 4),      # 暗像素占比（灰度 < DARK_LEVEL）
        "bright_ratio": round(bright_ratio, 4),  # 过亮像素占比（灰度 > 200）
        "sat_mean": round(sat_mean, 4),          # 平均饱和度
        "colorfulness": round(colorfulness, 2),  # 彩色度
        "r_share": round(r_share, 4),
        "g_share": round(g_share, 4),
        "b_share": round(b_share, 4),
        "hue_warm": round(hue_warm, 4),
        "hue_green": round(hue_green, 4),
        "hue_blue": round(hue_blue, 4),
        "hue_conc": round(hue_conc, 4),
        "texture": round(texture, 5),
        "edge_ratio": round(edge_ratio, 4),
        "calibrated": False,                     # 明确标注：特征/分数未做标注集校准
    }


def _ramp(v, lo, hi):
    """分段线性隶属度：v<=lo → 0.0，v>=hi → 1.0，中间线性（把特征值映射为“有多像该描述”）。"""
    if hi == lo:
        return 0.0
    x = (float(v) - lo) / (hi - lo)
    if x <= 0.0:
        return 0.0
    return 1.0 if x >= 1.0 else round(x, 4)


def classify_visual_features(f):
    """规则集成分类器：每个标签的隶属度都是真实特征的连续函数（没有写死的结论）。
    返回 dict: label / score（置信度，非准确率）/ status / margin / memberships / alt。"""
    if not f:
        return {"label": None, "score": 0.0, "status": "error", "margin": 0.0, "memberships": {},
                "alt": None, "msg": "特征提取失败（图片无法解码，或缺少 Pillow/NumPy）"}

    gray_mean, dark, bright = f["gray_mean"], f["dark_ratio"], f["bright_ratio"]
    sat, colorful = f["sat_mean"], f["colorfulness"]
    warm, green, blue, conc = f["hue_warm"], f["hue_green"], f["hue_blue"], f["hue_conc"]
    r_share, g_share, b_share = f["r_share"], f["g_share"], f["b_share"]
    tex = f["texture"]

    m = {}
    # 1) 过暗：与 week7 的遮挡阈值 BLOCKED_MAX_GRAY 对齐（镜头被挡住是最常见原因）
    m[AI_LABELS[0]] = max(_ramp(BLOCKED_MAX_GRAY - gray_mean, 2.0, 25.0), _ramp(dark, 0.55, 0.92))
    # 2) 过曝：平均亮度过高，或大面积像素接近饱和（强光/反光）
    m[AI_LABELS[1]] = max(_ramp(gray_mean, 195.0, 240.0), _ramp(bright, 0.28, 0.65))
    # 3/4/5) 色偏：通道占比 + 色相集中在对应色区 + 有足够饱和度（三个条件同时成立才算）
    # 阈值取自 50 张真机照片实测分布：通道占比中位 0.33，色偏场景 0.38~0.47
    m[AI_LABELS[2]] = min(_ramp(g_share, 0.345, 0.400), _ramp(green, 0.32, 0.65), _ramp(sat, 0.06, 0.22))
    m[AI_LABELS[3]] = min(_ramp(b_share, 0.345, 0.395), _ramp(blue, 0.32, 0.65), _ramp(sat, 0.06, 0.22))
    m[AI_LABELS[4]] = min(_ramp(r_share, 0.345, 0.395), _ramp(warm, 0.32, 0.62), _ramp(sat, 0.06, 0.22))
    # 6) 低彩度平坦画面：彩色度低 + 纹理弱 + 饱和度低，且亮度处在“非过暗也非过曝”的区间
    #    （亮度带避免把纯黑/纯白画面也算成“白墙桌面”）
    low_color = min(_ramp(24.0 - colorful, 6.0, 18.0),
                    _ramp(0.052 - tex, 0.015, 0.042),
                    _ramp(0.20 - sat, 0.03, 0.14))
    bright_band = min(_ramp(gray_mean - 42.0, 6.0, 30.0), _ramp(205.0 - gray_mean, 8.0, 35.0))
    m[AI_LABELS[5]] = min(low_color, bright_band)
    # 7) 细节丰富的杂色场景：纹理高、彩色度高、且没有单一色相支配
    m[AI_LABELS[6]] = min(_ramp(tex, 0.030, 0.075),
                          _ramp(colorful, 18.0, 42.0),
                          _ramp(0.80 - conc, 0.02, 0.30))
    # 8) 常规场景（兜底头）：亮度适中、彩度处在中段、没有单一色相支配
    #    —— 没有这一项时，大量“不好不坏”的画面会因为所有隶属度都接近 0 而被拒识
    m[AI_LABELS[7]] = min(_ramp(gray_mean - 45.0, 5.0, 30.0), _ramp(200.0 - gray_mean, 10.0, 40.0),
                          _ramp(colorful, 3.5, 10.0), _ramp(32.0 - colorful, 6.0, 24.0),
                          _ramp(0.72 - conc, 0.02, 0.25))

    order = sorted(m.items(), key=lambda kv: kv[1], reverse=True)
    best_label, best = order[0]
    second_label, second = order[1] if len(order) > 1 else (None, 0.0)
    # 置信度 = 最高隶属度 × 区分度裕量（与次优标签差距归一化到 0-0.3）。
    # 两者接近说明“这幅图同时像多个描述”，此时置信度被压下来 —— 宁可拒识也不硬贴标签。
    sep = _ramp(best - second, 0.0, 0.30)
    score = round(best * (0.60 + 0.40 * sep), 3)
    if best < INFER_LOW_CONF or score < INFER_LOW_CONF:
        return {"label": None, "score": score, "status": "low_confidence",
                "margin": round(best - second, 3), "memberships": m,
                # 只有“最接近的描述”本身有实际隶属度时才展示，0 分时不给用户看噪声
                "alt": ({"label": best_label, "score": round(best, 3)} if best >= 0.05 else None),
                "msg": "无法确定画面内容，请重新采集"}
    return {"label": best_label, "score": score, "status": "success",
            "margin": round(best - second, 3), "memberships": m, "alt": None,
            "alt2": {"label": second_label, "score": round(second, 3)} if second_label else None}


# ---------------- Request isolation & cancellation (week 6) ----------------
# Only ONE logical request (voice or text) may publish a result. Every new
# request adopts a fresh id and invalidates all older ids, so a slow
# ASR → GLM → TTS round-trip can never overwrite a newer answer nor fire a
# hardware action after the user already started/stopped another task.
current_request_id = None
_request_lock = threading.Lock()
_cancelled_request_ids = set()      # ids explicitly stopped by the user
_request_events = {}                # request_id -> threading.Event (cooperative abort)


def _mint_request_id():
    return f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def begin_request(client_id=None):
    """Adopt `client_id` (minted when absent) as THE active request id and
    invalidate every older one. Returns the request id in use."""
    global current_request_id
    with _request_lock:
        old = current_request_id
        rid = (client_id or "").strip() or _mint_request_id()
        if old and old != rid:
            ev = _request_events.get(old)
            if ev:
                ev.set()                        # let the older task bail out fast
            _cancelled_request_ids.add(old)     # its late result must be dropped
        current_request_id = rid
        _request_events[rid] = threading.Event()
        if len(_request_events) > 64:           # keep the registry bounded
            stale = [k for k in list(_request_events.keys()) if k != rid][:32]
            for k in stale:
                _request_events.pop(k, None)
            _cancelled_request_ids.intersection_update(_request_events.keys())
    print(f"[TASK] request_id={rid} 已受理（作废旧任务: {old or '-'}）")
    return rid


def is_current_request(request_id):
    """True while `request_id` is still the active, non-cancelled request."""
    if not request_id:
        return True     # callers without an id (legacy/test) are never blocked
    with _request_lock:
        return (request_id == current_request_id
                and request_id not in _cancelled_request_ids)


def request_abort_event(request_id):
    with _request_lock:
        return _request_events.get(request_id)


def cancel_request(request_id=None):
    """Invalidate `request_id` (default: the active one) so its result is dropped."""
    global current_request_id
    with _request_lock:
        target = (request_id or current_request_id)
        if target:
            _cancelled_request_ids.add(target)
            ev = _request_events.get(target)
            if ev:
                ev.set()
            if target == current_request_id:
                current_request_id = None
        return target


@app.route('/api/cancel', methods=['POST'])
def cancel_task():
    """🛑 Stop button: invalidate the in-flight request (late results are
    discarded by the ASR/GLM/TTS pipeline) and drop a not-yet-polled board task."""
    d = request.get_json(silent=True) or {}
    rid = (d.get("request_id") or "").strip() or None
    cancelled = cancel_request(rid)

    board_cancelled = False
    with lock:
        if task_state["status"] in ("PENDING", "ACCEPTED", "COLLECTING"):
            # PENDING = the board has not polled it yet -> recall it before it fires
            # （ACCEPTED/COLLECTING 时板端已在抓帧，无法撤回，只能让本次上传自然结束）
            board_cancelled = task_state["status"] == "PENDING"
            if board_cancelled:
                task_state["status"] = "IDLE"
                task_state["task_id"] = None
                task_state["ack_needed"] = False
                task_state["request_id"] = None
                task_state["deadline"] = None
                task_state["fail_reason"] = None
                task_state["last_update"] = time.time()
            else:
                print("【取消】板端已取走指令并开始抓帧，本次采集无法撤回（不写失败台账）")

    print(f"[TASK] 用户按下停止: request_id={cancelled or '-'}, "
          f"板端挂起任务={'已撤销' if board_cancelled else '无'}")
    return jsonify({"status": "ok", "request_id": cancelled,
                    "board_cancelled": board_cancelled}), 200


# ---------------- IMU data source ----------------
imu_lock = threading.Lock()
imu_history = deque(maxlen=60)   # keep last 60 points (course spec)
imu_last_real = 0.0
IMU_SIM_START = time.time()

def imu_simulator_loop():
    """1 Hz IMU source. Used until board firmware posts real samples
    to /api/device/upload_imu (then real data wins for 5 s windows).

    week7 fix(Bug2): 只有【设备在线但还没有真实数据】(开机预热/固件没在传) 时才允许注入模拟点；
    一旦板端心跳消失（离线/掉线/TIMEOUT）就彻底停止模拟，并清空历史里残留的模拟点，
    让前端冻结在“最后一帧真实数据”，杜绝用 SIM 数据掩盖掉线。"""
    while True:
        now = time.time()
        online = device_online()                      # 心跳判定（板端 2s 轮询 + 1Hz 上传）
        with imu_lock:
            real_recent = (now - imu_last_real) < 5.0
        if not online:
            _imu_purge_simulated()                    # 离线 → 不注入任何模拟数据，并清掉残留 SIM 点
        elif not real_recent:
            el = now - IMU_SIM_START
            sample = {
                "t": round(now, 3),
                "ax": round(0.35 * math.sin(el * 0.9) + 0.12 * math.sin(el * 3.1) + random.gauss(0, 0.05), 3),
                "ay": round(0.30 * math.sin(el * 0.7 + 1.3) + 0.10 * math.sin(el * 2.3) + random.gauss(0, 0.05), 3),
                "az": round(1.00 + 0.25 * math.sin(el * 0.5 + 0.4) + random.gauss(0, 0.04), 3),
                "source": "simulated",
            }
            with imu_lock:
                imu_history.append(sample)
        time.sleep(1.0)

def _imu_purge_simulated():
    """丢掉历史里混入的模拟点，只保留板端真实采样（离线冻结时用）。"""
    global imu_history
    with imu_lock:
        if any(s.get("source") != "device" for s in imu_history):
            imu_history = deque((s for s in imu_history if s.get("source") == "device"),
                                maxlen=imu_history.maxlen or 60)
            print("【IMU】设备离线 → 已清除历史中的模拟(SIM)数据，波形冻结在最后一帧真实采样")

threading.Thread(target=imu_simulator_loop, daemon=True).start()

# ---------------- photo ledger (stored / cleaned history) ----------------
ledger = []
ledger_lock = threading.Lock()

def ledger_save():
    try:
        with open(LEDGER_FILE, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False)
    except Exception as e:
        print(f"【告警】照片台账保存失败: {e}")

def ledger_load():
    global ledger
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, 'r', encoding='utf-8') as f:
                ledger = json.load(f)
        except Exception:
            ledger = []
    known = {e.get("name") for e in ledger}
    for fn in sorted(os.listdir(UPLOAD_FOLDER)):
        if fn.endswith('.jpg') and fn not in known:
            p = os.path.join(UPLOAD_FOLDER, fn)
            ledger.append({"name": fn, "ts": os.path.getmtime(p),
                           "ts_ms": int(os.path.getmtime(p) * 1000),
                           "size": os.path.getsize(p), "status": "stored", "kind": "photo"})
    for e in ledger:
        if not e.get("name"):
            continue        # week7 失败记录（无图片文件）：保留元数据，不做文件校验
        e.setdefault("kind", "photo")
        e.setdefault("ts_ms", int(float(e.get("ts") or 0) * 1000))   # 旧记录补齐毫秒字段
        if e.get("status") == "stored" and not os.path.exists(os.path.join(UPLOAD_FOLDER, e["name"])):
            e["status"] = "cleaned"
    # week10: 旧记录补齐三种时间字段（历史数据没有板端采集时间 → 如实标注 unknown，绝不冒充）
    for e in ledger:
        ms = int(e.get("ts_ms") or int(float(e.get("ts") or 0) * 1000))
        e.setdefault("received_at_ms", ms)
        e.setdefault("received_at", _ms_str(ms))
        e.setdefault("received_at_source", "server")
        e.setdefault("collected_at_ms", None)
        e.setdefault("collected_at", None)
        e.setdefault("collected_at_source", "unknown")   # 旧台账从未记录板端采集时间
        e.setdefault("displayed_at_ms", None)
        e.setdefault("displayed_at", None)
        e.setdefault("upload_latency_ms", None)
        e.setdefault("location_fix", False)
        e.setdefault("location_note", None)
        # week14: 旧记录补齐【投递状态】字段（week14 之前没有投递追踪 → 如实标注“按已接收”，不伪造投递时间）
        e.setdefault("delivery_status", "delivered")
        e.setdefault("delivery_label", NET_DELIVERY_LABEL["delivered"])
        e.setdefault("delivery_mode", "legacy_assumed")
        e.setdefault("delivery_stage", "历史记录（week14 之前无投递追踪，按“已接收”标注）")
        e.setdefault("delivered_at_ms", None)
        e.setdefault("delivered_at", None)
        e.setdefault("queue_id", None)
        e.setdefault("queued_at_ms", None)
        e.setdefault("queued_at", None)
        e.setdefault("delivery_note", "week14 之前的记录：当时没有投递追踪，按“已接收”如实标注")
    ledger_save()

ledger_load()

def ledger_add(name, size, **extra):
    """Legacy helper (kept for backwards compatibility) — appends a stored photo."""
    with ledger_lock:
        entry = {"name": name, "ts": time.time(), "ts_ms": int(time.time() * 1000),
                 "size": size, "status": "stored", "kind": "photo", "ok": True}
        entry.update(extra)
        ledger.append(entry)
        if len(ledger) > 200:
            ledger[:] = ledger[-200:]
        ledger_save()
        return entry

def ledger_add_attempt(request_id, task_id, trigger, ok, reason=None, detail=None,
                       name=None, size=0, blocked=False, avg_gray=None, dark_ratio=None,
                       unusable=False, ts_ms=None, state=None, retake_of=None, prompt_id=None,
                       collected_ms=None, received_ms=None, collected_source=None,
                       location_fix=False, location_note=None, location=None):
    """week7: 记录一次【真实硬件采集】的成败。
    成功 → status=stored（有图片文件）；失败 → status=failed（无图片文件，元数据保留）。
    两种记录都带 request_id 与毫秒级时间戳，满足“图像与请求强关联”的溯源要求。
    失败的记录同样进入画廊（前端渲染成 ❌ 采集失败卡片），不会被软删除。

    week10: 严格区分三种时间 + 本次位置观测（绝不用历史值顶替）：
      collected_at —— 传感器采集时间：只有板端上报（collected_ms）才写，否则 None；
      received_at  —— 服务器接收时间：= ts_ms（本函数被调用的时刻），必有；
      displayed_at —— 前端展示刷新时间：初始 None，由 /api/display_ack 回填。
    """
    ms = int(received_ms if received_ms is not None else
             (ts_ms if ts_ms is not None else time.time() * 1000))
    entry = {
        "name": name,                                   # 失败时无文件（None）
        "ts": ms / 1000.0,                              # 兼容旧字段（秒，浮点）= 接收时间
        "ts_ms": ms,                                    # week7: 毫秒级精确时间戳（接收时间）
        "size": size,
        "status": "stored" if ok else "failed",
        "kind": "photo" if ok else "failed",
        "ok": bool(ok),
        "state": state or ("COMPLETED" if ok else "FAIL"),
        "request_id": request_id,                       # 本次采集的 request_id
        "task_id": task_id,
        "trigger": trigger,                             # web / timer / ai_chat / physical_button / ble
        "blocked": bool(blocked),                       # 遮挡/画面不可用初判
        "unusable": bool(unusable),
        "avg_gray": avg_gray,                           # 平均灰度（0-255）
        "dark_ratio": dark_ratio,                       # 近黑像素占比（诊断用）
        "reason": reason,
        "detail": detail,
        # ---- week10: 三种时间（严格分开，互不冒充） ----
        "received_at_ms": ms,
        "received_at": _ms_str(ms),
        "received_at_source": "server",
        "collected_at_ms": int(collected_ms) if collected_ms else None,
        "collected_at": _ms_str(collected_ms) if collected_ms else None,
        # 板端没上报采集时间时如实标注来源为未知，绝不用 received_at 冒充
        "collected_at_source": ("device" if collected_ms else (collected_source or "unknown")),
        "displayed_at_ms": None,
        "displayed_at": None,
        # 端到端延迟（只在两端时间都可信时给出）
        "upload_latency_ms": (ms - int(collected_ms)) if collected_ms else None,
        # ---- week10: 本次位置观测（no_fix 时严格 False，历史值单独存放） ----
        "location_fix": bool(location_fix),
        "location_note": location_note,
    }
    loc = location or {}
    entry["location_collected_at_ms"] = loc.get("collected_at_ms")
    entry["location_received_at_ms"] = loc.get("received_at_ms")
    entry["location_latitude"] = loc.get("latitude")
    entry["location_longitude"] = loc.get("longitude")
    entry["location_satellites"] = loc.get("satellites")
    entry["location_request_id"] = loc.get("request_id")
    hist = loc.get("historical_location") or {}
    entry["location_historical_available"] = bool(hist.get("available"))
    entry["location_historical_latitude"] = hist.get("latitude")
    entry["location_historical_longitude"] = hist.get("longitude")
    entry["location_historical_collected_at_ms"] = hist.get("collected_at_ms")
    entry["location_historical_note"] = hist.get("note")
    # week9: 本次采集若源自【主动询问】建议的重拍 → 记录来源（谁建议的、哪次询问），
    # 这样画廊里能一眼看出“这张是忽略/确认重拍链路里的哪一张”，也不会与其它采集串台。
    if retake_of:
        entry["retake_of"] = retake_of
    if prompt_id:
        entry["prompt_id"] = prompt_id
    with ledger_lock:
        ledger.append(entry)
        if len(ledger) > 200:
            ledger[:] = ledger[-200:]
        ledger_save()
    # week14: 每条采集结果都要有明确的【投递状态】—— 在线直传（delivered）/ 断网进本地队列（pending_upload）。
    # 这里是所有采集结果（成功 + 失败 + 未联机）的唯一落台账出口，因此钩子放在这里，绝不漏记。
    try:
        net_track_delivery(entry)
    except Exception as e:
        print(f"【告警】投递状态记账失败（不影响采集结果与台账）: {e}")
    return entry

def ledger_mark_cleaned(names):
    with ledger_lock:
        for e in ledger:
            if e.get("name") in names and e.get("status") == "stored":
                e["status"] = "cleaned"
        ledger_save()

# ---------------- week8: 视觉推理结果 ↔ 台账（photos_ledger.json）----------------
def ledger_find_entry(request_id=None, filename=None):
    """按 filename / request_id 定位台账条目（返回台账内的同一个 dict 对象，便于写回）。
    两者都不给 → 返回最近一张“已入库”的记录。"""
    with ledger_lock:
        items = list(ledger)
    if filename:
        for e in reversed(items):
            if e.get("name") == filename:
                return e
        return None
    if request_id:
        for e in reversed(items):
            if e.get("request_id") == request_id and e.get("name"):
                return e
        return None
    for e in reversed(items):
        if e.get("name") and e.get("status") == "stored":
            return e
    return None


def ledger_save_ai(entry, **fields):
    """把视觉推理 / 人工纠正结果作为【新字段】追加到台账对应记录里（绝不新建记录）。"""
    if not entry:
        return None
    with ledger_lock:
        entry.update(fields)
        ledger_save()
    return entry


def ledger_find_any(request_id=None, filename=None):
    """week9: 比 ledger_find_entry 更宽松的定位（允许命中「无图片的采集失败记录」）。
    week8 的推理/纠正入口语义保持不变；主动询问需要把 user_action 写到失败卡片上，
    而失败记录 name=None，week8 的定位会跳过它。"""
    with ledger_lock:
        items = list(ledger)
    if filename:
        for e in reversed(items):
            if e.get("name") == filename:
                return e
        return None
    if request_id:
        for e in reversed(items):
            if e.get("request_id") == request_id:
                return e
        return None
    for e in reversed(items):
        if e.get("name") and e.get("status") == "stored":
            return e
    return None
def _ai_public(entry, cached=False):
    """推理结果的对外视图：人工纠正优先展示，并把“置信度≠准确率”一路带到前端。"""
    if not entry:
        return None
    corrected = bool(entry.get("ai_corrected"))
    return {
        "filename": entry.get("name"),
        "request_id": entry.get("request_id"),
        "label": entry.get("ai_label_manual") if corrected else entry.get("ai_label"),
        "label_raw": entry.get("ai_label"),
        "score": entry.get("ai_score"),
        "status": "success" if corrected else (entry.get("ai_status") or "pending"),
        "msg": entry.get("ai_msg"),
        "corrected": corrected,
        "source": entry.get("ai_source"),          # auto(采集时) / api(接口) / manual(人工纠正)
        "engine": entry.get("ai_engine"),
        "backend": entry.get("ai_backend"),
        "calibrated": False,                       # 硬性约束：未校准分数绝不写成准确率
        "low_conf_threshold": entry.get("ai_low_conf_threshold", INFER_LOW_CONF),
        "ts_ms": entry.get("ai_ts_ms"),
        "cost_ms": entry.get("ai_cost_ms"),
        "alt": entry.get("ai_alt"),                # 拒识时给出“最接近的描述”，方便人工纠正
        "margin": entry.get("ai_margin"),
        "features": entry.get("ai_features") or {},
        "note": entry.get("ai_manual_note"),
        "cached": cached,
    }


def infer_photo_entry(entry, force=False, source="api"):
    """对一条台账记录做一次真实视觉推理，并把结果写回台账。返回对外视图。"""
    if not entry or not entry.get("name"):
        return {"status": "not_found", "msg": "没有可推理的图片：请先抓拍，或传入 filename / request_id"}
    if entry.get("ai_status") and not force:
        return _ai_public(entry, cached=True)        # 已算过 → 复用（force=true 才重算）
    path = os.path.join(UPLOAD_FOLDER, entry["name"])
    if not os.path.exists(path):
        ledger_save_ai(entry, ai_status="error", ai_msg="原图已清理，无法推理",
                       ai_engine=INFER_ENGINE, ai_source=source, ai_calibrated=False,
                       ai_ts_ms=int(time.time() * 1000))
        return _ai_public(entry)

    t0 = time.time()
    feats = extract_visual_features(path)
    res = classify_visual_features(feats)
    cost_ms = round((time.time() - t0) * 1000.0, 1)
    ledger_save_ai(
        entry,
        ai_label=res.get("label"),
        ai_score=res.get("score"),
        ai_status=res["status"],
        ai_msg=res.get("msg"),
        ai_alt=res.get("alt"),
        ai_margin=res.get("margin"),
        ai_engine=INFER_ENGINE,
        ai_backend=(feats or {}).get("backend"),
        ai_calibrated=False,                     # 未做标注集校准 → 只能叫“置信度”
        ai_low_conf_threshold=INFER_LOW_CONF,
        ai_features=feats,
        ai_ts_ms=int(time.time() * 1000),
        ai_cost_ms=cost_ms,
        ai_source=source,
    )
    label_txt = res.get('label') or ('(解码失败)' if res['status'] == 'error' else '(拒识: 置信度不足)')
    print(f"【视觉推理】{entry['name']} → status={res['status']} "
          f"label={label_txt} score={res.get('score')} "
          f"backend={(feats or {}).get('backend')} 耗时={cost_ms}ms")
    return _ai_public(entry)


# ---------------- week9: 主动询问（画面不可用 → 询问是否重拍）核心逻辑 ----------------
def _prompt_verdict(entry=None, request_id=None, filename=None, cap=None):
    """统一判定“本次画面是否不可用”，全部依据真实数据（不编造任何结论/物体名/位置）：
      blocked        → week7 遮挡初判（平均灰度 < 40、< 500B 空帧、板端报错）
      low_confidence → week8 置信度 < INFER_LOW_CONF(0.4) 且用户尚未人工纠正
      infer_error    → 图片无法解码 / 原图已清理
      no_image       → 本次采集失败（空帧/超时/设备错误），根本没有可用画面
    返回判定明细，供 /api/prompt_check 与 /api/status 共用。"""
    cap = cap if cap is not None else _capture_public()
    entry = entry or {}
    rid = request_id or entry.get("request_id") or cap.get("request_id")
    fname = filename or entry.get("name")
    state = cap.get("state")
    blocked = bool(entry.get("blocked") or entry.get("unusable"))
    if not blocked and (not entry or entry.get("request_id") == cap.get("request_id")):
        # 台账无遮挡标记时回落到本次采集状态（板端报错/空帧同样会置 blocked/unusable）
        blocked = bool(cap.get("blocked") or cap.get("unusable"))
    ai_status = entry.get("ai_status")
    ai_score = entry.get("ai_score")
    corrected = bool(entry.get("ai_corrected"))
    # 无图片的采集失败记录（空帧/设备报错）：reason 必须如实说“没有可用画面”，而不是“遮挡”
    no_file = bool(entry) and (not entry.get("name") or entry.get("status") == "failed")
    unusable, reason, detail = False, "ok", None
    if no_file:
        unusable, reason = True, "no_image"
        detail = entry.get("detail") or entry.get("reason") or "本次采集未产生可用画面"
    elif blocked:
        unusable, reason = True, "blocked"
        detail = entry.get("detail") or cap.get("detail") or "画面偏黑/疑似遮挡（week7 遮挡初判）"
    elif corrected:
        reason, detail = "ok", "用户已人工修正标签 → 画面已被理解，不再打扰"
    elif ai_status == "low_confidence" or (ai_score is not None and float(ai_score) < INFER_LOW_CONF):
        unusable, reason = True, "low_confidence"
        detail = f"最高置信度 {ai_score} < 拒识阈值 {INFER_LOW_CONF}"
    elif ai_status == "error":
        unusable, reason = True, "infer_error"
        detail = entry.get("ai_msg") or "图片无法解码或已被清理"
    elif not entry:
        if state in ("TIMEOUT", "FAIL"):
            unusable, reason = True, "no_image"
            detail = cap.get("detail") or cap.get("fail_reason") or "未收到有效画面（空帧/超时）"
        else:
            detail = "本次画面可用（无遮挡，置信度达标）"
    gray = entry.get("avg_gray")
    if gray is None:
        gray = cap.get("avg_gray")
    # 拒识（low_confidence）时 ai_label 为空 → 用 week8 的 ai_alt 作为“模型最接近的描述”，
    # 供用户在弹窗里判断要不要重拍（依然只描述画面特征，不编造物体名与位置）
    alt = entry.get("ai_alt") or {}
    label = entry.get("ai_label_manual") if corrected else entry.get("ai_label")
    if not label and alt.get("label"):
        label = alt.get("label")
    return {
        "unusable": unusable, "reason": reason, "detail": detail,
        "request_id": rid, "filename": fname,
        "blocked": bool(blocked), "avg_gray": gray,
        "score": ai_score,
        "alt": alt or None,
        "label": label,
        "task_state": state,
    }


def evaluate_proactive_prompt(request_id=None, filename=None, register=True, source="front"):
    """week9 决策：本次是否要【主动询问重拍】，以及是否被冷却/去重压制。
    register=True  → 登记“已询问”（否则前端 2s 轮询会把弹窗刷屏）；
    register=False → 只读快照（/api/status 用），不改变任何门控计数。"""
    rid = str(request_id or "").strip() or None
    fname = str(filename or "").strip() or None
    entry = ledger_find_any(request_id=rid, filename=fname) if (rid or fname) else ledger_find_entry()
    v = _prompt_verdict(entry=entry, request_id=rid, filename=fname)
    rid = v["request_id"]
    now = time.time()
    pstate = _prompt_state()
    res = {
        "status": "ok",
        "ask": False,
        "reason": v["reason"],
        "unusable": v["unusable"],
        "title": None, "msg": None, "note": None,
        "voice": PROMPT_VOICE_TEXT,
        "detail": v["detail"],
        "request_id": rid, "filename": v["filename"],
        "prompt_id": ("pr_" + rid) if rid else None,
        "label": v["label"], "score": v["score"], "threshold": INFER_LOW_CONF,
        "alt": v["alt"],
        "blocked": v["blocked"], "avg_gray": v["avg_gray"], "task_state": v["task_state"],
        "cooldown_seconds": PROMPT_COOLDOWN_SECONDS,
        "cooldown_active": pstate["cooldown_active"],
        "cooldown_remaining": pstate["cooldown_remaining"],
        "reask_seconds": PROMPT_REASK_SECONDS,
        "suppressed": pstate["suppressed"],
        "last_action": pstate["last_action"],
        "user_action": (entry or {}).get("user_action"),
        "source": source,
        "ts_ms": int(now * 1000),
    }
    if register:
        with prompt_lock:
            prompt_gate["counters"]["checked"] += 1
    # ---- week12: 弹窗【之前】先查用户偏好（后端唯一权威；前端只负责展示）----
    pref = pref_current()
    pref_note = _pref_tag(pref)
    res.update({
        "preference": pref["preference"], "preference_label": pref["label"],
        "preference_short": pref["short"], "config_version": pref["config_version"],
        "preference_source": pref["source"], "pref_popup_allowed": pref["popup"],
        "pref_respect_cooldown": pref["respect_cooldown"], "verdict_reason": v["reason"],
        "pref_suppressed": False, "cooldown_overridden": False, "reask_overridden": False,
    })
    if not v["unusable"]:
        return res                     # 画面可用 → 不询问（黄条/徽章等 week7/8 反馈保持不变）
    # ---- ① 偏好闸门：never_ask ⇒ 绝不弹窗，只亮黄条 + 后台记录（原因全部写清）----
    if not pref["popup"]:
        decision = "决定不弹窗（用户偏好「关闭主动提醒」：只亮黄条 + 后台记录，不打扰用户）"
        with prompt_lock:
            res["reason"] = "pref_never_ask"
            res["ask"] = False
            res["pref_suppressed"] = True
            res["note"] = ("用户偏好「%s」（%s）→ 绝不弹窗：黄条 / 徽章照旧显示，本次判定仅后台记录"
                           % (pref["short"], pref_note))
            if register and rid and rid not in prompt_gate["suppressed_ids"]:
                prompt_gate["suppressed_ids"].add(rid)
                prompt_gate["suppressed"] += 1
                prompt_gate["suppressed_last"] = {"request_id": rid, "reason": "pref_never_ask",
                                                  "ts_ms": int(now * 1000)}
                if len(prompt_gate["suppressed_ids"]) > 256:
                    prompt_gate["suppressed_ids"] = set(list(prompt_gate["suppressed_ids"])[-128:])
            res["cooldown_active"] = prompt_gate["cooldown_until"] > now
            res["cooldown_remaining"] = round(max(0.0, prompt_gate["cooldown_until"] - now), 1)
            res["suppressed"] = prompt_gate["suppressed"]
        if register:
            pref_note_prompt_cycle(
                pref, rid, v["reason"], "no_popup_by_preference", decision, stage=source, persist=True,
                memory_key="blocked_by_pref",
                memory_text="系统已记住：按偏好「关闭主动提醒」不打扰（%s）" % v["reason"],
                memory_detail="preference=%s · popup=False" % pref["preference"])
            print(f"【主动询问·偏好压制】request_id={rid or '-'} 画面不可用（{v['reason']}），"
                  f"但用户偏好「{pref['short']}」（{pref['config_version']}）→ 不弹窗，"
                  f"仅黄条 + 后台记录（累计压制 {res['suppressed']} 次）")
        return res
    # ---- ② 常规门控（week9 既有策略）；偏好「总是提醒」不受静默期 / 去重限制 ----
    with prompt_lock:
        handled = prompt_gate["handled"].get(rid)
        cooling = prompt_gate["cooldown_until"] > now
        asked = prompt_gate["asked"].get(rid)
        recent_ask = bool(asked) and (now - float(asked.get("ts") or 0)) < PROMPT_REASK_SECONDS
        relaxed = not pref["respect_cooldown"]
        if handled:
            res["reason"] = "handled"
            res["note"] = f"用户已处理该画面（{handled}），不再重复询问"
            decision = "决定不弹窗（用户已处理该画面：%s）" % handled
        elif (cooling or recent_ask) and not relaxed:
            if cooling:
            # 硬性要求：用户刚点“忽略” → 30s 内即使再次判定不可用也【不弹窗】，只在后台记录
                res["reason"] = "cooldown"
                res["note"] = (f"用户刚选择忽略 → 静默 {PROMPT_COOLDOWN_SECONDS:.0f}s 内不弹窗，"
                               f"仅后台记录（黄条/徽章照旧显示）")
                decision = ("决定不弹窗（静默期剩余 %.1fs：用户刚选择忽略）"
                            % max(0.0, prompt_gate["cooldown_until"] - now))
                if register and rid and rid not in prompt_gate["suppressed_ids"]:
                    # 同一张画面在冷却期内被反复轮询只计一次（计数=被压制的不同画面数，不虚高）
                    prompt_gate["suppressed_ids"].add(rid)
                    prompt_gate["suppressed"] += 1
                    prompt_gate["suppressed_last"] = {"request_id": rid, "reason": v["reason"],
                                                      "ts_ms": int(now * 1000)}
                    if len(prompt_gate["suppressed_ids"]) > 256:
                        prompt_gate["suppressed_ids"] = set(
                            list(prompt_gate["suppressed_ids"])[-128:])
            else:
                res["reason"] = "already_asked"
                res["note"] = f"同一张画面 {PROMPT_REASK_SECONDS:.0f}s 内已询问过 → 不重复弹窗"
                decision = "决定不弹窗（同一张画面 %.0fs 内已询问过）" % PROMPT_REASK_SECONDS
        else:
            title, msg = PROMPT_TEXT.get(v["reason"],
                                         ("画面不可用", "⚠️ 画面不可用。是否立即重新抓拍？"))
            res.update(ask=True, title=title, msg=msg)
            if cooling or recent_ask:
                # 偏好「总是提醒」：明确覆盖 week9 的静默期 / 去重，并把“为什么照样弹”写进 note
                res["cooldown_overridden"] = bool(cooling)
                res["reask_overridden"] = bool(recent_ask)
                res["note"] = ("偏好「%s」（%s）不受%s限制 → 仍然弹窗（用户明确要求总提醒）"
                               % (pref["short"], pref_note,
                                  "静默期" if cooling else "同一张画面的去重窗口"))
            decision = (("决定弹窗询问是否重拍（偏好「总是提醒」覆盖了%s）"
                         % ("静默期" if cooling else "去重窗口")) if (cooling or recent_ask)
                        else ("决定弹窗询问是否重拍（画面不可用：%s）" % v["reason"]))
            if register:
                prompt_gate["asked"][rid] = {"prompt_id": res["prompt_id"], "ts": now,
                                             "reason": v["reason"]}
                prompt_gate["counters"]["ask"] += 1
                if len(prompt_gate["asked"]) > 128:
                    keep = sorted(prompt_gate["asked"].items(),
                                  key=lambda kv: float(kv[1].get("ts") or 0))[-64:]
                    prompt_gate["asked"] = dict(keep)
        res["cooldown_active"] = cooling
        res["cooldown_remaining"] = round(max(0.0, prompt_gate["cooldown_until"] - now), 1)
        res["suppressed"] = prompt_gate["suppressed"]
    if register:
        # week12: 决策落账（触发判定 → 查偏好 → 决定）—— 面板可逐条回放“为什么这次没弹窗”
        pref_note_prompt_cycle(
            pref, rid, v["reason"], ("ask_retake" if res["ask"] else (res["reason"] or "none")),
            decision or "决定不弹窗", stage=source, persist=True,
            cooldown_remaining=(res["cooldown_remaining"] if res["reason"] == "cooldown" else None),
            memory_key=("ask" if res["ask"] else None),
            memory_text=(("系统已记住：本次已弹窗询问是否重拍（偏好 %s · 配置 %s）"
                          % (pref["preference"], pref["config_version"])) if res["ask"] else None))
        if res["ask"]:
            print(f"【主动询问】request_id={rid or '-'} 判定画面不可用（{res['reason']}：{res['detail']}）"
                  f" → 下发询问「{res['msg']}」（prompt_id={res['prompt_id']}）"
                  f"｜偏好 {pref['preference']}（{pref['config_version']}）")
        elif res["reason"] == "cooldown":
            print(f"【主动询问·冷却压制】request_id={rid or '-'} 画面仍不可用（{v['reason']}），"
                  f"静默期剩余 {res['cooldown_remaining']:.1f}s → 不弹窗，仅后台记录"
                  f"（累计压制 {res['suppressed']} 次）")
        elif res["reason"] == "already_asked":
            print(f"【主动询问·去重】request_id={rid or '-'} {PROMPT_REASK_SECONDS:.0f}s 内已询问过 → 不重复弹窗")
    return res


PROMPT_ACTION_ALIASES = {
    "confirmed_retake": "confirmed_retake", "confirm": "confirmed_retake",
    "confirmed": "confirmed_retake", "retake": "confirmed_retake",
    "yes": "confirmed_retake", "ok": "confirmed_retake", "accept": "confirmed_retake",
    "ignored_retake": "ignored_retake", "ignore": "ignored_retake",
    "ignored": "ignored_retake", "no": "ignored_retake",
    "reject": "ignored_retake", "dismiss": "ignored_retake",
}


def submit_prompt_action(action, prompt_id=None, request_id=None, filename=None, note=None,
                         source="front"):
    """week9: 处理用户对【主动询问】的动作（确认重拍 / 忽略 + 冷却），返回 (payload, http_code)。
    强关联校验：prompt_id 必须等于 "pr_<request_id>"，否则按“询问已过期”拒绝（409）——
    从根上杜绝“给 A 照片弹窗，结果重拍的是 B”。动作写回台账 user_action 字段。"""
    act = PROMPT_ACTION_ALIASES.get(str(action or "").strip().lower())
    if not act:
        return {"status": "error", "action": action,
                "msg": "action 必须是 confirmed_retake（确认重拍）或 ignored_retake（忽略）"}, 400
    rid = str(request_id or "").strip() or None
    pid = str(prompt_id or "").strip() or None
    fname = str(filename or "").strip() or None
    if not rid and pid and pid.startswith("pr_"):
        rid = pid[3:]                      # 只回传 prompt_id 也能定位到那次采集
    entry = ledger_find_any(request_id=rid, filename=fname) if (rid or fname) else None
    if entry and not rid:
        rid = entry.get("request_id")
    expect_pid = ("pr_" + rid) if rid else None
    if pid and expect_pid and pid != expect_pid:
        print(f"【主动询问·过期】prompt_id={pid} 与 request_id={rid} 不匹配 → 拒绝动作（防串台）")
        return {"status": "stale", "action": act, "request_id": rid, "prompt_id": pid,
                "expected_prompt_id": expect_pid,
                "msg": "该询问已过期（prompt_id 与 request_id 不匹配），已忽略"}, 409
    if entry is None:
        return {"status": "not_found", "action": act, "request_id": rid,
                "prompt_id": pid or expect_pid,
                "msg": "找不到该询问对应的采集记录（request_id 已过期或从未落台账）"}, 404

    now = time.time()
    fields = {
        "user_action": act,
        "user_action_ts_ms": int(now * 1000),
        "user_action_prompt_id": pid or expect_pid,
        "user_action_source": source or "front",
    }
    if act == "ignored_retake":
        fields["prompt_cooldown_until_ms"] = int((now + PROMPT_COOLDOWN_SECONDS) * 1000)
        fields["prompt_cooldown_seconds"] = PROMPT_COOLDOWN_SECONDS
    if note:
        fields["user_action_note"] = str(note)[:120]
    ledger_save_ai(entry, **fields)           # 追加字段到原记录，绝不新建记录

    with prompt_lock:
        prompt_gate["last_action"] = act
        prompt_gate["last_action_ts_ms"] = int(now * 1000)
        prompt_gate["last_request_id"] = rid
        prompt_gate["last_prompt_id"] = pid or expect_pid
        prompt_gate["handled"][rid] = act
        if act == "ignored_retake":
            prompt_gate["cooldown_until"] = now + PROMPT_COOLDOWN_SECONDS
            prompt_gate["counters"]["ignored"] += 1
        else:
            prompt_gate["counters"]["confirmed"] += 1
        if len(prompt_gate["handled"]) > 128:
            for k in list(prompt_gate["handled"].keys())[:-64]:
                prompt_gate["handled"].pop(k, None)
    # week12: 记住用户对主动询问的处理方式（并写进决策日志；与偏好设置同一条时间线）
    pref_remember(
        ("ignored_retake" if act == "ignored_retake" else "confirmed_retake"),
        (("用户忽略重拍建议 → 系统记住：%.0fs 内不再为该画面弹窗（仅后台记录）" % PROMPT_COOLDOWN_SECONDS)
         if act == "ignored_retake" else
         ("用户确认重拍 → 系统记住：立即发起新的单张采集（原 request_id=%s）" % (rid or "-"))),
        detail="prompt_id=%s · 来源 %s" % ((pid or expect_pid) or "-", source or "front"),
        request_id=rid, save=True)
    if act == "ignored_retake":
        print(f"【主动询问·忽略】request_id={rid} → 台账 user_action=ignored_retake，"
              f"进入 {PROMPT_COOLDOWN_SECONDS:.0f}s 静默期（期间画面不可用也不弹窗，仅后台记录）")
    else:
        print(f"【主动询问·确认重拍】request_id={rid} → 台账 user_action=confirmed_retake，"
              f"前端将立即发起新的单张采集（新 request_id，带 retake_of={rid} 溯源）")
    cap = _capture_public()
    return {
        "status": "ok",
        "action": act,
        "request_id": rid,
        "filename": entry.get("name"),
        "prompt_id": pid or expect_pid,
        "user_action": act,
        "cooldown_seconds": PROMPT_COOLDOWN_SECONDS,
        "cooldown_active": act == "ignored_retake",
        "cooldown_remaining": PROMPT_COOLDOWN_SECONDS if act == "ignored_retake" else 0.0,
        "cooldown_until_ms": (int((now + PROMPT_COOLDOWN_SECONDS) * 1000)
                              if act == "ignored_retake" else None),
        "task_state": cap.get("state"),
        "ts_ms": int(now * 1000),
        "msg": (f"已记录：用户忽略重拍建议（{PROMPT_COOLDOWN_SECONDS:.0f}s 内不再弹窗）"
                if act == "ignored_retake" else "已记录：用户确认重拍"),
        "prompt_state": _prompt_state(),
    }, 200


# ---------------- API: control plane ----------------
@app.route('/api/trigger_capture', methods=['POST'])
def trigger_capture():
    """week7 单张采集入口（网页“立即抓拍”按钮）。
      1) 被锁定（连续 2 次未取得有效采集）→ 423 + 剩余秒数，前端提示“设备离线，抓拍已锁定”；
      2) 设备未联机 → 如实报失败并计入连续失败（绝不下发一个假任务让界面糊弄过去）；
      3) 上一次采集还没结束 → 409 busy，保证“每次抓拍 = 一次真实硬件采集”；
    每次下发都生成/采用一个 request_id，并与板端回传图片在台账里逐条对应。
    """
    d = request.get_json(silent=True) or {}
    source = (d.get("source") or request.args.get('source') or 'web').strip() or 'web'
    rid = (d.get("request_id") or "").strip() or _mint_capture_id()
    # week9: 本次采集若是【主动询问】建议的重拍，则记录来源（溯源，绝不与其它采集串台）
    retake_of = (d.get("retake_of") or "").strip() or None
    prompt_id = (d.get("prompt_id") or "").strip() or None

    gate = _capture_gate_state()
    if gate["locked"]:
        print(f"【抓拍锁定】拒绝下发（剩余 {gate['locked_remaining']:.0f}s）request_id={rid}")
        return jsonify({"status": "locked", "ok": False, "request_id": rid,
                        "locked": True, "retry_after": gate["locked_remaining"],
                        "lock_seconds": gate["lock_seconds"],
                        "msg": "设备离线，抓拍已锁定",
                        "capture": _capture_public()}), 423

    with lock:
        ip, seen = device_info["ip"], device_info["last_seen"]
    online = bool(ip and seen and (time.time() - seen) < 60)
    if not online:
        detail = "设备未联机（最近一次心跳超过 60s 或从未连接），未下发指令"
        with lock:
            task_state.update({"task_id": None, "status": "FAIL", "fail_reason": "offline",
                               "request_id": rid, "trigger_source": source,
                               "latest_photo": None, "deadline": None,
                               "result": None, "last_update": time.time()})
        _capture_failure("offline", detail, rid, None, source,
                         int(time.time() * 1000), unusable=True)
        fail_msg = f"❌ 采集失败：设备未联机（{detail}）"
        gate2 = _capture_gate_state()
        if gate2["locked"]:
            # 本次失败刚好触发第 2 次连续失败 → 同一条响应里就把锁定状态交给前端
            print(f"【抓拍锁定】失败次数达 {COLLECT_LOCK_FAILS} → 立即锁定并通知前端")
            return jsonify({"status": "locked", "ok": False, "request_id": rid,
                            "locked": True, "retry_after": gate2["locked_remaining"],
                            "lock_seconds": gate2["lock_seconds"],
                            "msg": "设备离线，抓拍已锁定", "error_msg": fail_msg,
                            "capture": _capture_public()}), 423
        return jsonify({"status": "error", "ok": False, "request_id": rid,
                        "msg": fail_msg,
                        "capture": _capture_public()}), 200

    ok, tid = create_capture_task(source, rid, retake_of=retake_of, prompt_id=prompt_id)
    if not ok:
        return jsonify({"status": "busy", "ok": False, "request_id": rid,
                        "msg": "⏳ 上一次采集尚未结束，请稍候",
                        "capture": _capture_public()}), 409

    return jsonify({"status": "ok", "ok": True, "task_id": tid, "request_id": rid,
                    "state": "PENDING", "timeout_s": CAPTURE_TIMEOUT_SECONDS,
                    "retake_of": retake_of, "prompt_id": prompt_id,
                    "capture": _capture_public()}), 200

@app.route('/api/device/ack', methods=['POST'])
def device_ack():
    with lock:
        task_state["ack_needed"] = False
        print("【ACK确认】用户已确认物理按键拍照")
    return jsonify({"status": "ok"}), 200

@app.route('/api/device/poll', methods=['GET'])
def device_poll():
    touch_device()
    with lock:
        if task_state["status"] == "PENDING":
            task_state["status"] = "ACCEPTED"
            task_state["accepted_at"] = time.time()
            task_state["last_update"] = time.time()
            print(f"【设备接收】ESP32 拉取到拍照指令 (ACCEPTED) "
                  f"request_id={task_state.get('request_id') or '-'}")
            return jsonify({"has_task": True, "task_id": task_state["task_id"],
                            "request_id": task_state.get("request_id")}), 200
        return jsonify({"has_task": False}), 200

@app.route('/api/device/upload_photo', methods=['POST'])
def upload_photo():
    """板端采集回传入口。week7：把图片与本次 request_id 强关联后写台账（毫秒时间戳），
    并做“镜头遮挡/画面不可用”初判；任何异常分支都记一次失败（同样带 request_id），
    绝不把上一张旧图当成这一次的采集结果。"""
    touch_device()          # 先更新心跳：即使本次上传无效，在线状态也是真实的
    now = time.time()
    with lock:
        active = task_state["status"] in ("PENDING", "ACCEPTED", "COLLECTING")
        active_request_id = task_state.get("request_id")
        active_task_id = task_state.get("task_id")
        active_trigger = task_state.get("trigger_source") or "web"
        # week9: 本次上传若是主动询问建议的重拍 → 把来源写进台账（溯源，防串台）
        active_retake_of = task_state.get("retake_of") if active else None
        active_prompt_id = task_state.get("prompt_id") if active else None

    trigger = request.form.get('trigger', 'web') or 'web'
    if trigger == 'web' and active and active_trigger in ('timer', 'ai_chat'):
        trigger = active_trigger        # 板端回显默认值时不丢失调度归因
    # 板端主动上传（物理按键 / BLE，无云端任务）也要有可溯源的 request_id
    rid = active_request_id if active else _mint_capture_id("hw")
    tid = active_task_id if active else None

    # ---- week10: 板端若随本次上传报了“拍摄/采集时刻”，先解析出来 ----
    # 失败分支也如实记录（采集时间只来自板端上报，没上报就是 None，绝不用接收时间冒充）
    collected_ms = _parse_ms_field(request.form.get('collected_at_ms')
                                   or request.form.get('captured_at_ms'))

    if 'image' not in request.files:
        _capture_failure("device_error", "设备返回错误：缺少 image 分片", rid, tid, trigger,
                         int(now * 1000), unusable=True, collected_ms=collected_ms)
        return jsonify({"error": "No image part", "request_id": rid, "blocked": True,
                        "unusable": True}), 400
    file = request.files['image']
    if file.filename == '':
        _capture_failure("device_error", "设备返回错误：文件名称为空", rid, tid, trigger,
                         int(now * 1000), unusable=True, collected_ms=collected_ms)
        return jsonify({"error": "No selected file", "request_id": rid, "blocked": True,
                        "unusable": True}), 400

    # Read and validate file size BEFORE saving
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size < MIN_PHOTO_SIZE:
        detail = f"设备返回错误：图像仅 {file_size} 字节（<{MIN_PHOTO_SIZE}B，空帧/镜头遮挡）"
        print(f"【告警】拒绝保存无效文件: {file.filename}, 大小仅 {file_size} 字节 (阈值 {MIN_PHOTO_SIZE})")
        _capture_failure("empty_image", detail, rid, tid, trigger, int(now * 1000),
                         unusable=True, collected_ms=collected_ms)
        return jsonify({"error": f"File too small ({file_size} bytes), likely corrupt",
                        "request_id": rid, "blocked": True, "unusable": True,
                        "detail": detail}), 400

    # Validate JPEG magic bytes (FF D8 FF)
    header = file.read(3)
    file.seek(0)
    if header[:2] != b'\xff\xd8':
        detail = f"设备返回错误：非 JPEG 数据（头部 {header.hex()}）"
        print(f"【告警】非JPEG文件，拒绝保存: {file.filename}, 头部: {header.hex()}")
        _capture_failure("device_error", detail, rid, tid, trigger, int(now * 1000),
                         unusable=True, collected_ms=collected_ms)
        return jsonify({"error": "Not a valid JPEG image",
                        "request_id": rid, "blocked": True, "unusable": True,
                        "detail": detail}), 400

    # 毫秒级文件名：同一秒内连续两次采集不会互相覆盖（旧命名 photo_<秒>.jpg 会撞名）
    ms = int(time.time() * 1000)                      # 服务器接收时间（week10: received_at 基准）
    filename = f"photo_{ms}.jpg"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    actual_size = os.path.getsize(filepath)

    # ---- week10: collected_ms 已在上文由 _parse_ms_field 解析（板端未上报则为 None） ----
    if collected_ms and not (0 < ms - collected_ms < 3600_000):
        print(f"【时间戳告警】板端采集时间与服务器时间差过大（{ms - collected_ms}ms）→ 丢弃该上报")
        collected_ms = None

    # ---- week7 要求 3：遮挡/画面不可用初判（<500B 或 偏黑=平均灰度 < 40）----
    # 小于 500B 已在上面拦下（unusable=True，前端显示黄色遮挡告警）；这里补“偏黑/遮挡”判定。
    # Bug1 fix: 原阈值 12 会漏判“遮住镜头”的黑图（实测 13.5 / 16.97），现放宽到 40。
    avg_gray, dark_ratio = photo_avg_gray(filepath)
    blocked = (actual_size < BLOCKED_MIN_SIZE) or (avg_gray is not None
                                                   and avg_gray < BLOCKED_MAX_GRAY)
    detail = None
    if blocked:
        detail = (f"画面可能被遮挡：平均灰度仅 {avg_gray}/255（阈值 {BLOCKED_MAX_GRAY:.0f}）"
                  if avg_gray is not None else f"画面可能被遮挡：文件仅 {actual_size} 字节")
        print(f"【遮挡告警】{filename} 判定画面不可用（{detail}，暗像素占比={dark_ratio}）")

    with lock:
        task_state.update({
            "latest_photo": filename,
            "status": "COMPLETED",
            "trigger_source": trigger,
            "ack_needed": (trigger == "physical_button"),
            "last_update": time.time(),
            "request_id": rid,
            "fail_reason": None,
            "result": {"photo": filename, "size": actual_size, "blocked": blocked,
                       "avg_gray": avg_gray, "dark_ratio": dark_ratio,
                       "ts_ms": ms, "detail": detail},
        })
        print(f"【任务完成】收到设备照片: {filename} ({actual_size} bytes, "
              f"触发源: {trigger}, request_id: {rid})")

    # 图片与请求强关联落台账（含毫秒时间戳 + 遮挡判定）
    # week10: 三种时间 + 本次位置观测（同一次采集才有位置；否则 no_fix + 历史位置另存）
    loc_fix, loc_note, loc_public = _observation_link(None, request_id=rid, received_ms=ms)
    entry = ledger_add_attempt(rid, tid, trigger, ok=True, name=filename, size=actual_size,
                               blocked=blocked, avg_gray=avg_gray, dark_ratio=dark_ratio,
                               detail=detail, ts_ms=ms, state="COMPLETED",
                               retake_of=active_retake_of, prompt_id=active_prompt_id,
                               collected_ms=collected_ms, received_ms=ms,
                               location_fix=loc_fix, location_note=loc_note, location=loc_public)
    global camera_last_success_ms
    with sensor_lock:
        camera_last_success_ms = ms           # week10: 摄像头源“最近一次成功画面”的接收时间

    # ---- week8: 采集成功即自动做一次真实视觉推理（毫秒级），并把结果写回台账 ----
    # 这样画廊/采集面板无需额外请求就能显示可理解的反馈；前端仍会调用 /api/infer_image（命中缓存）。
    try:
        infer_photo_entry(entry, source="auto")
    except Exception as e:
        print(f"【视觉推理】自动推理失败（不影响采集结果）: {e}")

    # 一次有效采集 → 连续失败计数清零（不会误触发 30s 锁定）
    with capture_lock:
        if capture_gate["consecutive_fails"]:
            print(f"【采集恢复】有效采集成功 → 连续失败计数清零"
                  f"（原 {capture_gate['consecutive_fails']} 次）")
        capture_gate["consecutive_fails"] = 0

    # Clean up old files: keep only last 50 photos
    cleanup_old_photos(50)

    return jsonify({"status": "ok", "filename": filename, "request_id": rid,
                    "ts_ms": ms, "blocked": blocked,
                    # week7 fix(Bug1): blocked / unusable 两个字段都给出，前端任一命中即弹黄色遮挡告警
                    "unusable": blocked, "avg_gray": avg_gray,
                    "dark_ratio": dark_ratio, "detail": detail,
                    "threshold": BLOCKED_MAX_GRAY,
                    # ---- week10: 三种时间 + 本次位置观测（no_fix 时如实标注，绝不顶替） ----
                    "collected_at_ms": collected_ms,
                    "received_at_ms": ms,
                    "received_at": _ms_str(ms),
                    "upload_latency_ms": (ms - collected_ms) if collected_ms else None,
                    "timing_source": {"collected_at": "device" if collected_ms else "unknown",
                                      "received_at": "server"},
                    "location_fix": loc_fix,
                    "location_note": loc_note,
                    "location": loc_public}), 200

def cleanup_old_photos(max_keep=50):
    """Remove oldest photos beyond max_keep and mark them cleaned in ledger."""
    files = sorted(glob.glob(os.path.join(UPLOAD_FOLDER, 'photo_*.jpg')), key=os.path.getmtime)
    removed = []
    while len(files) > max_keep:
        old = files.pop(0)
        try:
            os.remove(old)
            removed.append(os.path.basename(old))
        except OSError:
            pass
    if removed:
        ledger_mark_cleaned(set(removed))
        print(f"【清理】删除旧照片: {removed}")

VALID_PHOTO_RE = re.compile(r'^photo_\d+\.jpg$')

@app.route('/api/photos', methods=['GET'])
def list_photos():
    """Rich photo list (stored + cleaned + week7 采集失败记录) for gallery cards."""
    out = []
    with ledger_lock:
        entries = [e for e in ledger
                   if VALID_PHOTO_RE.match(e.get("name") or "")
                   or e.get("status") == "failed"]
    for e in reversed(entries):
        ms = e.get("ts_ms")
        if ms is None:                          # 兼容 week7 之前的台账条目
            ms = int(float(e.get("ts") or 0) * 1000)
        status = e.get("status")
        has_file = bool(e.get("name")) and status == "stored"
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ms / 1000.0))
        out.append({
            "name": e.get("name"),
            "ts": e.get("ts"),
            "ts_ms": ms,                        # 毫秒级时间戳（溯源用）
            "time_str": stamp,
            "time_ms_str": f"{stamp}.{ms % 1000:03d}",   # week7: 画廊显示到毫秒
            "size": e.get("size", 0),
            "status": status,
            "kind": e.get("kind") or ("photo" if status in ("stored", "cleaned") else "failed"),
            "ok": bool(e.get("ok", status in ("stored", "cleaned"))),
            "request_id": e.get("request_id"),   # week7: 本次采集的 request_id
            "task_id": e.get("task_id"),
            "trigger": e.get("trigger") or "web",     # week7: 触发来源
            "blocked": bool(e.get("blocked")),        # week7: 遮挡/画面不可用初判
            "unusable": bool(e.get("unusable")),
            "avg_gray": e.get("avg_gray"),
            "dark_ratio": e.get("dark_ratio"),
            "reason": e.get("reason"),
            "detail": e.get("detail"),
            "state": e.get("state"),
            # ---- week8: 视觉推理结果（score 是置信度，不是准确率；人工纠正优先） ----
            "ai_label": (e.get("ai_label_manual") if e.get("ai_corrected") else e.get("ai_label")),
            "ai_label_raw": e.get("ai_label"),
            "ai_score": e.get("ai_score"),
            "ai_status": ("success" if e.get("ai_corrected") else e.get("ai_status")),
            "ai_msg": e.get("ai_msg"),
            "ai_alt": e.get("ai_alt"),
            "ai_corrected": bool(e.get("ai_corrected")),
            "ai_source": e.get("ai_source"),
            "ai_engine": e.get("ai_engine"),
            "ai_backend": e.get("ai_backend"),
            "ai_calibrated": False,
            "ai_low_conf_threshold": e.get("ai_low_conf_threshold", INFER_LOW_CONF),
            "ai_ts_ms": e.get("ai_ts_ms"),
            "ai_cost_ms": e.get("ai_cost_ms"),
            # ---- week9: 主动询问的用户动作（照片卡片上要显示“用户已忽略重拍建议”） ----
            "user_action": e.get("user_action"),
            "user_action_ts_ms": e.get("user_action_ts_ms"),
            "user_action_prompt_id": e.get("user_action_prompt_id"),
            "prompt_cooldown_until_ms": e.get("prompt_cooldown_until_ms"),
            "retake_of": e.get("retake_of"),          # 本次采集源自哪次采集（主动询问建议的重拍）
            "prompt_id": e.get("prompt_id"),          # 源自哪次主动询问
            # ---- week10: 三种时间（采集/接收/展示）+ 时间差来源 + 本次位置观测 ----
            "collected_at": e.get("collected_at"),
            "collected_at_ms": e.get("collected_at_ms"),
            "collected_at_source": e.get("collected_at_source") or "unknown",
            "received_at": e.get("received_at"),
            "received_at_ms": e.get("received_at_ms", ms),
            "received_at_source": e.get("received_at_source") or "server",
            "displayed_at": e.get("displayed_at"),
            "displayed_at_ms": e.get("displayed_at_ms"),
            "upload_latency_ms": e.get("upload_latency_ms"),
            # 展示延迟：前端刷新展示时间 - 服务器接收时间（解释“我看到的比收到的晚多久”）
            "display_latency_ms": (int(e["displayed_at_ms"]) - int(e.get("received_at_ms") or ms))
            if e.get("displayed_at_ms") else None,
            "timing_note": ("collected_at 来自板端上报；received_at 为服务器接收时间；"
                            "displayed_at 由前端刷新时上报"),
            "location_fix": bool(e.get("location_fix")),
            "location_note": e.get("location_note"),
            "location": {
                "collected_at_ms": e.get("location_collected_at_ms"),
                "received_at_ms": e.get("location_received_at_ms"),
                "latitude": e.get("location_latitude"),
                "longitude": e.get("location_longitude"),
                "satellites": e.get("location_satellites"),
                "request_id": e.get("location_request_id"),
            },
            # ---- week14: 投递状态（本地已保存 ⇄ 远端待接收 / ✅ 已接收 / ⚠️ 未送达）----
            "delivery_status": e.get("delivery_status") or "delivered",
            "delivery_label": e.get("delivery_label") or NET_DELIVERY_LABEL["delivered"],
            "delivery_mode": e.get("delivery_mode") or "legacy_assumed",
            "delivery_stage": e.get("delivery_stage"),
            "delivery_note": e.get("delivery_note"),
            "delivered_at_ms": e.get("delivered_at_ms"),
            "delivered_at": e.get("delivered_at"),
            "queue_id": e.get("queue_id"),
            "queued_at_ms": e.get("queued_at_ms"),
            "queued_at": e.get("queued_at"),
            "pending_upload": ((e.get("delivery_status") or "delivered") == "pending_upload"),
            "delivery_missing_local": ((e.get("delivery_status") or "") == "missing_local"),
            "historical_location": {
                "available": bool(e.get("location_historical_available")),
                "latitude": e.get("location_historical_latitude"),
                "longitude": e.get("location_historical_longitude"),
                "collected_at_ms": e.get("location_historical_collected_at_ms"),
                "note": e.get("location_historical_note"),
            } if e.get("location_historical_available") else None,
            "url": f"/photos/{e['name']}" if has_file else None,
        })
    return jsonify({"photos": out[:60]})


@app.route('/api/infer_image', methods=['POST'])
def infer_image():
    """week8: 对指定（默认最近一次）抓拍图片做真实视觉推理。
    入参 JSON：{"request_id": "cap_..."} 或 {"filename": "photo_....jpg"}，可选 {"force": true} 强制重算。
    返回：{"status": "success"|"low_confidence"|"not_found"|"error", "label", "score", ...}
    最高置信度 < 0.4 → status=low_confidence + msg="无法确定画面内容，请重新采集"。"""
    d = request.get_json(silent=True) or {}
    rid = str(d.get("request_id") or request.args.get("request_id") or "").strip()
    fname = str(d.get("filename") or request.args.get("filename") or "").strip()
    raw_force = d.get("force", request.args.get("force", ""))
    force = str(raw_force).lower() in ("1", "true", "yes", "on")
    if fname and os.path.basename(fname) != fname:
        return jsonify({"status": "error", "msg": "filename 非法（不允许路径）"}), 400
    if fname and not fname.lower().endswith(".jpg"):
        return jsonify({"status": "error", "msg": "只支持 .jpg 图像"}), 400

    entry = ledger_find_entry(request_id=rid or None, filename=fname or None)
    if entry is None and fname:
        path = os.path.join(UPLOAD_FOLDER, fname)
        if os.path.exists(path):                     # 目录里有图但台账缺记录 → 补一条再推理
            entry = ledger_add(fname, os.path.getsize(path), trigger="gallery", request_id=None)
    if entry is None:
        return jsonify({"status": "not_found",
                        "msg": "没有可推理的图片：请先抓拍一次，或传入 filename / request_id"}), 404

    res = infer_photo_entry(entry, force=force, source="api")
    if not res.get("msg"):
        res["msg"] = "无法确定画面内容，请重新采集" if res.get("status") == "low_confidence" else None
    code = 200 if res.get("status") in ("success", "low_confidence") else 404
    return jsonify(res), code


@app.route('/api/correct_label', methods=['POST'])
def correct_label():
    """week8: 人工纠正 AI 标签（未经校准的模型不可能总是对，纠正结果写回台账）。
    入参 JSON：{"label": "暖色环境（室内灯光/木质）", "filename"|"request_id": ..., "note": 可选}
    台账新增字段：ai_label_manual / ai_corrected / ai_source='manual' / ai_correct_ts_ms。"""
    d = request.get_json(silent=True) or {}
    label = str(d.get("label") or d.get("value") or "").strip()
    note = (str(d.get("note") or "").strip()[:120]) or None
    fname = str(d.get("filename") or "").strip()
    rid = str(d.get("request_id") or "").strip()
    if not label:
        return jsonify({"status": "error", "msg": "标签不能为空"}), 400
    if len(label) > 40:
        return jsonify({"status": "error", "msg": "标签过长（≤40 字符）"}), 400
    if fname and os.path.basename(fname) != fname:
        return jsonify({"status": "error", "msg": "filename 非法（不允许路径）"}), 400

    entry = ledger_find_entry(request_id=rid or None, filename=fname or None)
    if entry is None:
        return jsonify({"status": "not_found", "msg": "找不到对应的图片记录"}), 404
    ledger_save_ai(entry, ai_label_manual=label, ai_corrected=True, ai_source="manual",
                   ai_correct_ts_ms=int(time.time() * 1000), ai_manual_note=note)
    # week12: 记住这次人工纠正（用户纠正过一次，就别再拿模型结论去打扰他）
    pref_remember("corrected_label",
                  "用户纠正了 AI 标签「%s → %s」→ 系统记住：这张画面以人工标签为准"
                  % (entry.get("ai_label") or "（拒识）", label),
                  detail="filename=%s · request_id=%s · 来源 manual"
                         % (entry.get("name"), entry.get("request_id") or rid or "-"),
                  request_id=entry.get("request_id") or rid or None, save=True)
    print(f"【人工纠正】{entry.get('name')} 模型原判 "
          f"{entry.get('ai_label') or '(拒识)'} → 人工标签: {label}")
    res = _ai_public(entry)
    res["status"] = "success"
    res["msg"] = "已记录人工修正"
    return jsonify(res), 200

@app.route('/api/prompt_check', methods=['POST'])
def prompt_check():
    """week9: 前端在抓拍完成/状态刷新时询问后端——本次画面是否需要【主动询问重拍】。
    入参 JSON：{"request_id": "cap_...", "filename": "photo_....jpg"(可选),
                "force": true(可选，跳过“同一张画面已询问过”的去重)}
    返回：ask=true/false + reason(blocked / low_confidence / infer_error / no_image /
          ok / cooldown / already_asked / handled) + msg/voice（弹窗与浏览器 TTS 文案）
          + prompt_id（用户动作必须回传它，天然防串台） + 冷却状态（cooldown_*）。
    画面不可用的依据全部来自后端真实数据（week7 遮挡灰度 / week8 置信度 / 空帧），
    前端传来的 score/blocked 一律不采信。"""
    d = request.get_json(silent=True) or {}
    rid = str(d.get("request_id") or request.args.get("request_id") or "").strip()
    fname = str(d.get("filename") or request.args.get("filename") or "").strip()
    if fname and os.path.basename(fname) != fname:
        return jsonify({"status": "error", "msg": "filename 非法（不允许路径）"}), 400
    raw_force = d.get("force", request.args.get("force", ""))
    force = str(raw_force).lower() in ("1", "true", "yes", "on")
    if force and rid:
        with prompt_lock:                 # 用户手动触发检查 → 清掉该 request_id 的“已询问”去重标记
            prompt_gate["asked"].pop(rid, None)
    res = evaluate_proactive_prompt(request_id=rid or None, filename=fname or None,
                                    register=True, source="api")
    res["forced"] = force
    return jsonify(res), 200


@app.route('/api/prompt_action', methods=['POST'])
def prompt_action():
    """week9: 用户对【主动询问】的动作：confirmed_retake（🔄 确认重拍）/ ignored_retake（❌ 忽略）。
    入参 JSON：{"action": "...", "prompt_id": "pr_...", "request_id": "cap_...", "filename": 可选}
    动作写回 photos_ledger.json（user_action / user_action_ts_ms / prompt_cooldown_*）；
    忽略 → 启动 PROMPT_COOLDOWN_SECONDS(30s) 静默期，期间画面不可用也不弹窗（仅后台记录）。
    prompt_id 与 request_id 不匹配 → 409 过期（防串台）。"""
    d = request.get_json(silent=True) or {}
    payload, code = submit_prompt_action(
        d.get("action") or request.args.get("action"),
        prompt_id=d.get("prompt_id") or request.args.get("prompt_id"),
        request_id=d.get("request_id") or request.args.get("request_id"),
        filename=d.get("filename") or request.args.get("filename"),
        note=d.get("note"), source=(d.get("source") or "front"))
    return jsonify(payload), code


@app.route('/api/status', methods=['GET'])
def get_status():
    check_timeout()
    with lock:
        data = dict(task_state)
        data["device_ip"] = device_info["ip"]
        data["device_last_seen"] = device_info["last_seen"]
        data["rssi"] = device_info["rssi"]
    # week7: 单张采集状态机快照（request_id / 剩余超时 / 遮挡判定 / 30s 锁定）
    cap = _capture_public()
    data["capture"] = cap
    data["capture_request_id"] = cap["request_id"]
    data["capture_locked"] = cap["locked"]
    data["capture_lock_remaining"] = cap["lock_remaining"]
    data["capture_blocked"] = cap["blocked"]
    data["capture_unusable"] = cap["unusable"]
    data["capture_photo"] = cap["photo"]
    # week7 fix(Bug2): 设备在线判定（以板端心跳为准）→ 前端离线时冻结 IMU 曲线、停抓拍提示
    online = device_online()
    data["device_online"] = online
    data["device_offline"] = (not online)
    data["device_seen_age"] = device_seen_age()
    data["device_online_window"] = DEVICE_ONLINE_WINDOW
    # ---- week10: 多源独立状态 + 数据新鲜度（陈旧/离线阈值）+ 三种时间戳 + 观测快照 ----
    obs = _observation_public()
    data["observation"] = obs
    data["imu_state"] = obs["imu_state"]              # 任务书：{"status","age_sec","last_time",...}
    data["camera_state"] = obs["camera_state"]        # 任务书：{"status":"ready/capturing/fail",...}
    data["location_state"] = obs["location_state"]    # 定位源独立状态（no_fix 严格区分）
    data["device_state"] = obs["device_state"]
    data["sensor_states"] = {                         # 一屏可读的“谁鲜活、谁陈旧”
        "imu": obs["imu_state"]["status"],
        "camera": obs["camera_state"]["status"],
        "location": obs["location_state"]["status"],
        "device": "online" if obs["device_state"]["online"] else "offline",
    }
    data["freshness_policy"] = obs["policy"]          # 5s stale / 10s offline 等阈值与文案
    data["network_but_no_data"] = obs["network_but_no_data"]   # 网络连通但传感器无新数据
    data["timing"] = obs["timing"]                    # 三种时间的含义（来源标注）
    data["display"] = obs["display"]                  # 前端展示刷新时间（displayed_at）回执
    data["server_time"] = obs["server_time"]
    data["server_time_ms"] = obs["server_time_ms"]
    # ---- week8: 本次采集的视觉推理结果 + 可解释标签集（前端“人工纠正”下拉框直接复用）----
    ai_entry = None
    if cap.get("photo"):
        ai_entry = ledger_find_entry(filename=cap["photo"])
    if ai_entry is None and cap.get("request_id"):
        ai_entry = ledger_find_entry(request_id=cap["request_id"])
    data["ai"] = _ai_public(ai_entry)
    data["ai_labels"] = list(AI_LABELS)
    data["ai_engine"] = INFER_ENGINE
    data["ai_low_conf_threshold"] = INFER_LOW_CONF
    # ---- week9: 主动询问决策（只读快照，register=False 不改变门控计数）+ 冷却状态 + 策略 ----
    data["prompt"] = evaluate_proactive_prompt(request_id=cap.get("request_id"),
                                               filename=cap.get("photo"),
                                               register=False, source="status")
    data["prompt_state"] = _prompt_state()
    data["prompt_policy"] = {
        "cooldown_seconds": PROMPT_COOLDOWN_SECONDS,     # 用户忽略后的静默期
        "reask_seconds": PROMPT_REASK_SECONDS,           # 同一张画面不重复弹窗的窗口
        "low_conf_threshold": INFER_LOW_CONF,            # 置信度拒识阈值（<0.4 → 询问重拍）
        "blocked_max_gray": BLOCKED_MAX_GRAY,            # 遮挡初判阈值（灰度 < 40 → 画面不可用）
        "blocked_min_size": BLOCKED_MIN_SIZE,            # 空帧阈值（<500B）
        "voice": PROMPT_VOICE_TEXT,                      # 浏览器 TTS 播报文案
        "actions": ["confirmed_retake", "ignored_retake"],
    }
    # ---- week12: 用户交互提醒偏好（轻量快照；完整日志 / 版本历史走 /api/preferences）----
    data["preferences"] = pref_public(compact=True)
    data["preference_policy"] = {
        "default_preference": PREF_DEFAULT,              # 默认偏好（= v1 配置）
        "default_config_version": PREF_VERSION_DEFAULT,
        "choices": list(PREF_ORDER),
        "endpoint": "/api/preferences",
        "note": "偏好只改变“要不要打扰用户”（弹窗 / 追问），不改变任何传感器判定；每次修改都会产生新的 config_version。",
    }
    # ---- week14: 网络与补传（轻量快照；完整队列明细 / 事件日志走 /api/network/status）----
    data["network"] = net_public(compact=True)
    data["network_policy"] = net_policy()
    with timer_lock:
        data["timer_running"] = timer_state["running"]
        data["timer_current_batch"] = timer_state["current_batch"]
        data["timer_max_batches"] = timer_state["max_batches"]
        data["timer_interval"] = timer_state["interval"]
        data["timer_last_result"] = timer_state["last_result"]
    return jsonify(data)

@app.route('/api/observation', methods=['GET', 'POST'])
def observation():
    """week10: “原始数据观测对照”面板的唯一数据源。

    GET /api/observation[?request_id=..&filename=..]     → 观测快照 + 指定照片的三时间对照
    POST /api/observation {request_id, filename, displayed_at_ms}
         → 记录 displayed_at（前端展示刷新时间）后返回同一份对照

    返回里的 photo 字段逐项标注每个时间差的来源：
      collected_at  （板端采集，可能为 null）
      received_at   （服务器接收，必有）
      displayed_at  （浏览器刷新展示，上报后才有）
      upload_latency_ms  = received_at - collected_at     （传输/入库延迟，采集时间缺失时为 null）
      display_latency_ms = displayed_at - received_at     （“我看到的”比“服务器收到的”晚多久）
      skew_ms            = 浏览器时间 - 服务器时间        （时钟偏差，用于解释上面两个差值）
    定位一律走 no_fix 规则：本次没有位置时 location.fix=false + note 明说，历史位置另存。
    """
    d = request.get_json(silent=True) or {}
    rid = str(d.get("request_id") or request.args.get("request_id") or "").strip()
    fname = str(d.get("filename") or request.args.get("filename") or "").strip()
    if fname and os.path.basename(fname) != fname:
        return jsonify({"status": "error", "msg": "filename 非法（不允许路径）"}), 400
    raw_disp = d.get("displayed_at_ms", request.args.get("displayed_at_ms"))
    displayed_ms = None
    if raw_disp:
        try:
            displayed_ms = int(float(raw_disp))
        except (TypeError, ValueError):
            displayed_ms = None
    if displayed_ms:
        _record_display_ack(request_id=rid or None, photo=fname or None,
                            source="observation", displayed_at_ms=displayed_ms,
                            client_time_ms=d.get("client_time_ms"))
    obs = _observation_public()
    entry = ledger_find_any(request_id=rid or None, filename=fname or None) if (rid or fname) \
        else ledger_find_any()
    obs["photo"] = _photo_observation(entry, obs)
    return jsonify(obs), 200


@app.route('/api/display_ack', methods=['POST'])
def display_ack():
    """week10: 前端每次刷新展示后回报 displayed_at（三种时间里的第三种）。

    入参 JSON：{"request_id": .., "filename": .., "displayed_at_ms": 浏览器时间(ms),
                "client_time_ms": 可选（浏览器本地时钟，用于算时钟偏差）,
                "target": "imu"|"observation"|"status"（默认 observation）}
    写回规则：能定位到台账记录（photo_* / 采集失败记录都可以）就只回填
    displayed_at* 三个字段，其它字段（采集/接收/位置/推理）一律不动。"""
    d = request.get_json(silent=True) or {}
    rid = str(d.get("request_id") or request.args.get("request_id") or "").strip()
    fname = str(d.get("filename") or request.args.get("filename") or "").strip()
    target = str(d.get("target") or "observation").strip() or "observation"
    if fname and os.path.basename(fname) != fname:
        return jsonify({"status": "error", "msg": "filename 非法（不允许路径）"}), 400
    raw_disp = d.get("displayed_at_ms")
    displayed_ms = None
    if raw_disp:
        try:
            displayed_ms = int(float(raw_disp))
        except (TypeError, ValueError):
            displayed_ms = None
    if displayed_ms is None:                  # 前端没带就用服务器现在（并标注来源）
        displayed_ms = int(time.time() * 1000)
    ack = _record_display_ack(request_id=rid or None, photo=fname or None, source=target,
                              displayed_at_ms=displayed_ms, client_time_ms=d.get("client_time_ms"))
    entry = ledger_find_any(request_id=rid or None, filename=fname or None) if (rid or fname) else None
    if entry:
        ledger_save_ai(entry, displayed_at_ms=displayed_ms, displayed_at=_ms_str(displayed_ms),
                       displayed_at_source="front")
    return jsonify({"status": "ok", "display": ack,
                    "photo": (entry or {}).get("name"),
                    "request_id": (entry or {}).get("request_id") or (rid or None),
                    "written": bool(entry)}), 200


@app.route('/api/device/upload_location', methods=['POST'])
def upload_location():
    """week10: 板端上报一条定位观测（可选接口：固件没有定位能力就不调用）。

    入参 JSON：{"fix": true/false, "latitude":.., "longitude":.., "satellites":..,
                "collected_at_ms": 采集时刻(ms|秒), "request_id": 绑定哪次采集,
                "fail": true, "detail": ".."}
    语义（边界用例①）：
      * fix=true  → 本次有位置（带 request_id 时只绑定这一次采集）；
      * fix=false → 本次【无位置数据】，历史位置不回填，只留在 historical 里；
      * fail=true → 定位模块故障（location_state=fail）。"""
    d = request.get_json(silent=True) or {}
    touch_device()                    # 板端上报定位同样算设备心跳
    raw_fail = d.get("fail")
    fail = raw_fail is not None and str(raw_fail).lower() not in ("", "0", "false", "none")
    if fail:
        _record_location({"fix": False, "fail": True,
                          "detail": str(d.get("detail") or d.get("msg") or "定位模块无响应"),
                          "request_id": str(d.get("request_id") or "").strip() or None})
        return jsonify({"status": "ok", "action": "fail",
                        "location_state": _location_state()}), 200
    fix = bool(d.get("fix", d.get("has_fix", d.get("valid", False))))
    collected_ms = _parse_ms_field(d.get("collected_at_ms", d.get("ts_ms")))
    lat = d.get("latitude", d.get("lat"))
    lon = d.get("longitude", d.get("lon", d.get("lng")))
    if fix and (lat is None or lon is None):
        return jsonify({"status": "error",
                        "msg": "fix=true 时必须带 latitude/longitude"}), 400
    _record_location({
        "fix": fix,
        "fail": False,
        "collected_at_ms": collected_ms,
        "latitude": None if lat is None else float(lat),
        "longitude": None if lon is None else float(lon),
        "satellites": d.get("satellites", d.get("sats")),
        "accuracy_m": d.get("accuracy_m"),
        "request_id": str(d.get("request_id") or "").strip() or None,
    })
    return jsonify({"status": "ok", "action": "fix" if fix else "no_fix",
                    "location_state": _location_state(),
                    "note": None if fix else OBSERVATION_NO_FIX_NOTE}), 200


@app.route('/photos/<filename>')
def get_photo(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ---------------- timer scheduler (cloud-side auto capture) ----------------
timer_lock = threading.Lock()
timer_state = {
    "running": False,
    "interval": 60.0,
    "max_batches": 3,
    "current_batch": 0,   # batches already dispatched by the scheduler
    "last_result": None,  # "completed" | "stopped" | "error: ..."
}
_timer_stop_evt = None

def create_capture_task(source, request_id=None, retake_of=None, prompt_id=None):
    """Shared task creation (web button / timer scheduler / AI chat).
    Refuses when board is still busy so a task id is never overwritten.
    week7: 每次下发绑定 request_id + 毫秒时间戳 + 10s 硬截止；先把 latest_photo 清空，
    避免上一张旧图被误当成这一次的采集结果。
    week9: retake_of/prompt_id 记录“本次重拍源自哪次主动询问”，用于溯源与防串台。"""
    with lock:
        if task_state["status"] in ("PENDING", "ACCEPTED", "COLLECTING"):
            return False, "busy"
        now = time.time()
        tid = str(int(now))
        rid = (request_id or "").strip() or _mint_capture_id()
        task_state.update({
            "task_id": tid,
            "status": "PENDING",
            "trigger_source": source,
            "ack_needed": False,
            "last_update": now,
            "request_id": rid,                 # week7: 本次采集与 request_id 强关联
            "created_at_ms": int(now * 1000),  # week7: 毫秒级下发时间戳
            "accepted_at": None,
            "deadline": now + CAPTURE_TIMEOUT_SECONDS,
            "fail_reason": None,
            "result": None,
            "latest_photo": None,              # 清空旧图 → 前端不可能拿到上一张糊弄
            "retake_of": retake_of or None,    # week9: 主动询问建议的重拍来源
            "prompt_id": prompt_id or None,
        })
        print(f"【任务创建】下发拍照指令，任务ID: {tid}，触发源: {source}，"
              f"request_id: {rid}（{CAPTURE_TIMEOUT_SECONDS:.0f}s 内必须收到图片）"
              + (f"，重拍来源: retake_of={retake_of}" if retake_of else ""))
    return True, tid

def timer_scheduler_loop(interval, max_batches, stop_evt):
    """Every `interval` seconds auto-dispatch one capture task.
    Busy board (PENDING/ACCEPTED) => skip this tick without touching task id.
    After the last batch finishes (COMPLETED/TIMEOUT) the timer self-stops."""
    try:
        while not stop_evt.is_set():
            if stop_evt.wait(interval):
                break  # stopped by user
            with lock:
                st = task_state["status"]
            if st in ("PENDING", "ACCEPTED", "COLLECTING"):
                print("【定时器】板端仍在处理上一任务，本轮跳过（不覆盖任务ID）")
                continue
            ok, tid = create_capture_task("timer")
            if not ok:
                continue
            with timer_lock:
                timer_state["current_batch"] += 1
                batch = timer_state["current_batch"]
            print(f"【定时器】第 {batch}/{max_batches} 批次已下发, task {tid}")
            if batch >= max_batches:
                # wait for the final task to finish before declaring completion
                deadline = time.time() + CAPTURE_TIMEOUT_SECONDS + 5
                while not stop_evt.is_set() and time.time() < deadline:
                    with lock:
                        st = task_state["status"]
                    if st in ("COMPLETED", "TIMEOUT", "FAIL"):
                        break
                    stop_evt.wait(0.5)
                result = "stopped" if stop_evt.is_set() else "completed"
                with timer_lock:
                    timer_state["running"] = False
                    timer_state["last_result"] = result
                print(f"【定时器】已达批次上限 {max_batches}，定时器自动停止 ({result})")
                return
    except Exception as e:
        print(f"【定时器异常】调度线程出错: {e}")
        with timer_lock:
            timer_state["last_result"] = f"error: {e}"
    finally:
        with timer_lock:
            timer_state["running"] = False

@app.route('/api/timer/start', methods=['POST'])
def timer_start():
    global _timer_stop_evt
    d = request.get_json(silent=True) or {}
    try:
        interval = float(d.get("interval", 60))
        max_batches = int(d.get("max_batches", 3))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "msg": "参数格式错误"}), 400
    interval = min(max(interval, 5.0), 3600.0)   # clamp 5s..3600s
    max_batches = min(max(max_batches, 1), 100)  # clamp 1..100
    with timer_lock:
        if timer_state["running"]:
            return jsonify({"status": "error", "msg": "定时器已在运行"}), 409
        timer_state.update({"running": True, "interval": interval,
                            "max_batches": max_batches, "current_batch": 0,
                            "last_result": None})
        evt = threading.Event()
        _timer_stop_evt = evt
    threading.Thread(target=timer_scheduler_loop,
                     args=(interval, max_batches, evt), daemon=True).start()
    print(f"【定时器】已启动: 每 {interval}s 一次, 上限 {max_batches} 批")
    return jsonify({"status": "ok", "interval": interval, "max_batches": max_batches})

@app.route('/api/timer/stop', methods=['POST'])
def timer_stop():
    with timer_lock:
        was_running = timer_state["running"]
        timer_state["running"] = False
        if was_running and timer_state["last_result"] is None:
            timer_state["last_result"] = "stopped"
        evt = _timer_stop_evt
    if evt is not None:
        evt.set()
    if was_running:
        print("【定时器】已被手动停用")
    return jsonify({"status": "ok", "was_running": was_running})

# ---------------- API: IMU ----------------
@app.route('/api/imu_data', methods=['GET'])
def imu_data():
    """实时 IMU 曲线数据。
    week7 fix(Bug2): 附带在线判定字段；设备离线时只返回板端真实采样（不含任何模拟点），
    前端据此把波形冻结在最后一帧并显示“设备离线，无实时数据”。

    week10: 每条采样都带三种时间（collected_at / received_at / displayed_at），并附带
    imu_state（online/stale/offline/no_data/fail + age_sec）——前端据此给“数据延迟”上色，
    绝不允许把陈旧数据当成当前状态展示。"""
    online = device_online()
    with imu_lock:
        hist = list(imu_history)[-60:]
    if not online:
        hist = [s for s in hist if s.get("source") == "device"]   # 离线：剥离模拟点，避免“假实时”
    disp = _display_public()
    for s in hist:                       # 逐条补齐三种时间的显式字段（缺的就是 null）
        s["collected_at_ms"] = s.get("collected_at_ms")
        s.setdefault("collected_at", None)
        s["received_at_ms"] = s.get("received_at_ms") or (int(float(s.get("t") or 0) * 1000))
        s["received_at"] = _ms_str(s["received_at_ms"])       # 与 gallery 同一格式（前端直接显示）
        s["displayed_at_ms"] = disp.get("displayed_at_ms")
        s["displayed_at"] = disp.get("displayed_at")
    latest = hist[-1] if hist else None
    imu_now = _imu_state()
    return jsonify({"latest": latest, "history": hist,
                    "source": (latest or {}).get("source", "device" if not online else "simulated"),
                    "device_online": online,
                    "offline": (not online),
                    "stale": (not online),
                    "frozen": (not online) and latest is not None,   # 有最后一帧真实数据 → 前端冻结
                    "frozen_at": (latest or {}).get("t"),
                    "device_seen_age": device_seen_age(),
                    # ---- week10: 独立状态 + 数据新鲜度 + 三种时间说明 ----
                    "imu_state": imu_now,
                    "age_sec": imu_now["age_sec"],                   # 数据延迟（秒），只增不减
                    "freshness": ("online" if imu_now["status"] == "online" else
                                  "stale" if imu_now["status"] == "stale" else "offline"),
                    "stale_threshold_sec": IMU_STALE_SECONDS,        # > 5s → 黄底“数据陈旧”
                    "offline_threshold_sec": IMU_OFFLINE_SECONDS,    # > 10s → 红底“数据丢失/设备离线”
                    "network_but_no_data": _sensor_role_hints(online, imu_now),
                    "timing": {
                        "collected_at": "传感器采集时间（板端上报才有，否则 null）",
                        "received_at": "服务器接收时间",
                        "displayed_at": "前端展示刷新时间（/api/display_ack 上报）",
                    },
                    "display": disp,
                    "server_time_ms": int(time.time() * 1000)})

@app.route('/api/device/upload_imu', methods=['POST'])
def upload_imu():
    """Real IMU samples from board firmware (QMA6100P, m/s^2).
    Accepts {"x","y","z"} (firmware v3 payload) or {"ax","ay","az"} (legacy).
    Real data wins for 5 s windows: /api/imu_data reports source='device'.

    week10 追加（全部可选，板端不报就是 null，绝不猜测）：
      * {"collected_at_ms": <ms|秒>} / {"captured_at_ms"} → 传感器采集时间（collected_at）；
      * {"fail": true, "detail": "..."}                    → 板端上报 IMU 故障（imu_state=fail）；
      * {"fail": false}                                    → 故障恢复。
    服务器接收时间（received_at）始终由本接口自己打点，是权威值。"""
    global imu_last_real
    d = request.get_json(silent=True) or {}
    raw_fail = d.get("fail", d.get("fault", d.get("error")))
    if raw_fail is not None and str(raw_fail).lower() not in ("", "0", "false", "none"):
        _set_imu_fail(True, str(d.get("detail") or d.get("msg") or "板端上报 IMU 故障"))
        touch_device()
        return jsonify({"status": "ok", "imu_fail": True,
                        "imu_state": _imu_state()}), 200
    if raw_fail is not None and str(raw_fail).lower() in ("0", "false"):
        _set_imu_fail(False)
    try:
        ax = float(d.get("ax", d.get("x", 0)))
        ay = float(d.get("ay", d.get("y", 0)))
        az = float(d.get("az", d.get("z", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "bad payload"}), 400
    # sanity clamp +-30 m/s^2 (~3g, sensor range is +-2g) to protect chart scale
    ax = min(max(ax, -30.0), 30.0)
    ay = min(max(ay, -30.0), 30.0)
    az = min(max(az, -30.0), 30.0)
    received_ms = int(time.time() * 1000)          # week10: 服务器接收时间（权威）
    collected_ms = _parse_ms_field(d.get("collected_at_ms", d.get("captured_at_ms", d.get("ts_ms"))))
    if collected_ms and abs(received_ms - collected_ms) > 3600_000:
        print(f"【时间戳告警】IMU 采集时间与接收时间差过大（{received_ms - collected_ms}ms）→ 丢弃")
        collected_ms = None
    sample = {"t": round(received_ms / 1000.0, 3),
              "ax": round(ax, 3), "ay": round(ay, 3), "az": round(az, 3),
              "source": "device",
              # ---- week10: 三种时间（逐条采样，绝不用接收时间冒充采集时间） ----
              "collected_at_ms": collected_ms,
              "received_at_ms": received_ms}
    with imu_lock:
        imu_history.append(sample)
        imu_last_real = received_ms / 1000.0
    touch_device()
    if imu_fail["active"]:
        _set_imu_fail(False)      # 收到真实采样 → 故障自动解除
    return jsonify({"status": "ok", "received_at_ms": received_ms,
                    "collected_at_ms": collected_ms,
                    "imu_state": _imu_state()}), 200

# ---------------- API: AI Chat with Function Calling ----------------
import urllib.request
import urllib.error

LLM_API_KEY = os.environ.get("MY_IMU_LLM_KEY", os.environ.get("LLM_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4.7-flash")

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_device_status",
            "description": "查询ESP32-S3-EYE开发板的当前状态，包括设备IP、在线状态、WiFi RSSI信号强度、当前任务状态、最近一次拍照时间等。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_photo",
            "description": "向ESP32-S3-EYE开发板发送一次拍照指令。开发板会在下一次轮询时接收指令并拍照上传。注意：如果设备当前离线或任务超时，将如实告知用户。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarify",
            "description": "当用户指令含糊不清、无法确定是要拍照还是查询设备状态时调用（例如\"帮我打张照\"这类被听错/说得不清楚的话）。调用后系统会引导用户重新说明意图：没听清，你是想拍照还是查状态？不确定意图时必须调用本工具，不要猜。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "可选的追问内容，省略时使用默认引导语"}
                },
                "required": [],
            },
        },
    },
]

UNSUPPORTED_KEYWORDS = {
    # English (kept for international users)
    "temperature", "humidity", "pressure", "light", "sound", "distance",
    "proximity", "gas", "co2", "voc",
    # Chinese — must cover all variants users actually type
    "温度", "湿度", "气压", "光照", "声音", "噪声", "噪音", "距离",
    "pm2.5", "PM2.5", "空气质量", "空气", "烟雾", "二氧化碳", "co2", "CO2",
    "心率", "血氧", "gps", "GPS", "定位", "磁场", "陀螺仪", "声音强度",
    "气压计", "气体", "voc", "VOC", "光线",
    # meta: "what sensors do you support?"
    "支持哪些传感器", "支持什么传感器", "有哪些传感器", "有什么传感器",
}
# Keyword-based tool pre‑matching: avoid extra LLM round‑trip for obvious intents
STATUS_KEYWORDS = {"状态", "在线", "离线", "情况", "rssi", "信号", "ip", "设备状态",
                   "当前状态", "任务状态", "最近一次", "最新照片"}
PHOTO_KEYWORDS = {"拍照", "拍张", "拍张照", "拍个照", "拍一张", "拍一下", "拍一",
                  "采集", "照相", "相机", "拍摄", "抓拍", "capture",
                  "照片", "帮我拍", "帮拍", "来一张", "来张",
                  "张照"}

# ---- week6: clarification of unclear / garbled intents ----
# Exact wording required by the spec: guide the user instead of guessing.
CLARIFY_REPLY = "没听清，你是想拍照还是查状态？"

# Garbled action wording (e.g. ASR turns "拍张照" into "打张照"). Checked BEFORE
# the tool keywords so a broken sentence is clarified instead of guessed.
CLARIFY_GARBLED = ("打张照", "打张照片", "打个照", "打照片", "打照", "打张",
                   "弄张照", "找张照", "招张照", "拍个相", "拍相")

# Vague filler that asks for "something" without naming a supported capability.
# Checked AFTER the tool keywords, so "帮我看看状态" still matches get_device_status.
CLARIFY_VAGUE = ("帮我弄", "帮我搞", "帮我整", "帮我做", "弄一下", "搞一下", "整一下",
                 "看一下", "看看", "瞅瞅", "瞧瞧", "来一下", "那个东西", "这玩意儿")


def _match_clarify_keyword(user_msg, words):
    """Return the first vague/garbled keyword present in the message, else None."""
    for w in words:
        if w in user_msg:
            return w
    return None


# ---- account-level rate limiter ----
_last_llm_call_time = 0.0
LLM_MIN_INTERVAL = 5  # seconds minimum between LLM calls (free tier QPS limit)


def _call_llm_with_retry(messages, tools=None, retries=1):
    """Call LLM with retry on 429, return (result, elapsed)."""
    global _last_llm_call_time
    # ---- account-level rate limiter (free tier QPS protection) ----
    wait_needed = LLM_MIN_INTERVAL - (time.time() - _last_llm_call_time)
    if wait_needed > 0:
        print(f"【AI】Rate-limited: need {wait_needed:.1f}s cooldown")
        return {"error": f"RATE_LIMIT:{wait_needed:.1f}"}, 0

    last_error = None
    for attempt in range(retries + 1):
        t0 = time.time()
        result = _call_llm(messages, tools)
        elapsed = time.time() - t0
        if "error" not in result:
            _last_llm_call_time = time.time()
            return result, elapsed
        err = result["error"]
        if "429" in err and attempt < retries:
            print(f"【AI】429 rate-limit, retry in 3s (attempt {attempt+1}/{retries})")
            time.sleep(3)
            last_error = err
            continue
        # Non-retryable or last attempt
        return result, elapsed
    return {"error": last_error}, 0


def _call_llm(messages, tools=None):
    if not LLM_API_KEY:
        return {"error": "LLM_API_KEY not configured. Set LLM_API_KEY environment variable (or DEEPSEEK_API_KEY as fallback)."}
    # strip whitespace from model and API key (env vars may have trailing spaces)
    model = LLM_MODEL.strip()
    key = LLM_API_KEY.strip()
    body = {"model": model, "messages": messages, "max_tokens": 512, "temperature": 0.3}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode("utf-8")
    url = f"{LLM_BASE_URL}/chat/completions"
    key_masked = key[:4] + "****" + key[-4:] if len(key) >= 8 else "***"
    print(f"【AI】POST {url}")
    print(f"【AI】Authorization: Bearer {key_masked}")
    print(f"【AI】Body keys: {list(body.keys())} | model={model} | msgs={len(messages)}")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            elapsed = time.time() - t0
            print(f"【AI】Response HTTP {resp.status} ({elapsed:.1f}s)")
            return result
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"【AI】LLM HTTP {e.code}: {body_text[:300]}")
        return {"error": f"LLM API error {e.code}: {body_text[:200]}"}
    except Exception as e:
        print(f"【AI】LLM request failed: {e}")
        return {"error": str(e)}

def _cancelled_tool_result():
    """Tool result used when the owning request was superseded or stopped."""
    return json.dumps({"success": False, "reason": "任务已取消",
                       "detail": "用户已停止当前任务，拍照指令未执行或已被撤销。"},
                      ensure_ascii=False)


def _execute_tool(func_name, request_id=None):
    """Execute a function call locally and return JSON result string.
    `request_id` makes long-running tools abortable: a superseded/cancelled
    request never fires a hardware action and stops polling immediately."""
    if func_name == "get_device_status":
        check_timeout()
        with lock:
            data = dict(task_state)
            data["device_ip"] = device_info["ip"]
            data["device_last_seen"] = device_info["last_seen"]
            data["rssi"] = device_info["rssi"]
        online = bool(device_info["ip"] and device_info["last_seen"]
                      and (time.time() - device_info["last_seen"] < 60))
        result = {
            "online": online,
            "device_ip": data["device_ip"] or "未知",
            "rssi": data["rssi"],
            "task_status": data.get("status", "IDLE"),
            "task_id": data.get("task_id"),
            "latest_photo": data.get("latest_photo"),
            "last_seen_seconds_ago": round(time.time() - (data["device_last_seen"] or 0), 1)
                if data["device_last_seen"] else None,
        }
        return json.dumps(result, ensure_ascii=False)

    elif func_name == "take_photo":
        # never fire a hardware action for a request that was already dropped
        if not is_current_request(request_id):
            return _cancelled_tool_result()
        with lock:
            ip = device_info["ip"]
            last_seen = device_info["last_seen"]
            busy = task_state["status"] in ("PENDING", "ACCEPTED", "COLLECTING")
        online = bool(ip and last_seen and (time.time() - last_seen < 60))
        if not online:
            return json.dumps({"success": False, "reason": "设备离线",
                "detail": "设备未连接到服务器，无法下发拍照指令。"}, ensure_ascii=False)
        if busy:
            return json.dumps({"success": False, "reason": "设备忙碌",
                "detail": "设备正在处理上一个任务，请稍后再试。"}, ensure_ascii=False)

        ok, tid = create_capture_task("ai_chat")
        if not ok:
            return json.dumps({"success": False, "reason": "任务创建失败",
                "detail": "服务器内部错误，无法创建拍照任务。"}, ensure_ascii=False)

        # Poll until COMPLETED/TIMEOUT/FAIL/OFFLINE/CANCELLED (week7: 采集硬超时 10s)
        deadline = time.time() + CAPTURE_TIMEOUT_SECONDS + 5
        result_status = None
        result_photo = None
        abort_evt = request_abort_event(request_id)
        while time.time() < deadline:
            if abort_evt is not None:
                abort_evt.wait(1.0)      # wakes up immediately when 🛑 is pressed
            else:
                time.sleep(1.0)
            if not is_current_request(request_id):
                result_status = "CANCELLED"; break
            check_timeout()
            with lock:
                st = task_state["status"]
                photo = task_state.get("latest_photo")
            if st in ("COMPLETED", "TIMEOUT", "FAIL"):
                result_status = st; result_photo = photo; break
            with lock:
                ip_now = device_info["ip"]
                ls_now = device_info["last_seen"]
            if not (ip_now and ls_now and (time.time() - ls_now < 60)):
                result_status = "OFFLINE"; break

        if result_status == "COMPLETED":
            with lock:
                meta = dict(task_state.get("result") or {})
                rid_now = task_state.get("request_id")
            return json.dumps({"success": True, "photo": result_photo, "task_id": tid,
                               "request_id": rid_now,
                               "blocked": bool(meta.get("blocked")),      # week7: 遮挡初判
                               "avg_gray": meta.get("avg_gray"),
                               "ts_ms": meta.get("ts_ms")}, ensure_ascii=False)
        elif result_status == "TIMEOUT":
            return json.dumps({"success": False, "reason": "任务超时",
                "detail": f"拍照指令已下发，但设备在{CAPTURE_TIMEOUT_SECONDS:.0f}秒内未完成采集。"}, ensure_ascii=False)
        elif result_status == "FAIL":
            with lock:
                why = task_state.get("fail_reason")
            return json.dumps({"success": False, "reason": "设备返回错误",
                "detail": f"设备上报错误（{why or '未知'}），本次采集未取得有效图像。"}, ensure_ascii=False)
        elif result_status == "CANCELLED":
            return _cancelled_tool_result()
        else:
            return json.dumps({"success": False, "reason": "设备未响应",
                "detail": "设备在接受任务后失去响应。"}, ensure_ascii=False)

    elif func_name == "ask_clarify":
        return json.dumps({"success": False, "reason": "意图不明确",
            "detail": "指令含糊，无法确定是要拍照还是查询设备状态。"}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {func_name}"})


@app.route('/api/chat', methods=['POST'])
def ai_chat():
    """Natural-language AI assistant.
    POST /api/chat {message: str, request_id: str|None}
      -> {reply, tool_used, request_id};  {cancelled: True} when a newer request
         or the 🛑 button invalidated this one (late result is discarded)."""
    try:
        d = request.get_json(silent=True) or {}
        user_msg = (d.get("message", "") or "").strip()
        if not user_msg:
            return jsonify({"reply": "请输入您的问题。", "tool_used": None}), 200
        if len(user_msg) > 500:
            return jsonify({"reply": "输入内容过长，请控制在500字以内。", "tool_used": None}), 200

        # week6: one live request id per task — a new request invalidates all older ids
        request_id = begin_request(d.get("request_id"))
        t0 = time.time()
        result = _do_ai_chat(user_msg, request_id)
        glm_elapsed = time.time() - t0

        if result.get("cancelled"):
            _task_log(request_id, 0.0, glm_elapsed, 0.0, "结果已作废（用户已停止/发起新任务）")
            return jsonify({"cancelled": True, "request_id": request_id,
                            "reply": "", "tool_used": None}), 200

        _task_log(request_id, 0.0, glm_elapsed, 0.0, f"source=text tool={result.get('tool_used') or 'none'}")
        result["request_id"] = request_id
        return jsonify(result), 200
    except Exception as e:
        print(f"【AI】Unhandled error in /api/chat: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"reply": f"AI服务异常，请稍后重试。", "tool_used": None}), 200


# ============ Voice Chat Endpoint ============
VOICE_TIMEOUT = 20  # seconds per the spec

@app.route('/api/voice_chat', methods=['POST'])
def voice_chat():
    """PTT / uploaded-audio voice input → ASR → AI chat → TTS → audio response.
    Accepts multipart/form-data with an 'audio' file field (WAV, 16kHz mono)
    plus an optional 'request_id' field used by 🛑 /api/cancel (week6)."""
    t_start = time.time()

    # week6: this request adopts a fresh id — every older id becomes invalid
    request_id = begin_request(request.form.get('request_id'))
    asr_elapsed = glm_elapsed = tts_elapsed = 0.0

    # ---- Check DashScope availability ----
    if not _dashscope_available:
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "DashScope 未配置")
        return jsonify({"error": "语音服务未配置，请设置DASHSCOPE_API_KEY环境变量"}), 503

    # ---- Get uploaded audio ----
    if 'audio' not in request.files:
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "缺少 audio 字段")
        return jsonify({"error": "未收到音频文件"}), 400

    audio_file = request.files['audio']
    if not audio_file.filename or audio_file.filename == '':
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "音频文件名为空")
        return jsonify({"error": "听不到声音，请重试"}), 400

    # Save uploaded audio to temp file
    tmp_suffix = os.path.splitext(audio_file.filename)[1] or '.wav'
    tmp_path = os.path.join(tempfile.gettempdir(), f'imu_voice_{uuid.uuid4().hex}{tmp_suffix}')
    audio_file.save(tmp_path)
    file_size = os.path.getsize(tmp_path)

    # Check for too-short / silent audio (less than ~0.5 seconds of 16kHz mono PCM = 16000 bytes)
    if file_size < 4000:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "音频过短(疑似静音)")
        return jsonify({"error": "听不到声音，请重试"}), 400

    print(f"【语音】收到音频: {file_size} bytes → {tmp_path}")

    try:
        # ---- Step 1: ASR (paraformer-realtime-v2) ----
        if time.time() - t_start > VOICE_TIMEOUT:
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "服务超时(ASR前)")
            return _voice_error(tmp_path, "服务超时，请重试")

        t0 = time.time()
        asr_text = _do_asr(tmp_path)
        asr_elapsed = time.time() - t0
        if asr_text is None:
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "ASR 失败")
            return _voice_error(tmp_path, "识别失败，请用键盘输入")

        print(f"【语音】ASR 结果: \"{asr_text}\"")

        if not asr_text.strip():
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "ASR 结果为空")
            return _voice_error(tmp_path, "听不到声音，请重试")

        # 🛑 may have been pressed while ASR was running → drop the stale result
        if not is_current_request(request_id):
            return _voice_cancelled(request_id, asr_elapsed, glm_elapsed, tts_elapsed, tmp_path)

        # ---- Step 2: AI Chat (reuse existing logic) ----
        if time.time() - t_start > VOICE_TIMEOUT:
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "服务超时(GLM前)")
            return _voice_error(tmp_path, "服务超时，请重试")

        stripped = asr_text.strip()
        if len(stripped) < 2:
            # too short to be a real command → low confidence → ask for clarification
            reply_text = CLARIFY_REPLY
            print(f"【语音】低置信度 ASR（{len(stripped)} 字）→ 引导用户澄清")
        else:
            t0 = time.time()
            chat_result = _do_ai_chat(stripped, request_id)
            glm_elapsed = time.time() - t0
            if chat_result.get("cancelled"):
                return _voice_cancelled(request_id, asr_elapsed, glm_elapsed, tts_elapsed, tmp_path)
            reply_text = chat_result.get("reply", "操作已完成。")

        print(f"【语音】AI 回复: \"{reply_text}\"")

        # ---- Step 3: TTS (cosyvoice-v2) ----
        if time.time() - t_start > VOICE_TIMEOUT:
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "服务超时(TTS前)")
            return _voice_error(tmp_path, "服务超时，请重试")

        audio_filename = f"voice_{uuid.uuid4().hex[:12]}.wav"
        audio_path = os.path.join(VOICE_DIR, audio_filename)

        t0 = time.time()
        tts_ok = _do_tts(reply_text, audio_path)
        tts_elapsed = time.time() - t0

        # last late-result gate: the user may have stopped while TTS was running
        if not is_current_request(request_id):
            return _voice_cancelled(request_id, asr_elapsed, glm_elapsed, tts_elapsed,
                                    tmp_path, audio_path)

        if not tts_ok:
            # TTS failed — still return text result without audio
            _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "TTS 失败，仅返回文字")
            return jsonify({
                "text": stripped,
                "reply": reply_text,
                "audio_url": None,
                "request_id": request_id,
                "_tts_error": True
            })

        audio_url = f"/static/voice/{audio_filename}"
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed,
                  f"text=\"{stripped[:30]}\" audio={audio_filename}")

        return jsonify({
            "text": stripped,
            "reply": reply_text,
            "audio_url": audio_url,
            "request_id": request_id
        })

    except Exception as e:
        print(f"【语音】未预期异常: {e}")
        import traceback
        traceback.print_exc()
        _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed, "未预期异常")
        return _voice_error(tmp_path, "服务超时，请重试")

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _voice_error(tmp_path, msg):
    """Clean up temp file and return error JSON."""
    try:
        os.unlink(tmp_path)
    except Exception:
        pass
    return jsonify({"error": msg}), 200


def _task_log(request_id, asr, glm, tts, note=""):
    """week6 日志与测速: [TASK] request_id=xxx, 耗时: ASR=1.2s, GLM=2.1s, TTS=1.5s"""
    line = (f"[TASK] request_id={request_id}, 耗时: "
            f"ASR={asr:.1f}s, GLM={glm:.1f}s, TTS={tts:.1f}s")
    if note:
        line += f" | {note}"
    print(line)


def _voice_cancelled(request_id, asr_elapsed, glm_elapsed, tts_elapsed,
                     tmp_path=None, audio_path=None):
    """Late-result isolation: 🛑 invalidated this request while it was running,
    so the result is discarded — it is never handed back to the frontend and no
    TTS audio is kept behind."""
    for p in (audio_path, tmp_path):
        if p:
            try:
                os.unlink(p)
            except Exception:
                pass
    _task_log(request_id, asr_elapsed, glm_elapsed, tts_elapsed,
              "结果已作废（用户已停止/发起新任务）")
    return jsonify({"cancelled": True, "request_id": request_id,
                    "text": "", "reply": "", "audio_url": None}), 200


def _do_asr(audio_path):
    """Run ASR on a WAV file, return recognized text or None on failure."""
    try:
        callback = RecognitionCallback()
        recognition = Recognition(
            model='paraformer-realtime-v2',
            format='wav',
            sample_rate=16000,
            callback=callback
        )
        result = recognition.call(audio_path)
        sentence = result.get_sentence()
        if sentence:
            if isinstance(sentence, list):
                text = "".join(s.get("text", "") if isinstance(s, dict) else str(s) for s in sentence)
            elif isinstance(sentence, dict):
                text = sentence.get("text", "")
            else:
                text = str(sentence)
            if text:
                return text
        return None
    except Exception as e:
        print(f"【语音】ASR 失败: {e}")
        return None


# ---- TTS (week6 fix) ----
# cosyvoice-v2 只支持其音色表中的 *_v2 音色，旧代码里的 'longxiaochun' 是 v1 时期的音色名，
# 不在 v2 音色表中（音色与模型不匹配），会直接导致 TTS 失败。
# 这里优先按“服务端默认音色”初始化（不传 voice）；若当前 SDK 要求 voice 必填
# （dashscope 1.27.7 中 voice 是无默认值的必填位置参数），则回退到
# cosyvoice-v2 自带的默认音色 longxiaochun_v2（龙小淳，知性积极女）。
COSYVOICE_V2_DEFAULT_VOICE = 'longxiaochun_v2'


def _do_tts(text, output_path):
    """Run TTS (cosyvoice-v2) and save audio to output_path. Returns True on success."""
    try:
        # 优先不传 voice，交由服务端使用默认最兼容音色
        try:
            synthesizer = SpeechSynthesizer(model='cosyvoice-v2')
        except TypeError:
            # dashscope 1.27.7: SpeechSynthesizer.__init__() missing 1
            # required positional argument: 'voice'
            # → 该版本无法省略 voice，退回 cosyvoice-v2 的默认音色
            synthesizer = SpeechSynthesizer(model='cosyvoice-v2',
                                            voice=COSYVOICE_V2_DEFAULT_VOICE)
        audio_data = synthesizer.call(text)
        if audio_data and isinstance(audio_data, bytes) and len(audio_data) > 0:
            with open(output_path, 'wb') as f:
                f.write(audio_data)
            print(f"【语音】TTS 保存: {output_path} ({len(audio_data)} bytes)")
            return True
        else:
            print(f"【语音】TTS 返回空数据")
            return False
    except Exception as e:
        print(f"【语音】TTS 失败: {e}")
        return False


def _do_ai_chat(user_msg: str, request_id=None):
    """Core chat logic: clarification → keyword matching → tool execution →
    LLM fallback → reply.
    Returns dict with keys 'reply', 'tool_used'; when a newer request has
    superseded this one it returns {'cancelled': True} instead of a late answer.
    `request_id` (week6) makes the whole flow abortable: a dropped request never
    fires a board action and never publishes its result to the frontend.
    Callable from both text chat and voice chat endpoints."""
    def _stale():
        """Late-result isolation: superseded or cancelled by the user."""
        return not is_current_request(request_id)

    def _stale_result():
        print(f"[TASK] request_id={request_id} 结果已作废（用户已停止或发起了新任务），丢弃不返回")
        return {"reply": "", "tool_used": None, "cancelled": True}

    msg_lower = user_msg.lower()
    for kw in UNSUPPORTED_KEYWORDS:
        if kw.lower() in msg_lower:
            # Meta‑questions about sensor support
            if kw in {"支持哪些传感器", "支持什么传感器", "有哪些传感器", "有什么传感器"}:
                return {"reply": "本系统仅支持以下功能：\n📷 摄像头拍照\n📡 WiFi状态查询\n🔄 IMU加速度计\n\n不支持温度/湿度/气压/PM2.5/声音/GPS/心率等传感器。",
                        "tool_used": None}
            # English keywords → Chinese display name
            display_map = {
                "temperature": "温度", "humidity": "湿度", "pressure": "气压",
                "light": "光照", "sound": "声音", "distance": "距离",
                "proximity": "接近", "gas": "气体", "co2": "CO₂", "voc": "VOC",
                "pm2.5": "PM2.5", "gps": "GPS",
            }
            display = display_map.get(kw.lower(), kw)
            return {"reply": f"本系统不支持{display}传感器。当前仅支持：摄像头拍照、IMU加速度计、WiFi状态查询。",
                    "tool_used": None}

    # ---- Step 1: Keyword pre‑matching → call tool directly (saves 1 LLM round‑trip) ----
    # ---- Step 0 (week6): unclear / garbled intent must be clarified, not guessed ----
    garbled = _match_clarify_keyword(user_msg, CLARIFY_GARBLED)
    if garbled:
        print(f"[TASK] request_id={request_id} 意图不明确（命中 '{garbled}'）→ 引导用户澄清")
        return {"reply": CLARIFY_REPLY, "tool_used": None}

    matched_tool = None
    # Check status keywords FIRST, then photo (status keywords like "最新照片" contain "照片")
    for kw in STATUS_KEYWORDS:
        if kw in user_msg:
            matched_tool = "get_device_status"
            break
    if not matched_tool:
        for kw in PHOTO_KEYWORDS:
            if kw in user_msg:
                matched_tool = "take_photo"
                break

    # ---- Step 1b (week6): nothing matched a clear tool → ask the user ----
    if not matched_tool:
        vague = _match_clarify_keyword(user_msg, CLARIFY_VAGUE)
        if vague:
            print(f"[TASK] request_id={request_id} 未匹配到明确工具（命中 '{vague}'）→ 引导用户澄清")
            return {"reply": CLARIFY_REPLY, "tool_used": None}

    if matched_tool:
        if _stale():
            return _stale_result()
        print(f"【AI】Keyword matched: '{matched_tool}'")
        tool_result_str = _execute_tool(matched_tool, request_id)
        try:
            tool_result = json.loads(tool_result_str)
        except json.JSONDecodeError:
            tool_result = {}
        if _stale():
            return _stale_result()
        final_reply = _format_tool_reply(matched_tool, tool_result)
        print(f"【AI】Keyword flow done (zero LLM)")
        return {"reply": final_reply, "tool_used": matched_tool}

    # ---- Step 2: Fallback — full Function Calling flow (LLM decides tool) ----
    system_prompt = (
        "你是ESP32-S3-EYE开发板的AI助手。"
        "\n\n【工具使用规则 - 必须遵守】"
        "\n1. 用户问\"状态/在线/设备情况\"等 → 必须调用 get_device_status 工具"
        "\n2. 用户问\"拍照/采集/拍一张\"等 → 必须调用 take_photo 工具"
        "\n3. 只能从工具返回的数据中提取信息，不允许编造"
        "\n4. 设备离线或任务超时，必须如实说\"设备未响应\"或\"任务超时\""
        "\n5. 不允许猜测设备状态，一切以工具返回为准"
        "\n6. 用中文简洁回复"
        "\n7. 如果用户的指令含糊不清、无法确定是要拍照还是查询状态，必须调用 ask_clarify 工具，绝对不要猜测意图"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    resp, elapsed1 = _call_llm_with_retry(messages, CHAT_TOOLS, retries=1)
    if "error" in resp:
        return {"reply": _friendly_error(resp["error"]), "tool_used": None}

    if _stale():
        return _stale_result()

    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    tool_calls = msg.get("tool_calls") or []

    if tool_calls:
        # week6: GLM reported a low-confidence intent → guide the user instead
        if any(tc.get("function", {}).get("name", "") == "ask_clarify" for tc in tool_calls):
            print(f"[TASK] request_id={request_id} GLM 置信度低 → 引导用户澄清")
            return {"reply": CLARIFY_REPLY, "tool_used": "ask_clarify"}
        if _stale():
            return _stale_result()
        tool_results = []
        tool_used = None
        for tc in tool_calls:
            func_name = tc.get("function", {}).get("name", "")
            if not func_name:
                continue
            if _stale():
                return _stale_result()
            print(f"【AI】Tool call: {func_name}")
            tool_used = func_name
            result_str = _execute_tool(func_name, request_id)
            tool_results.append({"tool_call_id": tc.get("id", "call_1"), "role": "tool", "content": result_str})

        messages.append(msg)
        messages.extend(tool_results)
        resp2, elapsed2 = _call_llm_with_retry(messages, retries=1)
        if _stale():
            return _stale_result()
        if "error" in resp2:
            final_reply = f"工具已执行，但AI回复生成失败：{_friendly_error(resp2['error'])}"
        else:
            choice2 = (resp2.get("choices") or [{}])[0]
            final_reply = choice2.get("message", {}).get("content", "") or "操作已完成。"
        print(f"【AI】FunctionCall flow done ({elapsed1:.1f}s + {elapsed2:.1f}s)")
        return {"reply": final_reply, "tool_used": tool_used}

    content = msg.get("content", "")
    if not content:
        # week6: no clear tool matched and no usable answer → guide the user
        content = CLARIFY_REPLY
    if _stale():
        return _stale_result()
    print(f"【AI】Direct reply ({elapsed1:.1f}s)")
    return {"reply": content, "tool_used": None}


def _format_tool_reply(tool_name, result):
    """Generate a canned Chinese reply from tool result — zero LLM cost."""
    if tool_name == "take_photo":
        if "取消" in str(result.get("reason", "")) or "CANCELLED" in str(result.get("reason", "")):
            return "🛑 任务已停止。"
        if result.get("success"):
            if result.get("blocked"):
                return ("已为您拍摄，但⚠️ 画面可能被遮挡或采集失败，请检查镜头"
                        "（平均灰度偏低）。")
            return "已为您拍摄，图片已上传。刷新页面即可查看。"
        reason = result.get("reason", "")
        if "离线" in reason or "未响应" in reason or "OFFLINE" in reason:
            return "设备未响应，请检查设备是否在线。"
        if "繁忙" in reason or "忙" in reason:
            return "设备正忙（上一个任务尚未完成），请稍后再试。"
        if "超时" in reason or "TIMEOUT" in reason:
            return (f"拍照指令已下发，但设备未在 {CAPTURE_TIMEOUT_SECONDS:.0f} 秒内完成采集"
                    f"（❌ 采集失败：设备超时/错误），请重试。")
        if "创建失败" in reason or "内部错误" in reason:
            return "任务创建失败，服务器内部错误，请重试。"
        if "错误" in reason or "FAIL" in reason:
            return f"❌ 采集失败：设备返回错误（{result.get('detail') or reason}）。"
        # fallback
        return f"拍照未成功（{reason}），请重试。"

    elif tool_name == "get_device_status":
        online = result.get("online", False)
        if not online:
            ago = result.get("last_seen_seconds_ago")
            if ago is not None:
                return f"设备离线，最后一次在线是 {ago} 秒前。请检查设备电源和网络。"
            return "设备离线，暂无在线记录。请检查设备电源和网络。"

        ip = result.get("device_ip", "未知")
        rssi = result.get("rssi")
        rssi_str = f"{rssi} dBm" if rssi is not None else "未知"
        task = result.get("task_status", "IDLE")
        task_map = {"IDLE": "空闲", "PENDING": "等待中", "ACCEPTED": "已接收",
                    "COLLECTING": "采集中", "COMPLETED": "已完成",
                    "TIMEOUT": "已超时", "FAIL": "采集失败"}
        task_display = task_map.get(task, task)
        latest = result.get("latest_photo")
        photo_str = latest if latest else "无"

        lines = [
            "📡 设备在线",
            f"IP：{ip}",
            f"信号：{rssi_str}",
            f"任务状态：{task_display}",
            f"最新照片：{photo_str}",
        ]
        return "\n".join(lines)

    return "操作已完成。"


def _friendly_error(err_str):
    """Map raw LLM errors to user-friendly messages."""
    if "429" in err_str:
        return "当前AI服务繁忙（访问量过大），请稍后再试。"
    if "1302" in err_str:
        return "当前AI服务繁忙（访问量过大），请稍后再试。"
    if err_str.startswith("RATE_LIMIT:"):
        wait = err_str.split(":")[1]
        return f"请求过于频繁，请 {wait} 秒后再试。"
    if "401" in err_str:
        return "AI服务认证失败，请检查API Key配置。"
    if "timeout" in err_str.lower() or "timed out" in err_str.lower():
        return "AI服务响应超时，请稍后再试。"
    return f"AI服务暂不可用：{err_str}"


@app.route('/api/chat/debug')
def chat_debug():
    """Return full request template that Flask would send to LLM (key masked)."""
    key_ok = bool(LLM_API_KEY)
    key_stripped = LLM_API_KEY.strip() if LLM_API_KEY else ""
    key_masked = key_stripped[:4] + "****" + key_stripped[-4:] if len(key_stripped) >= 8 else ("***" if key_stripped else None)
    model = LLM_MODEL.strip()
    url = f"{LLM_BASE_URL}/chat/completions"
    return jsonify({
        "key_priority": "MY_IMU_LLM_KEY > LLM_API_KEY > DEEPSEEK_API_KEY",
        "key_configured": key_ok,
        "key_masked": key_masked,
        "key_len": len(key_stripped) if key_stripped else 0,
        "base_url": LLM_BASE_URL,
        "model": model,
        "url": url,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key_masked or '***MISSING***'}"
        },
        "sample_body": {
            "model": model,
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 512,
            "temperature": 0.3
        }
    })

@app.route('/api/chat/health')
def chat_health():
    """Return current LLM configuration status."""
    key_ok = bool(LLM_API_KEY)
    key_masked = ""
    if LLM_API_KEY and len(LLM_API_KEY) >= 8:
        key_masked = LLM_API_KEY[:4] + "****" + LLM_API_KEY[-4:]
    elif LLM_API_KEY:
        key_masked = "***"  # key too short to mask safely
    return jsonify({
        "llm_configured": key_ok,
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "api_key_masked": key_masked if key_ok else None,
        "endpoint": f"{LLM_BASE_URL}/chat/completions",
    })

@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    # Startup diagnostic: print LLM configuration
    key_ok = bool(LLM_API_KEY)
    key_display = "未设置 ❌"
    if LLM_API_KEY and len(LLM_API_KEY) >= 8:
        key_display = f"{LLM_API_KEY[:4]}****{LLM_API_KEY[-4:]}"
    elif LLM_API_KEY:
        key_display = "已设置(长度过短)"
    print("=" * 55)
    print("   LLM 配置")
    print(f"   Model   : {LLM_MODEL}")
    print(f"   Base URL: {LLM_BASE_URL}")
    print(f"   API Key : {key_display}")
    print(f"   Ready   : {'✅ 可用' if key_ok else '❌ 缺少 API Key'}")
    print("=" * 55)
    net_startup_check()      # week14: 如实汇报本地缓存队列；上次断网遗留的待发送数据 → 启动即补传
    app.run(host='0.0.0.0', port=5000, threaded=True)
