from flask import Flask, request, jsonify, send_from_directory
import time, threading, os

app = Flask(__name__)
UPLOAD_FOLDER = 'photos'
os.makedirs(UPLOAD_FOLDER, exist_ok=True) # 自动创建照片保存文件夹

task_state = {
    "task_id": None,
    "status": "IDLE",  # IDLE, PENDING, ACCEPTED, COMPLETED, TIMEOUT
    "last_update": time.time(),
    "latest_photo": None
}
lock = threading.Lock()
TIMEOUT_SECONDS = 30 # 拍照需要时间，超时设置为30秒

def check_timeout():
    with lock:
        if task_state["status"] in ["PENDING", "ACCEPTED"]:
            if time.time() - task_state["last_update"] > TIMEOUT_SECONDS:
                task_state["status"] = "TIMEOUT"
                print("【状态变更】任务超时！")

@app.route('/api/trigger_capture', methods=['POST'])
def trigger_capture():
    with lock:
        task_state["task_id"] = str(int(time.time()))
        task_state["status"] = "PENDING"
        task_state["last_update"] = time.time()
        print(f"【服务器受理】下发拍照指令，任务ID: {task_state['task_id']}")
    return jsonify({"status": "ok", "task_id": task_state["task_id"]})

@app.route('/api/device/poll', methods=['GET'])
def device_poll():
    with lock:
        if task_state["status"] == "PENDING":
            task_state["status"] = "ACCEPTED"
            task_state["last_update"] = time.time()
            print("【设备接收】ESP32 拉取到拍照指令")
            return jsonify({"has_task": True, "task_id": task_state["task_id"]}), 200
        return jsonify({"has_task": False}), 200

@app.route('/api/device/upload_photo', methods=['POST'])
def upload_photo():
    # 接收开发板发来的图片文件
    if 'image' not in request.files:
        return jsonify({"error": "No image part"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    filename = f"photo_{int(time.time())}.jpg"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    
    with lock:
        task_state["latest_photo"] = filename
        task_state["status"] = "COMPLETED"
        task_state["last_update"] = time.time()
        print(f"【任务完成】收到设备照片: {filename}")
        
    return jsonify({"status": "ok"}), 200

@app.route('/api/status', methods=['GET'])
def get_status():
    check_timeout()
    with lock:
        return jsonify({
            "status": task_state["status"],
            "task_id": task_state["task_id"],
            "latest_photo": task_state["latest_photo"]
        })

@app.route('/photos/<filename>')
def get_photo(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>远程拍照控制台</title></head>
    <body style="font-family: Arial; text-align: center; margin-top: 50px;">
        <div style="border: 1px solid #ccc; padding: 20px; display: inline-block; border-radius: 10px;">
            <h2>远程抓拍控制台</h2>
            <div>当前状态: <span id="status" style="font-weight:bold;">IDLE</span></div>
            <div>任务ID: <span id="task_id">--</span></div>
            <hr>
            <button onclick="trigger()" style="padding: 10px 20px; font-size: 16px;">📸 立即下发抓拍指令</button>
            <div id="photo_box" style="margin-top: 20px;">
                <p>暂无照片</p>
            </div>
        </div>
        <script>
            function trigger() {
                fetch('/api/trigger_capture', { method: 'POST' });
            }
            setInterval(() => {
                fetch('/api/status').then(res => res.json()).then(res => {
                    document.getElementById('status').innerText = res.status;
                    document.getElementById('task_id').innerText = res.task_id || '--';
                    if (res.latest_photo) {
                        document.getElementById('photo_box').innerHTML = 
                            `<img src="/photos/${res.latest_photo}" style="max-width: 320px; border-radius: 8px; margin-top: 10px;">`;
                    }
                });
            }, 1000);
        </script>
    </body>
    </html>
    '''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)