#!/usr/bin/env python3
import os
import json
import threading
import tempfile
import logging
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# ---------- CONFIG ----------
VERSION = "v0.78"
API_KEY = os.environ.get("JTAICB_API_KEY")
MEMORY_FILE = os.environ.get("JTAICB_MEMORY_FILE", "./data/memory.json")
GEMINI_URL = ("https://generativelanguage.googleapis.com/"
              "v1beta/models/gemini-2.0-flash:generateContent")

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

memory_lock = threading.Lock()

# ---------------------------------------
# MEMORY MANAGEMENT
# ---------------------------------------
def _ensure_memory_dir():
    directory = os.path.dirname(MEMORY_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)

def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        return {}
    except Exception:
        return {}

def _write_memory_file(data):
    try:
        _ensure_memory_dir()
        d = os.path.dirname(MEMORY_FILE) or "."
        with tempfile.NamedTemporaryFile("w", dir=d,
                                         delete=False,
                                         encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            temp = tf.name
        os.replace(temp, MEMORY_FILE)
        return True
    except Exception as e:
        logger.error("Saving memory failed: %s", e)
        return False

memory = load_memory()

# ---------------------------------------
# GEMINI REQUEST USING urllib
# ---------------------------------------
def call_gemini(payload):
    data_bytes = json.dumps(payload).encode("utf-8")
    full_url = GEMINI_URL + ("?key=" + API_KEY if API_KEY and not API_KEY.lower().startswith("bearer ") else "")

    headers = {"Content-Type": "application/json"}
    if API_KEY and API_KEY.lower().startswith("bearer "):
        headers["Authorization"] = API_KEY

    req = urlrequest.Request(full_url, data=data_bytes, headers=headers)

    try:
        with urlrequest.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return {"error": f"HTTP error {e.code}"}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------
# EXTRACT TEXT
# ---------------------------------------
def extract_text(data):
    try:
        cand = data.get("candidates")
        if cand:
            parts = cand[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()
    except:
        pass
    return data.get("text") or data.get("message") or "No reply"

# ---------------------------------------
# REQUEST HANDLER
# ---------------------------------------
class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except:
            return {}

    # ----------------------------
    # GET /api
    # ----------------------------
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api":
            qs = parse_qs(parsed.query)
            if "version" in qs:
                return self._send(200, VERSION)

            device = qs.get("device", ["unknown"])[0]
            clear = qs.get("clear", ["false"])[0].lower() == "true"
            view = qs.get("view", [None])[0]
            inp = qs.get("input", [""])[0].strip()

            if clear:
                with memory_lock:
                    memory[device] = []
                    _write_memory_file(memory)
                return self._send(200, "Memory cleared!")

            if view == "history":
                hist = json.dumps(memory.get(device, []), indent=2)
                return self._send(200, hist, "application/json")

            if not inp:
                return self._send(400, "No input")

            # Add user message
            with memory_lock:
                memory.setdefault(device, []).append({"sender": "You", "text": inp})
                memory[device] = memory[device][-200:]
                _write_memory_file(memory)

            # Call Gemini
            history = memory.get(device, [])
            contents = [{"role": ("user" if m["sender"] == "You" else "assistant"),
                         "parts": [{"text": m["text"]}]} for m in history[-60:]]

            payload = {
                "contents": contents,
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 16384}
            }

            data = call_gemini(payload)
            reply = extract_text(data)

            with memory_lock:
                memory[device].append({"sender": "AI", "text": reply})
                _write_memory_file(memory)

            return self._send(200, reply)

        # Serve index.html or static file
        if parsed.path == "/" or parsed.path == "":
            try:
                with open("index.html", "rb") as f:
                    return self._send(200, f.read(), "text/html")
            except FileNotFoundError:
                return self._send(404, "Missing index.html")

        # static
        path = parsed.path.lstrip("/")
        if os.path.exists(path) and os.path.isfile(path):
            with open(path, "rb") as f:
                return self._send(200, f.read())
        return self._send(404, "Not found")

    # ----------------------------
    # POST /api
    # ----------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api":
            return self._send(404, "Not found")

        data = self._read_json()
        device = data.get("device") or "unknown"

        if isinstance(data.get("memory"), list):
            with memory_lock:
                memory[device] = data["memory"]
                _write_memory_file(memory)
            return self._send(200, "Memory imported!")

        inp = (data.get("input") or "").strip()
        if not inp:
            return self._send(400, "No input")

        # Add to memory
        with memory_lock:
            memory.setdefault(device, []).append({"sender": "You", "text": inp})
            memory[device] = memory[device][-200:]
            _write_memory_file(memory)

        # Send to model
        history = memory.get(device, [])
        contents = [{"role": ("user" if m["sender"] == "You" else "assistant"),
                     "parts": [{"text": m["text"]}]} for m in history[-60:]]

        payload = {
            "contents": contents,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 16384}
        }

        data = call_gemini(payload)
        reply = extract_text(data)

        with memory_lock:
            memory[device].append({"sender": "AI", "text": reply})
            _write_memory_file(memory)

        return self._send(200, reply)

# ---------------------------------------
# MAIN
# ---------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Server running at http://0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
