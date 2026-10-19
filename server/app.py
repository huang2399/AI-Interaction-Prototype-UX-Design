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

task_state = {
    "task_id": None,
    "status": "IDLE",  # IDLE, PENDING, ACCEPTED, COMPLETED, TIMEOUT
    "last_update": time.time(),
    "latest_photo": None,
    "trigger_source": "web",
    "ack_needed": False
}
device_info = {
    "ip": None,        # learned from device poll/upload requests (remote_addr)
    "last_seen": None,
    "rssi": 32,        # board firmware does not report RSSI yet -> hard-coded per requirement
}
lock = threading.Lock()
TIMEOUT_SECONDS = 30

def check_timeout():
    with lock:
        if task_state["status"] in ["PENDING", "ACCEPTED"]:
            if time.time() - task_state["last_update"] > TIMEOUT_SECONDS:
                task_state["status"] = "TIMEOUT"
                print("【状态变更】任务超时！")

def touch_device():
    with lock:
        device_info["ip"] = request.remote_addr
        device_info["last_seen"] = time.time()

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
        if task_state["status"] in ("PENDING", "ACCEPTED"):
            # PENDING = the board has not polled it yet -> recall it before it fires
            task_state["status"] = "IDLE"
            task_state["task_id"] = None
            task_state["ack_needed"] = False
            task_state["last_update"] = time.time()
            board_cancelled = True

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
    to /api/device/upload_imu (then real data wins for 5 s windows)."""
    while True:
        now = time.time()
        with imu_lock:
            real_recent = (now - imu_last_real) < 5.0
        if not real_recent:
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
                           "size": os.path.getsize(p), "status": "stored"})
    for e in ledger:
        if e.get("status") == "stored" and not os.path.exists(os.path.join(UPLOAD_FOLDER, e.get("name", ""))):
            e["status"] = "cleaned"
    ledger_save()

ledger_load()

def ledger_add(name, size):
    with ledger_lock:
        ledger.append({"name": name, "ts": time.time(), "size": size, "status": "stored"})
        if len(ledger) > 200:
            ledger[:] = ledger[-200:]
        ledger_save()

def ledger_mark_cleaned(names):
    with ledger_lock:
        for e in ledger:
            if e.get("name") in names and e.get("status") == "stored":
                e["status"] = "cleaned"
        ledger_save()

# ---------------- API: control plane ----------------
@app.route('/api/trigger_capture', methods=['POST'])
def trigger_capture():
    source = request.args.get('source', 'web')
    with lock:
        task_state["task_id"] = str(int(time.time()))
        task_state["status"] = "PENDING"
        task_state["trigger_source"] = source
        task_state["ack_needed"] = False
        task_state["last_update"] = time.time()
        print(f"【服务器受理】下发拍照指令，任务ID: {task_state['task_id']}，触发源: {source}")
    return jsonify({"status": "ok", "task_id": task_state["task_id"]})

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
            task_state["last_update"] = time.time()
            print("【设备接收】ESP32 拉取到拍照指令")
            return jsonify({"has_task": True, "task_id": task_state["task_id"]}), 200
        return jsonify({"has_task": False}), 200

@app.route('/api/device/upload_photo', methods=['POST'])
def upload_photo():
    if 'image' not in request.files:
        return jsonify({"error": "No image part"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    # Read and validate file size BEFORE saving
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size < MIN_PHOTO_SIZE:
        print(f"【告警】拒绝保存无效文件: {file.filename}, 大小仅 {file_size} 字节 (阈值 {MIN_PHOTO_SIZE})")
        return jsonify({"error": f"File too small ({file_size} bytes), likely corrupt"}), 400

    # Validate JPEG magic bytes (FF D8 FF)
    header = file.read(3)
    file.seek(0)
    if header[:2] != b'\xff\xd8':
        print(f"【告警】非JPEG文件，拒绝保存: {file.filename}, 头部: {header.hex()}")
        return jsonify({"error": "Not a valid JPEG image"}), 400

    trigger = request.form.get('trigger', 'web')
    # keep scheduler attribution: board echoes default 'web' for polled tasks
    with lock:
        if trigger == 'web' and task_state.get('trigger_source') == 'timer':
            trigger = 'timer'

    filename = f"photo_{int(time.time())}.jpg"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    actual_size = os.path.getsize(filepath)

    with lock:
        task_state["latest_photo"] = filename
        task_state["status"] = "COMPLETED"
        task_state["trigger_source"] = trigger
        task_state["ack_needed"] = (trigger == "physical_button")
        task_state["last_update"] = time.time()
        print(f"【任务完成】收到设备照片: {filename} ({actual_size} bytes, 触发源: {trigger})")
    touch_device()
    ledger_add(filename, actual_size)

    # Clean up old files: keep only last 50 photos
    cleanup_old_photos(50)

    return jsonify({"status": "ok", "filename": filename}), 200

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
    """Rich photo list (stored + cleaned) for gallery cards."""
    out = []
    with ledger_lock:
        entries = [e for e in ledger if VALID_PHOTO_RE.match(e.get("name", ""))]
    for e in reversed(entries):
        out.append({
            "name": e["name"],
            "ts": e["ts"],
            "time_str": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e["ts"])),
            "size": e["size"],
            "status": e["status"],
            "url": f"/photos/{e['name']}" if e["status"] == "stored" else None,
        })
    return jsonify({"photos": out[:60]})

@app.route('/api/status', methods=['GET'])
def get_status():
    check_timeout()
    with lock:
        data = dict(task_state)
        data["device_ip"] = device_info["ip"]
        data["device_last_seen"] = device_info["last_seen"]
        data["rssi"] = device_info["rssi"]
    with timer_lock:
        data["timer_running"] = timer_state["running"]
        data["timer_current_batch"] = timer_state["current_batch"]
        data["timer_max_batches"] = timer_state["max_batches"]
        data["timer_interval"] = timer_state["interval"]
        data["timer_last_result"] = timer_state["last_result"]
    return jsonify(data)

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

def create_capture_task(source):
    """Shared task creation (web button / timer scheduler).
    Refuses when board is still busy so a task id is never overwritten."""
    with lock:
        if task_state["status"] in ("PENDING", "ACCEPTED"):
            return False, "busy"
        tid = str(int(time.time()))
        task_state["task_id"] = tid
        task_state["status"] = "PENDING"
        task_state["trigger_source"] = source
        task_state["ack_needed"] = False
        task_state["last_update"] = time.time()
        print(f"【任务创建】下发拍照指令，任务ID: {tid}，触发源: {source}")
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
            if st in ("PENDING", "ACCEPTED"):
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
                deadline = time.time() + TIMEOUT_SECONDS + 10
                while not stop_evt.is_set() and time.time() < deadline:
                    with lock:
                        st = task_state["status"]
                    if st in ("COMPLETED", "TIMEOUT"):
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
    with imu_lock:
        hist = list(imu_history)[-60:]
    latest = hist[-1] if hist else None
    return jsonify({"latest": latest, "history": hist,
                    "source": (latest or {}).get("source", "simulated")})

@app.route('/api/device/upload_imu', methods=['POST'])
def upload_imu():
    """Real IMU samples from board firmware (QMA6100P, m/s^2).
    Accepts {"x","y","z"} (firmware v3 payload) or {"ax","ay","az"} (legacy).
    Real data wins for 5 s windows: /api/imu_data reports source='device'."""
    global imu_last_real
    d = request.get_json(silent=True) or {}
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
    sample = {"t": round(time.time(), 3),
              "ax": round(ax, 3), "ay": round(ay, 3), "az": round(az, 3),
              "source": "device"}
    with imu_lock:
        imu_history.append(sample)
        imu_last_real = time.time()
    touch_device()
    return jsonify({"status": "ok"}), 200

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
            busy = task_state["status"] in ("PENDING", "ACCEPTED")
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

        # Poll until COMPLETED/TIMEOUT/OFFLINE/CANCELLED (max 35s)
        deadline = time.time() + 35
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
            if st in ("COMPLETED", "TIMEOUT"):
                result_status = st; result_photo = photo; break
            with lock:
                ip_now = device_info["ip"]
                ls_now = device_info["last_seen"]
            if not (ip_now and ls_now and (time.time() - ls_now < 60)):
                result_status = "OFFLINE"; break

        if result_status == "COMPLETED":
            return json.dumps({"success": True, "photo": result_photo, "task_id": tid}, ensure_ascii=False)
        elif result_status == "TIMEOUT":
            return json.dumps({"success": False, "reason": "任务超时",
                "detail": "拍照指令已下发，但设备在30秒内未完成采集。"}, ensure_ascii=False)
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
            return "已为您拍摄，图片已上传。刷新页面即可查看。"
        reason = result.get("reason", "")
        if "离线" in reason or "未响应" in reason or "OFFLINE" in reason:
            return "设备未响应，请检查设备是否在线。"
        if "繁忙" in reason or "忙" in reason:
            return "设备正忙（上一个任务尚未完成），请稍后再试。"
        if "超时" in reason or "TIMEOUT" in reason:
            return "拍照指令已下发，但设备未在超时前完成采集，请重试。"
        if "创建失败" in reason or "内部错误" in reason:
            return "任务创建失败，服务器内部错误，请重试。"
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
                    "COMPLETED": "已完成", "TIMEOUT": "已超时"}
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
    app.run(host='0.0.0.0', port=5000, threaded=True)
