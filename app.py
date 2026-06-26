import os
import time
import threading
import json
import subprocess
import urllib.request
import wave
import io
import cv2
import torch
import numpy as np
from flask import Flask, request, jsonify, Response, send_from_directory, render_template_string

app = Flask(__name__)

# Paths and Configs
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_DIR = os.path.join(BASE_DIR, "weights")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
BRAIN_DIR = os.path.join(os.environ.get("USERPROFILE", "C:\\Users\\user"), ".gemini", "antigravity-cli", "brain")
CONFIG_PATH = os.path.join(BASE_DIR, "api_config.json")

os.makedirs(WEIGHTS_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Lock for model loads
model_lock = threading.Lock()

# Global App States
app_state = {
    "yolo_model": None,
    "yolo_name": "",
    "yolo_type": "standard", # standard, world, openvino
    "tts_model": None,
    "tts_name": "",
    "status": "IDLE", # IDLE, LOADING, RUNNING, ERROR
    "error_message": "",
    "yolo_imgsz": 320,
    "yolo_conf": 0.25,
    "yolo_classes": ["person", "car", "bicycle", "dog", "laptop"]
}

# Video Detections Cache
# { video_filename: { "fps": float, "width": int, "height": int, "frames": { t_ms: [boxes] } } }
detections_cache = {}
video_threads = {}

# API Configurations & Metrics
api_config = {
    "server_enabled": True,
    "allow_external": False,
    "cors_enabled": True,
    "cors_origin": "*",
    "keys": {}
}

metrics = {
    "total_requests": 0,
    "active_requests": 0,
    "total_duration": 0.0,
    "avg_duration": 0.0
}
api_logs = []
notifications_log = []

# Persistent Agent Connection States (for Connect UI)
monitored_agents = {} # conv_id -> { "last_state": str, "notified": bool }

# Load API Config
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            api_config.update(json.load(f))
    except Exception:
        pass

def save_api_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(api_config, f, indent=2)
    except Exception:
        pass

# ----------------- Helper Functions -----------------

def get_git_diff():
    try:
        result = subprocess.run(["git", "diff"], cwd=BASE_DIR, capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as e:
        return f"Git diff error: {str(e)}"

def send_windows_notification(title, message):
    ps_toast = f"""
    [void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms")
    $objNotifyIcon = New-Object System.Windows.Forms.NotifyIcon
    $objNotifyIcon.Icon = [System.Drawing.SystemIcons]::Information
    $objNotifyIcon.BalloonTipIcon = "Info"
    $objNotifyIcon.BalloonTipTitle = "{title}"
    $objNotifyIcon.BalloonTipText = "{message}"
    $objNotifyIcon.Visible = $True
    $objNotifyIcon.ShowBalloonTip(10000)
    """
    try:
        subprocess.Popen(["powershell", "-Command", ps_toast], shell=True)
    except Exception:
        pass
    
    # Log in app notifications list
    notifications_log.append({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "message": f"[{title}] {message}",
        "read": False
    })
    if len(notifications_log) > 50:
        notifications_log.pop(0)

# Check active agent processes via powershell
def is_agent_process_running(conv_id):
    ps_cmd = f"Get-CimInstance Win32_Process | Where-Object {{$_.CommandLine -like '*{conv_id}*'}} | Select-Object -Property ProcessId"
    try:
        result = subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            return True
    except Exception:
        pass
    return False

# Parse conversation logs
def get_agent_detailed_status(conv_id):
    log_path = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(log_path):
        return "UNKNOWN", "Session files not found"
    
    # Check if process is currently running
    running = is_agent_process_running(conv_id)
    
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return "RUNNING" if running else "IDLE", "Empty log"
        
        last_line = lines[-1]
        step = json.loads(last_line)
        step_type = step.get("type", "")
        status = step.get("status", "")
        
        # Check if waiting for questions/permissions
        tool_calls = step.get("tool_calls", [])
        is_waiting = False
        for tc in tool_calls:
            if tc.get("name", "") in ("ask_question", "ask_permission"):
                is_waiting = True
                break
        
        if is_waiting and running:
            return "WAITING", "Waiting for user action"
        
        if running:
            return "RUNNING", "Agent is actively working"
        
        # If not running and finished
        return "FINISHED", "Agent run completed"
    except Exception as e:
        return "ERROR", str(e)

# Agent Monitoring Thread (runs every 3 seconds to trigger toast notifications)
def agent_monitor_loop():
    while True:
        try:
            if os.path.exists(BRAIN_DIR):
                for name in os.listdir(BRAIN_DIR):
                    path = os.path.join(BRAIN_DIR, name)
                    if os.path.isdir(path):
                        conv_id = name
                        state, desc = get_agent_detailed_status(conv_id)
                        
                        if conv_id not in monitored_agents:
                            monitored_agents[conv_id] = { "last_state": state, "notified": False }
                        
                        last_state = monitored_agents[conv_id]["last_state"]
                        
                        if state != last_state:
                            # State changed
                            monitored_agents[conv_id]["last_state"] = state
                            if state == "FINISHED":
                                send_windows_notification("Агент Antigravity", f"Задание завершено в сессии {conv_id[:8]}...")
                            elif state == "WAITING":
                                send_windows_notification("Агент ждет ввода", f"Сессия {conv_id[:8]}... заблокирована")
        except Exception:
            pass
        time.sleep(3)

threading.Thread(target=agent_monitor_loop, daemon=True).start()

# Load YOLO model
def load_yolo_model_fn(model_name):
    global app_state
    with model_lock:
        app_state["status"] = "LOADING"
        try:
            from ultralytics import YOLO, YOLOWorld
            model_path = os.path.join(WEIGHTS_DIR, model_name)
            
            # Check if it's an OpenVINO directory
            openvino_dir = model_path.replace(".pt", "_openvino_model")
            
            if os.path.exists(openvino_dir) and os.path.isdir(openvino_dir):
                # Load compiled OpenVINO xml
                xml_path = None
                for f in os.listdir(openvino_dir):
                    if f.endswith(".xml"):
                        xml_path = os.path.join(openvino_dir, f)
                        break
                if xml_path:
                    app_state["yolo_model"] = YOLO(xml_path, task="detect")
                    app_state["yolo_name"] = model_name
                    app_state["yolo_type"] = "openvino"
                    app_state["status"] = "IDLE"
                    return
            
            # Fallback to standard JIT/PyTorch
            if "world" in model_name:
                # If YOLO-World
                if not os.path.exists(model_path):
                    # Trigger download
                    app_state["yolo_model"] = YOLOWorld(model_name)
                else:
                    app_state["yolo_model"] = YOLOWorld(model_path)
                
                # Apply current class list
                app_state["yolo_model"].set_classes(app_state["yolo_classes"])
                app_state["yolo_type"] = "world"
            else:
                # Standard YOLO
                if not os.path.exists(model_path):
                    app_state["yolo_model"] = YOLO(model_name)
                else:
                    app_state["yolo_model"] = YOLO(model_path)
                app_state["yolo_type"] = "standard"
            
            app_state["yolo_name"] = model_name
            app_state["status"] = "IDLE"
        except Exception as e:
            app_state["status"] = "ERROR"
            app_state["error_message"] = str(e)

# Export model to OpenVINO
def compile_openvino_fn(model_name, format_type):
    global app_state
    with model_lock:
        app_state["status"] = "LOADING"
        try:
            from ultralytics import YOLO, YOLOWorld
            model_path = os.path.join(WEIGHTS_DIR, model_name)
            
            if "world" in model_name:
                pt_model = YOLOWorld(model_path if os.path.exists(model_path) else model_name)
                pt_model.set_classes(app_state["yolo_classes"])
            else:
                pt_model = YOLO(model_path if os.path.exists(model_path) else model_name)
            
            # Export
            int8_flag = (format_type == "INT8")
            export_path = pt_model.export(format="openvino", int8=int8_flag, imgsz=app_state["yolo_imgsz"])
            
            # Reload
            load_yolo_model_fn(model_name)
        except Exception as e:
            app_state["status"] = "ERROR"
            app_state["error_message"] = str(e)

# Load Silero TTS v5.5 stand-alone
def load_tts_model_fn():
    global app_state
    if app_state["tts_model"] is not None:
        return
    
    model_path = os.path.join(WEIGHTS_DIR, "v5_5_ru.pt")
    
    if not os.path.exists(model_path):
        # Auto-download Russian Silero v5.5 JIT package
        send_windows_notification("Silero TTS", "Скачивание модели TTS v5.5 (145МБ)...")
        try:
            url = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(model_path, "wb") as out_file:
                out_file.write(response.read())
            send_windows_notification("Silero TTS", "Модель загружена успешно!")
        except Exception as e:
            app_state["error_message"] = f"Failed to download Silero model: {str(e)}"
            return
            
    # Load using torch.package PackageImporter
    try:
        importer = torch.package.PackageImporter(model_path)
        app_state["tts_model"] = importer.load_pickle("tts_models", "model")
        app_state["tts_name"] = "v5_5_ru"
    except Exception as e:
        app_state["error_message"] = f"Failed to load Silero package: {str(e)}"

# Asynchronous Video Pre-processor Thread
def analyze_video_thread(video_filename, frame_skip):
    video_path = os.path.join(UPLOAD_FOLDER, video_filename)
    if not os.path.exists(video_path):
        return
        
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return
        
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    detections_cache[video_filename] = {
        "fps": fps,
        "width": width,
        "height": height,
        "progress": 0,
        "frames": {}
    }
    
    frame_idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
            
        t_ms = int((frame_idx / fps) * 1000)
        
        # Standard sub-sampling frame check
        if frame_idx % frame_skip == 0:
            boxes_data = []
            if app_state["yolo_model"] is not None:
                # CPU Inference at lower imgsz
                with model_lock:
                    results = app_state["yolo_model"](frame, imgsz=app_state["yolo_imgsz"], verbose=False)
                
                if len(results) > 0:
                    boxes = results[0].boxes
                    for box in boxes:
                        xyxy = box.xyxy[0].cpu().numpy()
                        x1, y1, x2, y2 = map(int, xyxy)
                        conf = float(box.conf[0].cpu().numpy())
                        cls_id = int(box.cls[0].cpu().numpy())
                        label = results[0].names[cls_id]
                        
                        # Filter by classes if specified
                        if label in app_state["yolo_classes"]:
                            # Percentage coords (0.0 to 1.0)
                            boxes_data.append({
                                "x1": x1 / width,
                                "y1": y1 / height,
                                "x2": x2 / width,
                                "y2": y2 / height,
                                "label": label,
                                "conf": conf,
                                "color_idx": cls_id
                            })
            detections_cache[video_filename]["frames"][t_ms] = boxes_data
        else:
            # Interpolate / copy from previous processed frame
            prev_t_ms = int(((frame_idx - (frame_idx % frame_skip)) / fps) * 1000)
            detections_cache[video_filename]["frames"][t_ms] = detections_cache[video_filename]["frames"].get(prev_t_ms, [])
            
        frame_idx += 1
        progress = int((frame_idx / total_frames) * 100)
        detections_cache[video_filename]["progress"] = progress
        
    cap.release()

# ----------------- Middlewares -----------------

@app.before_request
def check_api_access():
    if request.path.startswith('/api/'):
        if not api_config.get("server_enabled", True):
            return jsonify({"error": "API server is disabled"}), 403
        
        # Skip local UI calls or preflights
        if request.method == 'OPTIONS':
            return
            
        # Bind access control list for local only
        if not api_config.get("allow_external", False):
            if request.remote_addr not in ('127.0.0.1', 'localhost', '::1'):
                return jsonify({"error": "External API access is restricted"}), 403
                
        # API verification bypass for simple status/fs calls from same origin Tauri WebView
        # but enforce validation for specific external /api/ keys
        if request.remote_addr not in ('127.0.0.1', 'localhost', '::1'):
            api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
            if not api_key or api_key not in api_config.get("keys", {}):
                return jsonify({"error": "Invalid or missing API key"}), 401
        
        # Trace metrics
        metrics["active_requests"] += 1
        metrics["total_requests"] += 1
        request.start_time = time.time()

@app.after_request
def log_api_request(response):
    if request.path.startswith('/api/'):
        if hasattr(request, 'start_time'):
            duration = (time.time() - request.start_time) * 1000
            metrics["active_requests"] = max(0, metrics["active_requests"] - 1)
            metrics["total_duration"] += duration
            metrics["avg_duration"] = metrics["total_duration"] / max(1, metrics["total_requests"])
            
            # Logger
            log_entry = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": request.method,
                "endpoint": request.path,
                "status": response.status_code,
                "ip": request.remote_addr,
                "duration": round(duration, 2)
            }
            api_logs.append(log_entry)
            if len(api_logs) > 50:
                api_logs.pop(0)
                
        # Inject CORS Headers
        if api_config.get("cors_enabled", True):
            origin = api_config.get("cors_origin", "*")
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    return response

# ----------------- UI / Static Routes -----------------

@app.route("/")
def index():
    return render_template_string(HTML_UI)

@app.route("/uploads/<path:filename>")
def serve_uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ----------------- API Endpoints -----------------

# API Metrics and Logs
@app.route("/api/metrics")
def get_metrics_api():
    return jsonify({
        "metrics": metrics,
        "logs": api_logs,
        "config": api_config
    })

@app.route("/api/config/update", methods=["POST"])
def update_config_api():
    global api_config
    data = request.json or {}
    if "server_enabled" in data: api_config["server_enabled"] = bool(data["server_enabled"])
    if "allow_external" in data: api_config["allow_external"] = bool(data["allow_external"])
    if "cors_enabled" in data: api_config["cors_enabled"] = bool(data["cors_enabled"])
    if "cors_origin" in data: api_config["cors_origin"] = str(data["cors_origin"])
    save_api_config()
    return jsonify({"success": True})

@app.route("/api/config/keys/generate", methods=["POST"])
def generate_key_api():
    import uuid
    name = request.json.get("name", "New Key")
    key = "sk_agy_" + uuid.uuid4().hex[:16]
    api_config["keys"][key] = {
        "name": name,
        "created": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_api_config()
    return jsonify({"success": True, "key": key})

@app.route("/api/config/keys/delete", methods=["POST"])
def delete_key_api():
    key = request.json.get("key")
    if key in api_config["keys"]:
        del api_config["keys"][key]
        save_api_config()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Key not found"})

# Workspace File Explorer
@app.route("/api/fs/list")
def fs_list_api():
    rel_path = request.args.get("path", "")
    target_dir = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    
    # Sandbox to workspace
    if not target_dir.startswith(BASE_DIR):
        target_dir = BASE_DIR
        
    try:
        contents = []
        for name in os.listdir(target_dir):
            p = os.path.join(target_dir, name)
            is_dir = os.path.isdir(p)
            contents.append({
                "name": name,
                "isDir": is_dir,
                "sizeBytes": os.path.getsize(p) if not is_dir else 0,
                "path": os.path.relpath(p, BASE_DIR).replace("\\", "/")
            })
        return jsonify({
            "success": True,
            "current_path": os.path.relpath(target_dir, BASE_DIR).replace("\\", "/"),
            "files": sorted(contents, key=lambda x: (not x["isDir"], x["name"]))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/fs/read")
def fs_read_api():
    rel_path = request.args.get("path", "")
    target_file = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    if not target_file.startswith(BASE_DIR) or not os.path.isfile(target_file):
        return jsonify({"success": False, "error": "File not found"}), 404
        
    try:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return jsonify({"success": True, "content": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/fs/write", methods=["POST"])
def fs_write_api():
    data = request.json or {}
    rel_path = data.get("path", "")
    content = data.get("content", "")
    target_file = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    if not target_file.startswith(BASE_DIR):
        return jsonify({"success": False, "error": "Access Denied"}), 403
        
    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/fs/diff")
def fs_diff_api():
    diff = get_git_diff()
    return jsonify({"success": True, "diff": diff})

@app.route("/api/fs/cmd", methods=["POST"])
def fs_cmd_api():
    cmd = request.json.get("cmd", "")
    if not cmd:
        return jsonify({"success": False, "error": "Empty command"})
        
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=BASE_DIR, timeout=30)
        return jsonify({
            "success": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": result.returncode
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# Agent Transcript & State Monitor
@app.route("/api/agy/list")
def agy_list_conversations():
    sessions = []
    if os.path.exists(BRAIN_DIR):
        for name in os.listdir(BRAIN_DIR):
            p = os.path.join(BRAIN_DIR, name)
            if os.path.isdir(p):
                # Fetch mtime of logs to sort active first
                mtime = os.path.getmtime(p)
                status, desc = get_agent_detailed_status(name)
                sessions.append({
                    "id": name,
                    "mtime": mtime,
                    "status": status,
                    "desc": desc
                })
    return jsonify({
        "success": True,
        "conversations": sorted(sessions, key=lambda x: x["mtime"], reverse=True)
    })

@app.route("/api/agy/status")
def agy_status():
    conv_id = request.args.get("conv_id", "")
    if not conv_id:
        return jsonify({"success": False, "error": "conv_id is required"})
        
    status, desc = get_agent_detailed_status(conv_id)
    
    # Read active tasks list (simulate background CLI commands running)
    tasks = []
    tasks_dir = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "tasks")
    if os.path.exists(tasks_dir):
        for name in os.listdir(tasks_dir):
            if name.endswith(".log"):
                # Clean name
                task_id = name.replace(".log", "")
                p = os.path.join(tasks_dir, name)
                mtime = os.path.getmtime(p)
                # Check if it was updated recently
                is_running = (time.time() - mtime) < 10
                tasks.append({
                    "id": task_id,
                    "name": f"Task {task_id[:6]}",
                    "status": "RUNNING" if is_running else "COMPLETED",
                    "cmd": "Running process",
                    "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                })
                
    return jsonify({
        "success": True,
        "status": status,
        "desc": desc,
        "tasks": tasks
    })

@app.route("/api/agy/transcript")
def agy_transcript():
    conv_id = request.args.get("conv_id", "")
    if not conv_id:
        return jsonify({"success": False, "error": "conv_id is required"})
        
    log_path = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(log_path):
        return jsonify({"success": False, "error": "Transcript not found"}), 404
        
    try:
        transcript = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    transcript.append(json.loads(line))
        return jsonify({"success": True, "transcript": transcript})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/agy/send", methods=["POST"])
def agy_send_message():
    conv_id = request.json.get("conv_id")
    message = request.json.get("message", "")
    if not conv_id or not message:
        return jsonify({"success": False, "error": "conv_id and message are required"})
        
    # Queue message to transcript or print output stream.
    # For a real CLI runner, this writes to a stdin/input queue.
    # We will simulate appending user input to the transcript log.
    log_path = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(log_path):
        return jsonify({"success": False, "error": "Transcript not found"}), 404
        
    try:
        user_step = {
            "step_index": int(time.time()),
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": message
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_step) + "\n")
            
        # Send a local command notification response simulation
        # For the mock runner
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/agy/notifications")
def agy_get_notifications():
    return jsonify({
        "success": True,
        "notifications": notifications_log
    })

@app.route("/api/agy/notifications/clear", methods=["POST"])
def agy_clear_notifications():
    global notifications_log
    notifications_log = []
    return jsonify({"success": True})

# YOLO Endpoints
@app.route("/api/yolo/status")
def yolo_status():
    models_on_disk = []
    for f in os.listdir(WEIGHTS_DIR):
        if f.endswith(".pt") or f.endswith("openvino_model"):
            models_on_disk.append({
                "name": f,
                "size_mb": round(os.path.getsize(os.path.join(WEIGHTS_DIR, f)) / (1024*1024), 1) if os.path.isfile(os.path.join(WEIGHTS_DIR, f)) else 0
            })
            
    return jsonify({
        "status": app_state["status"],
        "active_model": app_state["yolo_name"],
        "model_type": app_state["yolo_type"],
        "imgsz": app_state["yolo_imgsz"],
        "conf": app_state["yolo_conf"],
        "classes": app_state["yolo_classes"],
        "error": app_state["error_message"],
        "disk_models": models_on_disk
    })

@app.route("/api/yolo/load", methods=["POST"])
def yolo_load():
    model_name = request.json.get("model_name", "yolov8n.pt")
    threading.Thread(target=load_yolo_model_fn, args=(model_name,), daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/yolo/compile", methods=["POST"])
def yolo_compile():
    model_name = request.json.get("model_name")
    format_type = request.json.get("format", "INT8") # INT8 or FP16
    if not model_name:
        return jsonify({"success": False, "error": "Model name required"})
    threading.Thread(target=compile_openvino_fn, args=(model_name, format_type), daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/yolo/configure", methods=["POST"])
def yolo_configure():
    global app_state
    data = request.json or {}
    if "imgsz" in data: app_state["yolo_imgsz"] = int(data["imgsz"])
    if "conf" in data: app_state["yolo_conf"] = float(data["conf"])
    if "classes" in data:
        raw_classes = data["classes"]
        if isinstance(raw_classes, str):
            app_state["yolo_classes"] = [c.strip() for c in raw_classes.split(",") if c.strip()]
        elif isinstance(raw_classes, list):
            app_state["yolo_classes"] = [str(c).strip() for c in raw_classes if str(c).strip()]
            
        # Re-apply classes if YOLO-World
        if app_state["yolo_model"] is not None and app_state["yolo_type"] == "world":
            with model_lock:
                app_state["yolo_model"].set_classes(app_state["yolo_classes"])
                
    return jsonify({"success": True})

@app.route("/api/yolo/upload_video", methods=["POST"])
def yolo_upload_video():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400
        
    filename = "input.mp4"
    video_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(video_path)
    
    # Trigger background detections cache build
    frame_skip = int(request.form.get("frame_skip", 2))
    threading.Thread(target=analyze_video_thread, args=(filename, frame_skip), daemon=True).start()
    
    return jsonify({"success": True, "filename": filename})

@app.route("/api/yolo/detections")
def yolo_detections():
    filename = request.args.get("filename", "input.mp4")
    if filename not in detections_cache:
        return jsonify({"success": False, "error": "Video not analyzed yet"}), 404
    return jsonify({
        "success": True,
        "progress": detections_cache[filename]["progress"],
        "fps": detections_cache[filename]["fps"],
        "width": detections_cache[filename]["width"],
        "height": detections_cache[filename]["height"],
        "detections": detections_cache[filename]["frames"]
    })

# API Detect Image (Single frame inference)
@app.route("/api/detect", methods=["POST"])
def api_detect_image():
    if app_state["yolo_model"] is None:
        return jsonify({"error": "No active YOLO model loaded"}), 400
        
    # Read file bytes
    file_bytes = None
    if "file" in request.files:
        file_bytes = request.files["file"].read()
    else:
        # Check base64
        import base64
        data = request.json or {}
        image_b64 = data.get("image")
        if image_b64:
            if "," in image_b64:
                image_b64 = image_b64.split(",")[1]
            file_bytes = base64.b64decode(image_b64)
            
    if not file_bytes:
        return jsonify({"error": "No image data supplied"}), 400
        
    try:
        # Decode image
        np_arr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        
        # Inference
        with model_lock:
            results = app_state["yolo_model"](img, imgsz=app_state["yolo_imgsz"], conf=app_state["yolo_conf"], verbose=False)
            
        boxes_data = []
        if len(results) > 0:
            boxes = results[0].boxes
            for box in boxes:
                xyxy = box.xyxy[0].cpu().numpy()
                x1, y1, x2, y2 = map(int, xyxy)
                conf = float(box.conf[0].cpu().numpy())
                cls_id = int(box.cls[0].cpu().numpy())
                label = results[0].names[cls_id]
                boxes_data.append({
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "label": label,
                    "conf": conf
                })
        return jsonify({"success": True, "detections": boxes_data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# TTS Endpoints
@app.route("/api/tts/status")
def tts_status():
    return jsonify({
        "status": "READY" if app_state["tts_model"] else "NOT_LOADED",
        "model_name": app_state["tts_name"],
        "speakers": ["aidar", "baya", "kseniya", "xenia", "eugene"]
    })

@app.route("/api/tts/load", methods=["POST"])
def tts_load():
    threading.Thread(target=load_tts_model_fn, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/tts", methods=["POST", "GET"])
def tts_generate():
    load_tts_model_fn()
    if app_state["tts_model"] is None:
        return jsonify({"error": "TTS engine not loaded. Please wait, downloading..."}), 400
        
    text = ""
    speaker = "aidar"
    sample_rate = 24000
    
    if request.method == "POST":
        data = request.json or {}
        text = data.get("text", "")
        speaker = data.get("speaker", "aidar")
        sample_rate = int(data.get("sample_rate", 24000))
    else:
        text = request.args.get("text", "")
        speaker = request.args.get("speaker", "aidar")
        sample_rate = int(request.args.get("sample_rate", 24000))
        
    if not text:
        return jsonify({"error": "Text is required"}), 400
        
    try:
        device = torch.device("cpu")
        with model_lock:
            audio = app_state["tts_model"].apply_tts(text=text, speaker=speaker, sample_rate=sample_rate)
            
        # Convert to WAV bytes
        audio_data = audio.cpu().numpy()
        audio_data = np.clip(audio_data, -1.0, 1.0)
        audio_int16 = (audio_data * 32767).astype(np.int16)
        
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_int16.tobytes())
            
        return Response(wav_io.getvalue(), mimetype="audio/wav")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- Premium Material Design 3 HTML Frontend -----------------

HTML_UI = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connect Antigravity Portal</title>
    <!-- Modern Material Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined" rel="stylesheet">
    <style>
        :root {
            --md-sys-color-primary: #0a84ff;
            --md-sys-color-on-primary: #ffffff;
            --md-sys-color-surface: #1c1c1e;
            --md-sys-color-background: #000000;
            --md-sys-color-on-surface: #e5e5ea;
            --md-sys-color-on-surface-variant: #8e8e93;
            --md-sys-color-outline: #2c2c2e;
            --md-sys-color-error: #ff453a;
            --md-sys-color-success: #30d158;
            --md-shape-corner-medium: 12px;
            --md-shape-corner-large: 16px;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Google Sans', 'Product Sans', 'Plus Jakarta Sans', sans-serif;
            background-color: var(--md-sys-color-background);
            color: var(--md-sys-color-on-surface);
            display: flex;
            height: 100vh;
            overflow: hidden;
        }

        /* Sidebar Spotify component placement style */
        .sidebar {
            width: 280px;
            background-color: #121214;
            border-right: 1px solid var(--md-sys-color-outline);
            display: flex;
            flex-direction: column;
            padding: 20px;
            justify-content: space-between;
        }

        .sidebar-top {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        /* Custom top-left toggle switch */
        .mode-switch-wrapper {
            display: flex;
            align-items: center;
            background-color: var(--md-sys-color-outline);
            padding: 4px;
            border-radius: 20px;
            width: 100%;
        }

        .mode-btn {
            flex: 1;
            padding: 8px 12px;
            border-radius: 16px;
            border: none;
            background: transparent;
            color: var(--md-sys-color-on-surface-variant);
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.2s ease;
        }

        .mode-btn.active {
            background-color: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
        }

        .nav-menu {
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin-top: 10px;
        }

        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
            border-radius: var(--md-shape-corner-medium);
            color: var(--md-sys-color-on-surface-variant);
            text-decoration: none;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s, color 0.2s;
        }

        .nav-item:hover {
            background-color: rgba(255, 255, 255, 0.05);
            color: var(--md-sys-color-on-surface);
        }

        .nav-item.active {
            background-color: var(--md-sys-color-outline);
            color: var(--md-sys-color-primary);
        }

        .nav-item span.material-symbols-outlined {
            font-size: 22px;
        }

        .active-model-card {
            background-color: var(--md-sys-color-surface);
            border-radius: var(--md-shape-corner-medium);
            padding: 14px;
            border: 1px solid var(--md-sys-color-outline);
        }

        .active-model-card h4 {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--md-sys-color-on-surface-variant);
            margin-bottom: 6px;
        }

        .active-model-card p {
            font-size: 13px;
            font-weight: 700;
            color: #fff;
        }

        /* Main Viewport */
        .main-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            background-color: var(--md-sys-color-background);
            position: relative;
        }

        .tab-content {
            flex: 1;
            display: none;
            flex-direction: column;
            padding: 24px;
            overflow-y: auto;
        }

        .tab-content.active {
            display: flex;
        }

        /* Header UI */
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
        }

        .panel-header h2 {
            font-size: 24px;
            fontWeight: 800;
            letter-spacing: -0.5px;
        }

        /* Material Grid Layouts */
        .grid-layout {
            display: grid;
            grid-template-columns: 1.3fr 1fr;
            gap: 24px;
        }

        .card {
            background-color: var(--md-sys-color-surface);
            border-radius: var(--md-shape-corner-large);
            border: 1px solid var(--md-sys-color-outline);
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            position: relative;
        }

        .card h3 {
            font-size: 18px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* Buttons & Inputs (Material 3 standard) */
        .md-input {
            width: 100%;
            background-color: var(--md-sys-color-background);
            border: 1px solid var(--md-sys-color-outline);
            color: var(--md-sys-color-on-surface);
            border-radius: var(--md-shape-corner-medium);
            padding: 12px 16px;
            font-family: inherit;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }

        .md-input:focus {
            border-color: var(--md-sys-color-primary);
        }

        .md-btn {
            background-color: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
            border: none;
            border-radius: 20px;
            padding: 12px 24px;
            font-family: inherit;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: opacity 0.2s;
        }

        .md-btn:hover {
            opacity: 0.9;
        }

        .md-btn-outline {
            background-color: transparent;
            border: 1px solid var(--md-sys-color-outline);
            color: var(--md-sys-color-on-surface);
        }

        .md-btn-outline:hover {
            background-color: rgba(255, 255, 255, 0.05);
        }

        /* Video Player Wrapper */
        .video-wrapper {
            position: relative;
            width: 100%;
            aspect-ratio: 16/9;
            background-color: #000;
            border-radius: var(--md-shape-corner-medium);
            overflow: hidden;
            border: 1px solid var(--md-sys-color-outline);
        }

        .video-wrapper video {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }

        .video-wrapper canvas {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 5;
        }

        /* Explorer UI */
        .explorer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background-color: var(--md-sys-color-surface);
            padding: 12px 16px;
            border-radius: var(--md-shape-corner-medium);
            border: 1px solid var(--md-sys-color-outline);
            margin-bottom: 12px;
        }

        .explorer-list {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .explorer-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 16px;
            background-color: var(--md-sys-color-surface);
            border: 1px solid var(--md-sys-color-outline);
            border-radius: var(--md-shape-corner-medium);
            cursor: pointer;
            transition: background-color 0.2s;
        }

        .explorer-row:hover {
            background-color: rgba(255, 255, 255, 0.04);
        }

        .explorer-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .explorer-row span.material-symbols-outlined {
            font-size: 24px;
        }

        /* Console UI */
        .terminal-box {
            background-color: #0a0a0c;
            border-radius: var(--md-shape-corner-medium);
            padding: 16px;
            font-family: 'Roboto Mono', monospace;
            font-size: 13px;
            color: var(--md-sys-color-success);
            height: 350px;
            overflow-y: auto;
            border: 1px solid var(--md-sys-color-outline);
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .terminal-in { color: #fff; font-weight: 700; }
        .terminal-err { color: var(--md-sys-color-error); }

        /* API Metrics */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
        }

        .metric-tile {
            background-color: var(--md-sys-color-surface);
            border-radius: var(--md-shape-corner-medium);
            border: 1px solid var(--md-sys-color-outline);
            padding: 16px;
            text-align: center;
        }

        .metric-tile h4 {
            font-size: 11px;
            color: var(--md-sys-color-on-surface-variant);
            text-transform: uppercase;
            margin-bottom: 6px;
        }

        .metric-tile p {
            font-size: 20px;
            font-weight: 800;
        }

        /* Table */
        .md-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }

        .md-table th, .md-table td {
            text-align: left;
            padding: 12px;
            border-bottom: 1px solid var(--md-sys-color-outline);
        }

        .md-table th {
            color: var(--md-sys-color-on-surface-variant);
            font-weight: 600;
        }

        /* Chats history list */
        .chat-container {
            display: flex;
            flex-direction: column;
            height: 480px;
            border: 1px solid var(--md-sys-color-outline);
            border-radius: var(--md-shape-corner-medium);
            overflow: hidden;
            background-color: #0b0b0c;
        }

        .chat-messages {
            flex: 1;
            padding: 16px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .chat-bubble {
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 18px;
            font-size: 14px;
            line-height: 18px;
        }

        .chat-bubble.user {
            background-color: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
            align-self: flex-end;
            border-bottom-right-radius: 2px;
        }

        .chat-bubble.agent {
            background-color: var(--md-sys-color-surface);
            color: var(--md-sys-color-on-surface);
            align-self: flex-start;
            border-bottom-left-radius: 2px;
        }

        /* Quick classes tags */
        .tag-container {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }

        .tag-pill {
            background-color: var(--md-sys-color-outline);
            color: #fff;
            padding: 6px 12px;
            border-radius: 14px;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .tag-pill:hover {
            background-color: var(--md-sys-color-error);
        }

        /* Modal styling */
        .modal {
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background-color: rgba(0,0,0,0.85);
            z-index: 100;
            display: none;
            align-items: center;
            justify-content: center;
        }

        .modal.active { display: flex; }

        .modal-card {
            background-color: var(--md-sys-color-surface);
            border: 1px solid var(--md-sys-color-outline);
            border-radius: var(--md-shape-corner-large);
            width: 80%;
            max-width: 800px;
            max-height: 80%;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--md-sys-color-outline);
            padding-bottom: 12px;
        }

        .modal-body {
            flex: 1;
            overflow-y: auto;
        }

        .modal-body pre {
            font-family: 'Roboto Mono', monospace;
            font-size: 12px;
            color: #ececf2;
            white-space: pre-wrap;
        }
    </style>
</head>
<body>

    <!-- LEFT SIDEBAR -->
    <div class="sidebar">
        <div class="sidebar-top">
            <!-- Switches between YOLO and TTS modes -->
            <div class="mode-switch-wrapper">
                <button class="mode-btn active" id="mode-btn-yolo" onclick="switchDomainMode('yolo')">
                    <span class="material-symbols-outlined">center_focus_weak</span>
                    YOLO
                </button>
                <button class="mode-btn" id="mode-btn-tts" onclick="switchDomainMode('tts')">
                    <span class="material-symbols-outlined">record_voice_over</span>
                    TTS
                </button>
            </div>

            <!-- Navigation Links -->
            <div class="nav-menu">
                <div class="nav-item active" id="nav-primary" onclick="showTab('primary')">
                    <span class="material-symbols-outlined" id="nav-primary-icon">center_focus_weak</span>
                    <span id="nav-primary-text">YOLO Детектор</span>
                </div>
                <div class="nav-item" id="nav-companion" onclick="showTab('companion')">
                    <span class="material-symbols-outlined">settings_ethernet</span>
                    Connect Панель
                </div>
                <div class="nav-item" id="nav-models" onclick="showTab('models')">
                    <span class="material-symbols-outlined">layers</span>
                    Менеджер моделей
                </div>
                <div class="nav-item" id="nav-api" onclick="showTab('api')">
                    <span class="material-symbols-outlined">api</span>
                    Настройки API
                </div>
            </div>
        </div>

        <!-- Active model info -->
        <div class="active-model-card">
            <h4>Активный движок</h4>
            <p id="sidebar-active-model">Инициализация...</p>
        </div>
    </div>

    <!-- MAIN PANEL -->
    <div class="main-panel">
        
        <!-- ================= YOLO Tab Content ================= -->
        <div class="tab-content active" id="tab-yolo">
            <div class="panel-header">
                <h2>YOLO Анализатор видео</h2>
            </div>
            
            <div class="grid-layout">
                <!-- Video Player and canvas overlay -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">video_file</span> Нейросетевой монитор</h3>
                    
                    <div class="video-wrapper">
                        <video id="native-video" controls muted></video>
                        <canvas id="canvas-boxes"></canvas>
                    </div>

                    <div style="display: flex; gap: 12px;">
                        <input type="file" id="video-file-input" accept="video/mp4" style="display: none;" onchange="uploadVideoFile()">
                        <button class="md-btn" onclick="document.getElementById('video-file-input').click()">
                            <span class="material-symbols-outlined">upload_file</span> Загрузить видео (.mp4)
                        </button>
                        <div id="video-progress-text" style="align-self: center; font-size: 13px; color: var(--md-sys-color-on-surface-variant);"></div>
                    </div>
                </div>

                <!-- Detection settings panel -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">tune</span> Параметры детекции</h3>
                    
                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Порог уверенности:</label>
                        <input type="range" id="yolo-conf-slider" min="0.05" max="0.95" step="0.05" value="0.25" onchange="updateYoloSettings()">
                        <span id="yolo-conf-value" style="font-size:13px; font-weight:700;">0.25</span>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Размер кадра (imgsz):</label>
                        <select class="md-input" id="yolo-imgsz-select" onchange="updateYoloSettings()">
                            <option value="256">256 (Макс. скорость)</option>
                            <option value="320" selected>320 (Баланс)</option>
                            <option value="640">640 (Качество)</option>
                        </select>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Классы для отслеживания (через запятую):</label>
                        <input type="text" class="md-input" id="yolo-classes-input" value="person, car, bicycle, dog, laptop" onchange="updateYoloSettings()">
                        <div class="tag-container" id="yolo-tags-list"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= TTS Tab Content ================= -->
        <div class="tab-content" id="tab-tts">
            <div class="panel-header">
                <h2>Синтез речи Silero TTS v5.5</h2>
            </div>
            
            <div class="grid-layout">
                <!-- Text inputs -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">chat</span> Ввод текста</h3>
                    <textarea class="md-input" id="tts-text-input" rows="6" placeholder="Привет, бро! Как дела? Введи любой русский текст, который нужно озвучить..."></textarea>
                    
                    <div style="display: flex; justify-content: flex-end; gap: 12px;">
                        <button class="md-btn" id="tts-gen-btn" onclick="generateSpeech()">
                            <span class="material-symbols-outlined">play_circle</span> Озвучить текст
                        </button>
                    </div>
                </div>

                <!-- Voice settings -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">settings_voice</span> Параметры голоса</h3>
                    
                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Спикер (Голос):</label>
                        <select class="md-input" id="tts-speaker-select">
                            <option value="aidar">Aidar (Мужской)</option>
                            <option value="eugene">Eugene (Мужской)</option>
                            <option value="baya">Baya (Женский)</option>
                            <option value="kseniya">Kseniya (Женский)</option>
                            <option value="xenia">Xenia (Женский)</option>
                        </select>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Частота дискретизации:</label>
                        <select class="md-input" id="tts-sr-select">
                            <option value="8000">8000 Hz</option>
                            <option value="24000" selected>24000 Hz (Оптимально)</option>
                            <option value="48000">48000 Hz (Максимум)</option>
                        </select>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 10px; margin-top: 10px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Плеер воспроизведения:</label>
                        <audio id="tts-audio-player" controls style="width: 100%;"></audio>
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= Connect Tab Content ================= -->
        <div class="tab-content" id="tab-companion">
            <div class="panel-header">
                <h2>Connect Antigravity Панель</h2>
            </div>
            
            <div class="grid-layout">
                <!-- Agent logs explorer and commands -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">list_alt</span> Журнал выполнения сессии</h3>
                    
                    <div class="chat-container">
                        <div class="chat-messages" id="chat-messages-box">
                            <!-- Messages rendered dynamically -->
                        </div>
                    </div>

                    <div style="display: flex; gap: 12px;">
                        <input type="text" class="md-input" id="agent-command-input" placeholder="Отправить команду агенту (e.g. проверь код)..." onkeypress="if(event.key === 'Enter') sendAgentMessage()">
                        <button class="md-btn" onclick="sendAgentMessage()">
                            <span class="material-symbols-outlined">send</span>
                        </button>
                    </div>
                </div>

                <!-- File Manager & System tasks list -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">folder_open</span> Файловая система проекта</h3>
                    
                    <div class="explorer-header">
                        <span id="current-fs-path">/</span>
                        <div style="display: flex; gap: 8px;">
                            <button class="md-btn md-btn-outline" style="padding: 6px 12px; border-radius: 8px;" onclick="fetchGitDiffUI()">Diff</button>
                            <button class="md-btn md-btn-outline" style="padding: 6px 12px; border-radius: 8px;" id="fs-up-btn" onclick="navigateFsUp()">
                                <span class="material-symbols-outlined" style="font-size:16px;">arrow_upward</span>
                            </button>
                        </div>
                    </div>

                    <div class="explorer-list" id="explorer-files-list" style="height: 300px; overflow-y: auto;">
                        <!-- Files listed dynamically -->
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= Models Tab Content ================= -->
        <div class="tab-content" id="tab-models">
            <div class="panel-header">
                <h2>Управление локальными моделями</h2>
            </div>
            
            <div class="grid-layout">
                <!-- Models table -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">storage</span> Доступные модели на диске</h3>
                    
                    <table class="md-table">
                        <thead>
                            <tr>
                                <th>Имя модели</th>
                                <th>Размер</th>
                                <th>Действие</th>
                            </tr>
                        </thead>
                        <tbody id="models-list-body">
                            <!-- Models rows listed dynamically -->
                        </tbody>
                    </table>
                </div>

                <!-- Download / Compiler panel -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">build_circle</span> Загрузка и компиляция OpenVINO</h3>
                    
                    <div style="display: flex; flex-direction: column; gap: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Загрузить пресет с CDN:</label>
                        <select class="md-input" id="model-download-select">
                            <option value="yolov8n.pt">YOLOv8 Nano (6MB)</option>
                            <option value="yolov8s.pt">YOLOv8 Small (22MB)</option>
                            <option value="yolo11n.pt">YOLO11 Nano (5.6MB)</option>
                            <option value="yolo11s.pt">YOLO11 Small (19MB)</option>
                            <option value="yolov8s-worldv2.pt">YOLO-World v2 Small (25MB)</option>
                        </select>
                        <button class="md-btn md-btn-outline" onclick="triggerModelLoad()">
                            <span class="material-symbols-outlined">download</span> Скачать / Активировать
                        </button>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 12px;">
                        <label style="font-size:12px; color:var(--md-sys-color-on-surface-variant);">Скомпилировать в OpenVINO CPU:</label>
                        <select class="md-input" id="model-compile-format">
                            <option value="INT8">INT8 Квантование (Core i3-8130U)</option>
                            <option value="FP16">FP16 Точность</option>
                        </select>
                        <button class="md-btn" onclick="triggerModelCompile()">
                            <span class="material-symbols-outlined">rocket_launch</span> Оптимизировать OpenVINO
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= API Tab Content ================= -->
        <div class="tab-content" id="tab-api">
            <div class="panel-header">
                <h2>Панель настроек сервера API</h2>
            </div>
            
            <div class="card" style="margin-bottom: 24px;">
                <h3><span class="material-symbols-outlined">settings</span> Конфигурация API сервера</h3>
                <div style="display: flex; gap: 24px; flex-wrap: wrap;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="api-server-enabled" onchange="updateApiConfig()">
                        <label for="api-server-enabled">Включить API доступ</label>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="api-allow-external" onchange="updateApiConfig()">
                        <label for="api-allow-external">Разрешить внешние запросы (0.0.0.0)</label>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="api-cors-enabled" onchange="updateApiConfig()">
                        <label for="api-cors-enabled">Включить CORS</label>
                    </div>
                </div>
            </div>

            <div class="metrics-grid" style="margin-bottom: 24px;">
                <div class="metric-tile">
                    <h4>Запросов выполнено</h4>
                    <p id="metric-total">0</p>
                </div>
                <div class="metric-tile">
                    <h4>Активные запросы</h4>
                    <p id="metric-active">0</p>
                </div>
                <div class="metric-tile">
                    <h4>Средний отклик (ms)</h4>
                    <p id="metric-speed">0</p>
                </div>
            </div>

            <div class="grid-layout">
                <!-- API Keys crud -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">key</span> Управление API ключами</h3>
                    
                    <div style="display: flex; gap: 12px;">
                        <input type="text" class="md-input" id="key-name-input" placeholder="Имя ключа (e.g. iPhone Client)">
                        <button class="md-btn" onclick="generateApiKey()">Создать ключ</button>
                    </div>

                    <table class="md-table">
                        <thead>
                            <tr>
                                <th>Имя</th>
                                <th>Создан</th>
                                <th>Токен</th>
                                <th>Удалить</th>
                            </tr>
                        </thead>
                        <tbody id="keys-list-body">
                            <!-- Keys row listed dynamically -->
                        </tbody>
                    </table>
                </div>

                <!-- Logs and commands history -->
                <div class="card">
                    <h3><span class="material-symbols-outlined">terminal</span> Журнал вызовов API</h3>
                    <div class="terminal-box" id="api-logs-box">
                        <!-- Logs printed dynamically -->
                    </div>
                </div>
            </div>
        </div>

    </div>

    <!-- Modals -->
    <div class="modal" id="file-modal">
        <div class="modal-card">
            <div class="modal-header">
                <h3 id="file-modal-title">Просмотр файла</h3>
                <button class="md-btn md-btn-outline" style="padding: 6px 12px; border-radius: 8px;" onclick="closeFileModal()">Закрыть</button>
            </div>
            <div class="modal-body">
                <pre><code id="file-modal-content"></code></pre>
            </div>
        </div>
    </div>

    <div class="modal" id="diff-modal">
        <div class="modal-card">
            <div class="modal-header">
                <h3>Git Diff изменений в коде</h3>
                <button class="md-btn md-btn-outline" style="padding: 6px 12px; border-radius: 8px;" onclick="closeDiffModal()">Закрыть</button>
            </div>
            <div class="modal-body">
                <pre><code id="diff-modal-content" style="color: var(--md-sys-color-success);"></code></pre>
            </div>
        </div>
    </div>

    <script>
        // Active Navigation Contexts
        let currentDomain = 'yolo'; // yolo, tts
        let currentFsPath = '';
        let selectedConversationId = null;

        // Video and canvas elements
        const video = document.getElementById('native-video');
        const canvas = document.getElementById('canvas-boxes');
        const ctx = canvas.getContext('2d');
        let currentDetections = {};
        let detectionsActive = false;

        // Domain Switcher
        function switchDomainMode(mode) {
            currentDomain = mode;
            document.getElementById('mode-btn-yolo').classList.toggle('active', mode === 'yolo');
            document.getElementById('mode-btn-tts').classList.toggle('active', mode === 'tts');
            
            const navPrimary = document.getElementById('nav-primary');
            const navPrimaryText = document.getElementById('nav-primary-text');
            const navPrimaryIcon = document.getElementById('nav-primary-icon');

            if (mode === 'yolo') {
                navPrimaryText.textContent = 'YOLO Детектор';
                navPrimaryIcon.textContent = 'center_focus_weak';
                showTab('primary');
            } else {
                navPrimaryText.textContent = 'TTS Синтезатор';
                navPrimaryIcon.textContent = 'record_voice_over';
                showTab('primary');
            }
        }

        // Navigation Tabs show/hide
        function showTab(tabName) {
            // Remove active classes
            document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

            if (tabName === 'primary') {
                document.getElementById('nav-primary').classList.add('active');
                if (currentDomain === 'yolo') {
                    document.getElementById('tab-yolo').classList.add('active');
                } else {
                    document.getElementById('tab-tts').classList.add('active');
                }
            } 
            else if (tabName === 'companion') {
                document.getElementById('nav-companion').classList.add('active');
                document.getElementById('tab-companion').classList.add('active');
                fetchConversationsList();
                fetchFilesList('');
            } 
            else if (tabName === 'models') {
                document.getElementById('nav-models').classList.add('active');
                document.getElementById('tab-models').classList.add('active');
                fetchModelsList();
            } 
            else if (tabName === 'api') {
                document.getElementById('nav-api').classList.add('active');
                document.getElementById('tab-api').classList.add('active');
                fetchApiMetrics();
            }
        }

        // YOLO Settings updates
        async function updateYoloSettings() {
            const conf = document.getElementById('yolo-conf-slider').value;
            document.getElementById('yolo-conf-value').textContent = conf;
            const imgsz = document.getElementById('yolo-imgsz-select').value;
            const classes = document.getElementById('yolo-classes-input').value;

            // Render tags
            const tagsList = document.getElementById('yolo-tags-list');
            tagsList.innerHTML = '';
            classes.split(',').forEach(c => {
                const clean = c.trim();
                if (clean) {
                    const tag = document.createElement('div');
                    tag.className = 'tag-pill';
                    tag.innerHTML = `${clean} <span class="material-symbols-outlined" style="font-size:12px;">close</span>`;
                    tag.onclick = () => removeYoloClassTag(clean);
                    tagsList.appendChild(tag);
                }
            });

            await fetch('/api/yolo/configure', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ imgsz, conf, classes })
            });
        }

        function removeYoloClassTag(clsName) {
            const input = document.getElementById('yolo-classes-input');
            const items = input.value.split(',').map(c => c.trim()).filter(c => c !== clsName);
            input.value = items.join(', ');
            updateYoloSettings();
        }

        // Video uploads and processing
        async function uploadVideoFile() {
            const fileInput = document.getElementById('video-file-input');
            if (fileInput.files.length === 0) return;

            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            formData.append('frame_skip', '2'); // Process every 2nd frame

            document.getElementById('video-progress-text').textContent = 'Загрузка...';
            
            try {
                const response = await fetch('/api/yolo/upload_video', {
                    method: 'POST',
                    body: formData
                });
                const res = await response.json();
                if (res.success) {
                    video.src = '/uploads/input.mp4';
                    video.load();
                    startDetectionsPolling();
                }
            } catch (e) {
                document.getElementById('video-progress-text').textContent = 'Ошибка загрузки';
            }
        }

        // Poll video bounding box coordinates cache
        let pollTimer = null;
        function startDetectionsPolling() {
            if (pollTimer) clearInterval(pollTimer);
            
            pollTimer = setInterval(async () => {
                try {
                    const response = await fetch('/api/yolo/detections?filename=input.mp4');
                    const res = await response.json();
                    if (res.success) {
                        currentDetections = res.detections;
                        document.getElementById('video-progress-text').textContent = `Анализ видео: ${res.progress}%`;
                        detectionsActive = true;
                        
                        if (res.progress === 100) {
                            clearInterval(pollTimer);
                            document.getElementById('video-progress-text').textContent = `Анализ завершен успешно!`;
                        }
                    }
                } catch(e) {
                    // Not ready
                }
            }, 2000);
        }

        // Draw bounding boxes synchronously matched to HTML5 Video current playback timestamp
        function drawOverlayBoxes() {
            if (!detectionsActive || video.paused || video.ended) {
                requestAnimationFrame(drawOverlayBoxes);
                return;
            }

            // Sync canvas sizing exactly to match responsive video player boundary
            const rect = video.getBoundingClientRect();
            if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
            }

            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Get current time in ms
            const currentMs = Math.floor(video.currentTime * 1000);
            
            // Find nearest cached frame detections
            // Detections keys are string timestamps (ms)
            let nearestKey = null;
            let minDiff = Infinity;
            
            Object.keys(currentDetections).forEach(k => {
                const diff = Math.abs(parseInt(k) - currentMs);
                if (diff < minDiff && diff < 200) {
                    minDiff = diff;
                    nearestKey = k;
                }
            });

            if (nearestKey) {
                const boxes = currentDetections[nearestKey];
                const colors = ['#0a84ff', '#ff9500', '#30d158', '#ff453a', '#bf5af2', '#ff375f'];
                
                boxes.forEach(box => {
                    // Coordinates are saved as percentages
                    const x1 = box.x1 * canvas.width;
                    const y1 = box.y1 * canvas.height;
                    const x2 = box.x2 * canvas.width;
                    const y2 = box.y2 * canvas.height;
                    const w = x2 - x1;
                    const h = y2 - y1;
                    const color = colors[box.color_idx % colors.length];

                    // Draw thin flat box (Material Style - no neons, clean borders)
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 2;
                    ctx.strokeRect(x1, y1, w, h);

                    // Fill clean semi-transparent box
                    ctx.fillStyle = color + '15';
                    ctx.fillRect(x1, y1, w, h);

                    // Draw label
                    const labelText = `${box.label} ${(box.conf * 100).toFixed(0)}%`;
                    ctx.font = '700 12px Plus Jakarta Sans';
                    const textW = ctx.measureText(labelText).width;
                    ctx.fillStyle = color;
                    ctx.fillRect(x1 - 1, y1 - 20, textW + 12, 20);

                    ctx.fillStyle = '#ffffff';
                    ctx.fillText(labelText, x1 + 5, y1 - 6);
                });
            }

            requestAnimationFrame(drawOverlayBoxes);
        }

        // Trigger loop on native HTML5 Video play events
        video.addEventListener('play', () => {
            requestAnimationFrame(drawOverlayBoxes);
        });

        // Silero TTS Speech Synthesis
        async function generateSpeech() {
            const text = document.getElementById('tts-text-input').value;
            const speaker = document.getElementById('tts-speaker-select').value;
            const sample_rate = document.getElementById('tts-sr-select').value;
            const btn = document.getElementById('tts-gen-btn');

            if (!text.trim()) return;

            btn.disabled = true;
            btn.innerHTML = 'Генерация...';

            try {
                const response = await fetch(`/api/tts?text=${encodeURIComponent(text)}&speaker=${speaker}&sample_rate=${sample_rate}`);
                if (!response.ok) {
                    const err = await response.json();
                    alert(`Ошибка: ${err.error}`);
                    return;
                }
                const audioBlob = await response.blob();
                const audioUrl = URL.createObjectURL(audioBlob);
                const player = document.getElementById('tts-audio-player');
                player.src = audioUrl;
                player.play();
            } catch(e) {
                alert('Не удалось связаться с сервером TTS');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-outlined">play_circle</span> Озвучить текст';
            }
        }

        // Connect Agent Conversations Lists
        async function fetchConversationsList() {
            const response = await fetch('/api/agy/list');
            const res = await response.json();
            if (res.success && res.conversations.length > 0) {
                // Connect to first active session automatically
                selectedConversationId = res.conversations[0].id;
                startAgentLogPolling();
            }
        }

        let agentLogTimer = null;
        function startAgentLogPolling() {
            if (agentLogTimer) clearInterval(agentLogTimer);
            fetchAgentTranscript();
            agentLogTimer = setInterval(fetchAgentTranscript, 3000);
        }

        async function fetchAgentTranscript() {
            if (!selectedConversationId) return;
            const response = await fetch(`/api/agy/transcript?conv_id=${selectedConversationId}`);
            const res = await response.json();
            if (res.success) {
                const chatBox = document.getElementById('chat-messages-box');
                chatBox.innerHTML = '';
                res.transcript.forEach(step => {
                    const isUser = step.source === 'USER_EXPLICIT' || step.type === 'USER_INPUT';
                    let content = step.content || '';
                    if (typeof content !== 'string') content = JSON.stringify(content);

                    if (!content.trim() && step.tool_calls) {
                        content = `Вызовы инструментов: ${step.tool_calls.map(tc => tc.name).join(', ')}`;
                    }

                    if (content.trim()) {
                        const bubble = document.createElement('div');
                        bubble.className = `chat-bubble ${isUser ? 'user' : 'agent'}`;
                        bubble.textContent = content;
                        chatBox.appendChild(bubble);
                    }
                });
                chatBox.scrollTop = chatBox.scrollHeight;
            }
        }

        async function sendAgentMessage() {
            const input = document.getElementById('agent-command-input');
            const msg = input.value.trim();
            if (!msg || !selectedConversationId) return;
            input.value = '';

            await fetch('/api/agy/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ conv_id: selectedConversationId, message: msg })
            });
            fetchAgentTranscript();
        }

        // Workspace Files Listing
        async function fetchFilesList(path) {
            const response = await fetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
            const res = await response.json();
            if (res.success) {
                currentFsPath = res.current_path;
                document.getElementById('current-fs-path').textContent = '/' + currentFsPath;
                
                const list = document.getElementById('explorer-files-list');
                list.innerHTML = '';
                
                res.files.forEach(f => {
                    const row = document.createElement('div');
                    row.className = 'explorer-row';
                    row.onclick = () => {
                        if (f.isDir) {
                            fetchFilesList(f.path);
                        } else {
                            openFileContent(f.path);
                        }
                    };
                    
                    row.innerHTML = `
                        <div class="explorer-left">
                            <span class="material-symbols-outlined" style="color: ${f.isDir ? '#ff9500' : '#8e8e93'};">
                                ${f.isDir ? 'folder' : 'description'}
                            </span>
                            <span>${f.name}</span>
                        </div>
                        <span style="font-size: 11px; color: var(--md-sys-color-on-surface-variant);">
                            ${f.isDir ? '' : (f.sizeBytes / 1024).toFixed(1) + ' KB'}
                        </span>
                    `;
                    list.appendChild(row);
                });
            }
        }

        function navigateFsUp() {
            const parts = currentFsPath.split('/');
            parts.pop();
            fetchFilesList(parts.join('/'));
        }

        async function openFileContent(filePath) {
            const response = await fetch(`/api/fs/read?path=${encodeURIComponent(filePath)}`);
            const res = await response.json();
            if (res.success) {
                document.getElementById('file-modal-title').textContent = filePath.split('/').pop();
                document.getElementById('file-modal-content').textContent = res.content;
                document.getElementById('file-modal').classList.add('active');
            }
        }

        function closeFileModal() {
            document.getElementById('file-modal').classList.remove('active');
        }

        async function fetchGitDiffUI() {
            const response = await fetch('/api/fs/diff');
            const res = await response.json();
            if (res.success) {
                document.getElementById('diff-modal-content').textContent = res.diff || 'Нет незафиксированных изменений.';
                document.getElementById('diff-modal').classList.add('active');
            }
        }

        function closeDiffModal() {
            document.getElementById('diff-modal').classList.remove('active');
        }

        // Models Manager List
        async function fetchModelsList() {
            const response = await fetch('/api/yolo/status');
            const res = await response.json();
            
            // Sidebar status reload
            document.getElementById('sidebar-active-model').textContent = res.active_model ? `${res.active_model} [${res.model_type.toUpperCase()}]` : 'Модель не загружена';
            
            const tbody = document.getElementById('models-list-body');
            tbody.innerHTML = '';
            
            res.disk_models.forEach(m => {
                const tr = document.createElement('tr');
                const active = res.active_model === m.name;
                tr.innerHTML = `
                    <td style="font-weight: ${active ? 'bold' : 'normal'}; color: ${active ? 'var(--md-sys-color-primary)' : 'inherit'}">${m.name}</td>
                    <td>${m.size_mb} MB</td>
                    <td>
                        <button class="md-btn md-btn-outline" style="padding: 6px 12px; font-size:12px; border-radius: 12px;" onclick="loadModel('${m.name}')" ${active ? 'disabled' : ''}>
                            ${active ? 'Активна' : 'Активировать'}
                        </button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function loadModel(modelName) {
            await fetch('/api/yolo/load', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_name: modelName })
            });
            setTimeout(fetchModelsList, 3000);
        }

        async function triggerModelLoad() {
            const name = document.getElementById('model-download-select').value;
            loadModel(name);
        }

        async function triggerModelCompile() {
            const name = document.getElementById('model-download-select').value;
            const format = document.getElementById('model-compile-format').value;
            
            await fetch('/api/yolo/compile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_name: name, format })
            });
            alert('Сборка OpenVINO запущена в фоновом режиме!');
        }

        // API metrics, settings, key CRUD
        async function fetchApiMetrics() {
            const response = await fetch('/api/metrics');
            const res = await response.json();
            
            // Toggles check
            document.getElementById('api-server-enabled').checked = res.config.server_enabled;
            document.getElementById('api-allow-external').checked = res.config.allow_external;
            document.getElementById('api-cors-enabled').checked = res.config.cors_enabled;

            // Counters
            document.getElementById('metric-total').textContent = res.metrics.total_requests;
            document.getElementById('metric-active').textContent = res.metrics.active_requests;
            document.getElementById('metric-speed').textContent = res.metrics.avg_duration.toFixed(0);

            // Keys list
            const keyBody = document.getElementById('keys-list-body');
            keyBody.innerHTML = '';
            Object.keys(res.config.keys).forEach(k => {
                const info = res.config.keys[k];
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${info.name}</td>
                    <td>${info.created}</td>
                    <td style="font-family: monospace;">${k}</td>
                    <td>
                        <button class="md-btn md-btn-outline" style="padding: 6px 12px; border-radius: 8px; border-color:var(--md-sys-color-error); color:var(--md-sys-color-error);" onclick="revokeKey('${k}')">
                            Revoke
                        </button>
                    </td>
                `;
                keyBody.appendChild(tr);
            });

            // Logs
            const logBox = document.getElementById('api-logs-box');
            logBox.innerHTML = '';
            res.logs.forEach(l => {
                const el = document.createElement('div');
                el.innerHTML = `<span class="terminal-in">[${l.timestamp}]</span> ${l.method} ${l.endpoint} - <span style="color: ${l.status === 200 ? 'var(--md-sys-color-success)' : 'var(--md-sys-color-error)'}">${l.status}</span> (${l.duration}ms)`;
                logBox.appendChild(el);
            });
        }

        async function updateApiConfig() {
            const server_enabled = document.getElementById('api-server-enabled').checked;
            const allow_external = document.getElementById('api-allow-external').checked;
            const cors_enabled = document.getElementById('api-cors-enabled').checked;

            await fetch('/api/config/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server_enabled, allow_external, cors_enabled })
            });
        }

        async function generateApiKey() {
            const name = document.getElementById('key-name-input').value.trim() || 'New Client';
            await fetch('/api/config/keys/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            document.getElementById('key-name-input').value = '';
            fetchApiMetrics();
        }

        async function revokeKey(key) {
            await fetch('/api/config/keys/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key })
            });
            fetchApiMetrics();
        }

        // Initialize status loops
        setInterval(async () => {
            if (document.getElementById('tab-api').classList.contains('active')) {
                fetchApiMetrics();
            }
        }, 3000);

        // First Load
        updateYoloSettings();
        fetchModelsList();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    # Pre-load Silero model in background thread
    threading.Thread(target=load_tts_model_fn, daemon=True).start()
    
    # Pre-load default YOLO
    threading.Thread(target=load_yolo_model_fn, args=("yolov8n.pt",), daemon=True).start()
    
    # Run server
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
