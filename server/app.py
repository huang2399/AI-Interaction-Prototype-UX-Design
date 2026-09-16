from flask import Flask, request, jsonify
import time
import threading

app = Flask(__name__)

# 全局状态管理
task_state = {
    "task_id": None,
    "status": "IDLE",  # IDLE, PENDING(等待设备接收), ACCEPTED(设备已接收), COMPLETED(完成), TIMEOUT(超时)
    "last_update": time.time(),
    "latest_data": {"x": 0, "y": 0, "z": 0, "time": "无数据"}
}
lock = threading.Lock()

# 超时时间（秒）
TIMEOUT_SECONDS = 15

def check_timeout():
    """检查任务是否超时"""
    with lock:
        if task_state["status"] in ["PENDING", "ACCEPTED"]:
            if time.time() - task_state["last_update"] > TIMEOUT_SECONDS:
                task_state["status"] = "TIMEOUT"
                print("【状态变更】任务超时！")

# 1. Web端触发采集
@app.route('/api/trigger_collect', methods=['POST'])
def trigger_collect():
    with lock:
        task_id = str(int(time.time()))
        task_state["task_id"] = task_id
        task_state["status"] = "PENDING"
        task_state["last_update"] = time.time()
        print(f"【服务器受理】收到Web端采集请求，任务ID: {task_id}，状态: PENDING")
    return jsonify({"status": "ok", "task_id": task_id}), 200

# 2. ESP32轮询是否有新任务
@app.route('/api/device/poll', methods=['GET'])
def device_poll():
    with lock:
        # 如果是PENDING状态，说明有任务，下发并把状态改为ACCEPTED
        if task_state["status"] == "PENDING":
            task_state["status"] = "ACCEPTED"
            task_state["last_update"] = time.time()
            print("【设备接收】ESP32已拉取到指令，状态变更为 ACCEPTED")
            return jsonify({"has_task": True, "task_id": task_state["task_id"]}), 200
        # 如果没有任务，让设备继续等待
        return jsonify({"has_task": False}), 200

# 3. ESP32上报执行结果和数据
@app.route('/api/device/report', methods=['POST'])
def device_report():
    data = request.json
    with lock:
        task_state["latest_data"] = {
            "x": data.get("x", 0),
            "y": data.get("y", 0),
            "z": data.get("z", 0),
            "time": time.strftime("%H:%M:%S")
        }
        task_state["status"] = "COMPLETED"
        task_state["last_update"] = time.time()
        print(f"【任务完成】收到设备上报数据: {task_state['latest_data']}，状态变更为 COMPLETED")
    return jsonify({"status": "ok"}), 200

# 4. Web端查询当前状态和数据
@app.route('/api/status', methods=['GET'])
def get_status():
    check_timeout()
    with lock:
        return jsonify({
            "status": task_state["status"],
            "data": task_state["latest_data"],
            "task_id": task_state["task_id"]
        })

# 5. Web展示页面
@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>ESP32-S3-EYE 远程采集控制</title>
        <style>
            body { font-family: Arial; text-align: center; margin-top: 50px; }
            .box { border: 1px solid #ccc; padding: 20px; display: inline-block; border-radius: 10px; min-width: 300px; }
            .status { font-weight: bold; padding: 5px; border-radius: 5px; margin: 10px 0; }
            #status-IDLE { color: gray; }
            #status-PENDING { color: orange; }
            #status-ACCEPTED { color: blue; }
            #status-COMPLETED { color: green; }
            #status-TIMEOUT { color: red; }
            button { padding: 10px 20px; font-size: 16px; cursor: pointer; margin-top: 15px; }
        </style>
    </head>
    <body>
        <div class="box">
            <h2>远程采集控制面板</h2>
            <div>当前状态: <span id="status" class="status">IDLE</span></div>
            <div style="font-size: 14px; color: #555;">任务ID: <span id="task_id">--</span></div>
            <hr>
            <p>X轴: <span id="x" style="color:red; font-size:20px;">--</span> m/s²</p>
            <p>Y轴: <span id="y" style="color:green; font-size:20px;">--</span> m/s²</p>
            <p>Z轴: <span id="z" style="color:blue; font-size:20px;">--</span> m/s²</p>
            <p>更新时间: <span id="time">--</span></p>
            <button onclick="trigger()">🔄 发起重新采集</button>
        </div>
        <script>
            function trigger() {
                fetch('/api/trigger_collect', { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    console.log("触发成功:", data);
                });
            }

            // 每秒轮询状态
            setInterval(() => {
                fetch('/api/status').then(res => res.json()).then(res => {
                    document.getElementById('status').innerText = res.status;
                    document.getElementById('status').className = 'status status-' + res.status;
                    document.getElementById('task_id').innerText = res.task_id || '--';
                    
                    if (res.data) {
                        document.getElementById('x').innerText = res.data.x;
                        document.getElementById('y').innerText = res.data.y;
                        document.getElementById('z').innerText = res.data.z;
                        document.getElementById('time').innerText = res.data.time;
                    }
                });
            }, 1000);
        </script>
    </body>
    </html>
    '''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)