from flask import Flask, request, jsonify, send_from_directory, render_template
import time, threading, os, glob, json, math, random, re
from collections import deque

app = Flask(__name__)
UPLOAD_FOLDER = r'D:\esp_project\imu_web_demo\photos'
LEDGER_FILE = r'D:\esp_project\imu_web_demo\server\photos_ledger.json'
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

LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
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
]

UNSUPPORTED_KEYWORDS = {"temperature", "humidity", "pressure", "light", "sound", "distance",
                        "proximity", "gas", "co2", "voc"}


def _call_llm(messages, tools=None):
    if not LLM_API_KEY:
        return {"error": "LLM_API_KEY not configured. Set LLM_API_KEY environment variable (or DEEPSEEK_API_KEY as fallback)."}
    body = {"model": LLM_MODEL, "messages": messages, "max_tokens": 512, "temperature": 0.3}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"【AI】LLM HTTP {e.code}: {body[:300]}")
        return {"error": f"LLM API error {e.code}: {body[:200]}"}
    except Exception as e:
        print(f"【AI】LLM request failed: {e}")
        return {"error": str(e)}

def _execute_tool(func_name):
    """Execute a function call locally and return JSON result string."""
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

        # Poll until COMPLETED/TIMEOUT/OFFLINE (max 35s)
        deadline = time.time() + 35
        result_status = None
        result_photo = None
        while time.time() < deadline:
            time.sleep(1.0)
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
        else:
            return json.dumps({"success": False, "reason": "设备未响应",
                "detail": "设备在接受任务后失去响应。"}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {func_name}"})


@app.route('/api/chat', methods=['POST'])
def ai_chat():
    """Natural-language AI assistant: /api/chat {message: ...} -> {reply: ..., tool_used: ...}"""
    d = request.get_json(silent=True) or {}
    user_msg = (d.get("message", "") or "").strip()
    if not user_msg:
        return jsonify({"reply": "请输入您的问题。", "tool_used": None}), 200
    if len(user_msg) > 500:
        return jsonify({"reply": "输入内容过长，请控制在500字以内。", "tool_used": None}), 200

    msg_lower = user_msg.lower()
    for kw in UNSUPPORTED_KEYWORDS:
        if kw in msg_lower:
            return jsonify({"reply": f"本系统不支持{kw}传感器。当前仅支持：摄像头拍照、IMU加速度计、WiFi状态查询。",
                            "tool_used": None}), 200

    system_prompt = (
        "你是ESP32-S3-EYE开发板的AI助手。"
        "\n\n【规则】1. 可用get_device_status查状态、take_photo拍照。"
        "\n2. 只有take_photo返回success=true才说'已采集成功'。"
        "\n3. 设备离线/超时必须如实说'设备未响应'或'任务超时'。"
        "\n4. 不支持温度/湿度等传感器，问这些直接回复'本系统不支持该功能'。"
        "\n5. 不编造数据，使用中文简洁回复。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    resp = _call_llm(messages, CHAT_TOOLS)
    if "error" in resp:
        return jsonify({"reply": f"AI服务暂不可用：{resp['error']}", "tool_used": None}), 200

    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    tool_calls = msg.get("tool_calls") or []

    if tool_calls:
        tool_results = []
        tool_used = None
        for tc in tool_calls:
            func_name = tc.get("function", {}).get("name", "")
            if not func_name:
                continue
            print(f"【AI】Tool call: {func_name}")
            tool_used = func_name
            result_str = _execute_tool(func_name)
            tool_results.append({"tool_call_id": tc.get("id", "call_1"), "role": "tool", "content": result_str})

        messages.append(msg)
        messages.extend(tool_results)
        resp2 = _call_llm(messages)
        if "error" in resp2:
            final_reply = f"工具已执行，但AI回复生成失败：{resp2['error']}"
        else:
            choice2 = (resp2.get("choices") or [{}])[0]
            final_reply = choice2.get("message", {}).get("content", "") or "操作已完成。"
        return jsonify({"reply": final_reply, "tool_used": tool_used}), 200

    content = msg.get("content", "")
    if not content:
        content = "抱歉，我没有理解您的问题。试试：'现在状态怎么样' 或 '帮我拍张照'。"
    return jsonify({"reply": content, "tool_used": None}), 200


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
