from flask import Flask, send_from_directory, request, jsonify
import requests
import os
import json
import tempfile
import threading
import logging
import html as html_escape

app = Flask(__name__, static_folder='.')

# ---------- CONFIG ----------
VERSION = "v0.78" # Change to whatever you like
API_KEY = os.environ.get("JTAICB_API_KEY")  # Set this in Render environment
MEMORY_FILE = os.environ.get("JTAICB_MEMORY_FILE", "./data/memory.json")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- THREADING / LOCKS ----------
memory_lock = threading.Lock()

def _ensure_memory_dir():
    directory = os.path.dirname(MEMORY_FILE)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception as e:
            logger.error("Failed to create memory directory '%s': %s", directory, e)

# ---------- MEMORY MANAGEMENT ----------
def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        return {}
    except Exception as e:
        logger.warning("Failed to load memory.json, resetting. Error: %s", e)
        return {}

def _write_memory_file(data):
    """Write memory JSON to disk safely."""
    try:
        _ensure_memory_dir()
        dirpath = os.path.dirname(MEMORY_FILE) or "."
        with tempfile.NamedTemporaryFile("w", dir=dirpath, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, ensure_ascii=False, indent=2)
            tempname = tf.name
        os.replace(tempname, MEMORY_FILE)
        logger.debug("Memory saved to %s", MEMORY_FILE)
        return True
    except Exception as e:
        logger.error("Error saving memory: %s", e)
        return False

def save_memory():
    """Acquire the memory lock, then write memory to disk."""
    with memory_lock:
        return _write_memory_file(app.memory)

# Load memory once at startup
app.memory = load_memory()
logger.info("Loaded memory for %d devices (from %s)", len(app.memory), MEMORY_FILE)

# ---------- HELPER FUNCTIONS ----------
def _get_device_id(req):
    # Priority: query string -> JSON body -> header -> fallback 'unknown'
    device = req.args.get('device')
    if not device:
        try:
            data = req.get_json(silent=True) or {}
            device = data.get('device')
        except Exception:
            device = None
    if not device:
        device = req.headers.get('X-Device-Id')
    return device or 'unknown'

def _extract_text_from_gemini_response(data):
    """Try several response shapes to extract generated text."""
    try:
        candidates = data.get("candidates")
        if candidates and isinstance(candidates, list):
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()

        output = data.get("output")
        if output:
            if isinstance(output, list):
                texts = []
                for item in output:
                    if isinstance(item, dict):
                        texts.append(item.get("content", {}).get("text", "") or item.get("text", "") or "")
                return "\n".join(t for t in texts if t).strip()
            elif isinstance(output, dict):
                return output.get("content", {}).get("text", "") or output.get("text", "") or ""

        if "responses" in data and isinstance(data["responses"], list):
            return " ".join(r.get("text", "") for r in data["responses"]).strip()

        return ""
    except Exception as e:
        logger.exception("Failed to extract text from Gemini response: %s", e)
        return ""

# ---------- ROUTES ----------
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('.', path)

@app.route('/api', methods=['GET', 'POST'])
def api():
    if request.args.get("version"):
        return VERSION, 200

    device_id = _get_device_id(request)
    clear = request.args.get('clear', 'false').lower() == 'true'
    view = request.args.get('view', None)

    # Handle memory import via POST JSON
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}

        if "memory" in data and isinstance(data["memory"], list):
            with memory_lock:
                app.memory[device_id] = data["memory"]
                _write_memory_file(app.memory)
            return "Memory imported successfully!", 200

        user_input = (data.get("input") or "").strip()
    else:
        user_input = (request.args.get("input") or "").strip()

    # Clear memory
    if clear:
        with memory_lock:
            app.memory[device_id] = []
            _write_memory_file(app.memory)
        return "Memory cleared!", 200

    # View history
    if view == "history":
        return jsonify(app.memory.get(device_id, []))

    # Handle no input
    if not user_input:
        return "No input", 400

    # Add user message
    with memory_lock:
        app.memory.setdefault(device_id, []).append({"sender": "You", "text": user_input})
        if len(app.memory[device_id]) > 200:
            app.memory[device_id] = app.memory[device_id][-200:]
        _write_memory_file(app.memory)

    # ---------- GEMINI API ----------
    reply = "No reply"
    if not API_KEY:
        reply = "Error: Missing Gemini API key."
    else:
        with memory_lock:
            history = app.memory.get(device_id, []).copy()

        MAX_HISTORY_ENTRIES = 60
        recent = history[-MAX_HISTORY_ENTRIES:]

        contents = []
        for entry in recent:
            sender = entry.get("sender", "")
            role = "user" if sender == "You" else "assistant"
            text = entry.get("text", "") or ""
            if not text:
                continue
            contents.append({"role": role, "parts": [{"text": text}]})

        if not contents or contents[-1].get("role") != "user":
            contents.append({"role": "user", "parts": [{"text": user_input}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 16384
            }
        }

        headers = {"Content-Type": "application/json"}
        params = {}

        if API_KEY.lower().startswith("bearer "):
            headers["Authorization"] = API_KEY
        else:
            params["key"] = API_KEY

        try:
            resp = requests.post(
                GEMINI_URL,
                headers=headers,
                params=params,
                json=payload,
                timeout=25
            )

            try:
                resp.raise_for_status()
            except requests.HTTPError:
                logger.error("Gemini API returned status %s: %s", resp.status_code, resp.text)
                reply = f"Error contacting Gemini: status {resp.status_code}"
            else:
                try:
                    data = resp.json()
                except Exception as e:
                    logger.exception("Failed to parse Gemini JSON response: %s", e)
                    reply = f"Error parsing Gemini response: {e}"
                else:
                    extracted = _extract_text_from_gemini_response(data)
                    if extracted:
                        reply = extracted
                    else:
                        reply = data.get("text") or data.get("message") or "No reply"
        except requests.RequestException as e:
            logger.exception("Error during Gemini request: %s", e)
            reply = f"Error contacting Gemini: {e}"

    # Save AI reply
    with memory_lock:
        app.memory.setdefault(device_id, []).append({"sender": "AI", "text": reply})
        _write_memory_file(app.memory)

    return reply, 200

@app.route("/history")
def history_page():
    try:
        if not os.path.exists(MEMORY_FILE):
            return "<p style='color:white'>No memory file found.</p>"

        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            all_data = json.load(f)

        html_output = """
        <html><head><title>Chat History</title></head>
        <body style='background:#111;color:white;font-family:monospace;padding:10px;'>
        <h2>Chat History</h2><hr>
        """

        for device, entries in all_data.items():
            html_output += f"<h3 style='color:#9cf'>Device: {html_escape.escape(str(device))}</h3><hr>"
            for entry in entries:
                sender = html_escape.escape(entry.get("sender", "Unknown"))
                text = html_escape.escape(entry.get("text", "")).replace("\n", "<br>")
                html_output += f"<p><b style='color:#6cf'>{sender}:</b> {text}</p>"
            html_output += "<hr>"

        html_output += "</body></html>"
        return html_output
    except Exception as e:
        logger.exception("Error loading history: %s", e)
        return f"<p style='color:red'>Error loading history: {e}</p>"

# ---------- MAIN ----------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    logger.info("Server running on http://0.0.0.0:%d", port)
    app.run(host='0.0.0.0', port=port)
