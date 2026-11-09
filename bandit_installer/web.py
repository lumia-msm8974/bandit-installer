"""Local web front end (Windows 10 Mobile look) served by the installer.

Listens on 127.0.0.1 only, on a random port, and every request must carry a
random token: the process runs as root/administrator and writes to the phone.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

from . import devices, steps, sysdeps
from .core import UI, Abort, Cancelled
from .i18n import L, get_lang, set_lang
from .phone import mode_text

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
VERSION = "0.3.0"


class WebUI(UI):
    def __init__(self, logfile):
        super().__init__(logfile)
        self.events = []
        self.cond = threading.Condition()
        self.prompt = None
        self._answers = {}
        self._next_prompt = 0
        self._last_progress = 0

    def push(self, ev):
        with self.cond:
            ev["id"] = len(self.events) + 1
            self.events.append(ev)
            if len(self.events) > 5000:  # keep ids stable, drop old payloads
                for e in self.events[:-5000]:
                    e.clear()
                    e["id"] = 0
            self.cond.notify_all()

    def since(self, n, timeout=20):
        with self.cond:
            if len(self.events) <= n:
                self.cond.wait(timeout)
            return [e for e in self.events[n:] if e.get("id")]

    def emit(self, kind, text):
        self.push({"type": "log", "kind": kind, "text": text})

    def emit_block(self, title, lines):
        self.push({"type": "block", "title": title, "lines": lines})

    def progress(self, label, done, total):
        now = time.time()
        if total and done < total and now - self._last_progress < 0.25:
            return
        self._last_progress = now
        self.push({"type": "progress", "label": label, "done": done, "total": total})

    def status(self, text):
        self.push({"type": "status", "text": text})

    def _ask(self, data):
        with self.cond:
            self._next_prompt += 1
            pid = self._next_prompt
            ev = threading.Event()
            self._answers[pid] = [ev, None]
            data["pid"] = pid
            self.prompt = data
        self.push({"type": "prompt", **data})
        while not ev.wait(0.2):
            if self.cancel_event.is_set():
                break
        with self.cond:
            self.prompt = None
            value = self._answers.pop(pid)[1]
        self.push({"type": "prompt-closed", "pid": pid})
        if self.cancel_event.is_set():
            raise Cancelled()
        return value

    def answer(self, pid, value):
        with self.cond:
            slot = self._answers.get(pid)
            if not slot:
                return False
            slot[1] = value
            slot[0].set()
            return True

    def confirm(self, title, body, yes, no=None, word=None, danger=False):
        return bool(self._ask({"kind": "confirm", "title": title, "body": body, "yes": yes,
                               "no": no, "word": word, "danger": danger}))

    def choose(self, title, body, options):
        opts = []
        for i, o in enumerate(options):
            o = dict(o)
            o["key"] = i
            opts.append(o)
        v = self._ask({"kind": "choose", "title": title, "body": body,
                       "options": [{k: v for k, v in o.items() if k != "value"} for o in opts]})
        if v is None:
            return None
        hit = [o for o in opts if o["key"] == v and not o.get("disabled")]
        return hit[0]["value"] if hit else None

    def form(self, title, body, fields, submit):
        maps, out = {}, []
        for f in fields:
            f = dict(f)
            if f.get("type") == "select":
                vals = [o["value"] for o in f["options"]]
                maps[f["key"]] = vals
                f["value"] = vals.index(f["value"]) if f.get("value") in vals else 0
                f["options"] = [{"key": i, "title": o["title"], "sub": o.get("sub", "")} for i, o in enumerate(f["options"])]
            out.append(f)
        raw = self._ask({"kind": "form", "title": title, "body": body, "submit": submit, "fields": out})
        if not isinstance(raw, dict):
            return None
        for k, vals in maps.items():
            try:
                raw[k] = vals[int(raw.get(k, 0))]
            except (ValueError, IndexError):
                raw[k] = vals[0]
        return raw


class App:
    def __init__(self, ctx_factory, system, phone, poller, state, kit, logfile, root):
        self.ctx_factory = ctx_factory
        self.system = system
        self.phone = phone
        self.poller = poller
        self.state = state
        self.kit = kit
        self.logfile = logfile
        self.root = root
        self.ui = WebUI(logfile)
        self.task = None
        self.task_name = None
        self.token = secrets.token_urlsafe(24)
        self.reqs = None
        self.quit = threading.Event()
        self.accent = None

    # tasks -------------------------------------------------------------
    def run_task(self, name):
        if self.task and self.task.is_alive():
            return False
        fn = steps.ACTIONS.get(name)
        if not fn:
            return False
        self.ui.cancel_event.clear()
        self.task_name = name
        self.poller.stop()

        def body():
            ctx = self.ctx_factory(self.ui)
            self.ui.push({"type": "task", "state": "running", "name": name})
            result, msg = "done", ""
            try:
                fn(ctx)
            except Cancelled:
                result, msg = "cancelled", L("Annullato.", "Cancelled.")
            except Abort as e:
                result, msg = "error", str(e)
            except Exception as e:  # show, log, keep the server alive
                result, msg = "error", f"{type(e).__name__}: {e}"
                self.ui.write_log(traceback.format_exc())
            if msg:
                (self.ui.warn if result == "cancelled" else self.ui.error)(msg)
            self.reqs = None
            self.ui.push({"type": "task", "state": result, "name": name, "message": msg})
            self.poller.start()

        self.task = threading.Thread(target=body, daemon=True)
        self.task.start()
        return True

    def running(self):
        return bool(self.task and self.task.is_alive())

    # home data -----------------------------------------------------------
    def state_json(self):
        if self.reqs is None and not self.running():
            ctx = self.ctx_factory(self.ui)
            self.reqs = [{"name": n, "ok": ok, "hint": h} for n, ok, h in steps.requirements(ctx)]
        snap = self.poller.snap
        done = {}
        for k, v in (self.state.phone().get("steps") or {}).items():
            done[k] = v.get("time", "")
        cur = devices.get(steps.current_device(self.state))
        return {
            "version": VERSION,
            "lang": get_lang(),
            "platform": self.system.name,
            "admin": self.system.is_admin(),
            "phone": {"mode": snap.mode, "text": mode_text(snap.mode), "detail": snap.detail,
                      "problem": snap.problem, "when": snap.when},
            "requirements": self.reqs or [],
            "kit": [{"name": n, "ok": ok} for n, ok in self.kit.for_device(cur.codename).status_lines()],
            "done": done,
            "modes": {k: steps.mode_ready(k, cur) for k in steps.MODES},
            "device": cur.codename,
            "devices": [{"codename": d.codename, "name": d.name, "models": list(d.models),
                         "note": L(*d.notes) if any(d.notes) else ""} for d in devices.DEVICES.values()],
            "accent": self.state.data.get("accent"),
            "running": self.task_name if self.running() else None,
            "prompt": self.ui.prompt,
            "last_event": len(self.ui.events),
            "work": self.ctx_factory(self.ui).work,
        }

    # server --------------------------------------------------------------
    def serve(self, port=0):
        app = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "lumia-installer"

            def log_message(self, *a):
                pass

            def _deny(self, code=403):
                self.send_response(code)
                self.end_headers()

            def _ok_host(self):
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{httpd.server_address[1]}", f"localhost:{httpd.server_address[1]}")

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if not self._ok_host():
                    return self._deny()
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if q.get("t", [""])[0] != app.token:
                    return self._deny()
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        b = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                elif u.path == "/api/state":
                    self._json(app.state_json())
                elif u.path == "/api/events":
                    n = int(q.get("since", ["0"])[0])
                    self._json({"events": app.ui.since(n), "running": app.running()})
                else:
                    self._deny(404)

            def do_POST(self):
                if not self._ok_host() or self.headers.get("X-Token") != app.token:
                    return self._deny()
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._deny(400)
                p = self.path
                if p == "/api/run":
                    self._json({"ok": app.run_task(data.get("action", ""))})
                elif p == "/api/answer":
                    self._json({"ok": app.ui.answer(int(data.get("pid", 0)), data.get("value"))})
                elif p == "/api/cancel":
                    app.ui.cancel_event.set()
                    self._json({"ok": True})
                elif p == "/api/lang":
                    set_lang(data.get("lang", "en"))
                    app.state.data["lang"] = get_lang()
                    app.state.save()
                    app.reqs = None
                    self._json({"ok": True})
                elif p == "/api/device":
                    c = str(data.get("device", ""))
                    if c in devices.DEVICES and not app.running():
                        app.state.data["device"] = c
                        app.state.save()
                        app.reqs = None
                        self._json({"ok": True})
                    else:
                        self._json({"ok": False})
                elif p == "/api/accent":
                    a = str(data.get("accent", ""))
                    if re.fullmatch(r"#[0-9a-fA-F]{6}", a):
                        app.state.data["accent"] = a
                        app.state.save()
                    self._json({"ok": True})
                elif p == "/api/quit":
                    self._json({"ok": not app.running()})
                    if not app.running():
                        app.quit.set()
                else:
                    self._deny(404)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    return  # the window was closed during a request
                super().handle_error(request, client_address)

        httpd = Server(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpd = httpd
        return f"http://127.0.0.1:{httpd.server_address[1]}/?t={self.token}"


# ---------------------------------------------------------------------------
# opening an app window
# ---------------------------------------------------------------------------

APP_SIZE = "--window-size=1280,800"


def open_window(url):
    """Chromium-style --app window when possible, else the default browser."""
    if sysdeps.IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                exe = os.path.join(base, rel)
                if os.path.isfile(exe):
                    # Chromium drops administrator rights by itself when started elevated
                    subprocess.Popen([exe, f"--app={url}", APP_SIZE])
                    return True
        os.startfile(url)  # noqa
        return True
    prefix = []
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user:
        uid = os.environ.get("SUDO_UID", "")
        env = [f"{k}={os.environ[k]}" for k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")
               if k in os.environ]
        env.append(f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR') or '/run/user/' + uid}")
        prefix = ["sudo", "-u", user, "env"] + env
    elif os.geteuid() == 0:
        return False  # never start a browser as root
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "microsoft-edge-stable",
                 "brave", "brave-browser", "vivaldi-stable"):
        exe = shutil.which(name)
        if exe:
            subprocess.Popen(prefix + [exe, f"--app={url}", APP_SIZE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    exe = shutil.which("xdg-open")
    if exe:
        subprocess.Popen(prefix + [exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def main_loop(app, url, open_browser=True):
    print(L("Installer avviato. Se la finestra non si apre, apri questo indirizzo:",
            "Installer running. If no window opens, open this address:"))
    print("  " + url)
    print(L("Ctrl+C qui per uscire.", "Ctrl+C here to quit."))
    sys.stdout.flush()
    if open_browser and not open_window(url):
        print(L("(nessun browser trovato da aprire)", "(no browser found to open)"))
    try:
        while not app.quit.wait(0.5):
            pass
    except KeyboardInterrupt:
        if app.running():
            app.ui.cancel_event.set()
            print(L("\nAnnullo l'operazione in corso…", "\nCancelling the running task…"))
            app.task.join(30)
    print(L("Uscita.", "Bye."))
