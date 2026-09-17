"""Local web UI over the engine. Three pages (decision 2026-09-17): Acasă, Facturi,
Setări. Served on 127.0.0.1 only and opened in the default browser.

Safety model for a localhost server (risk 4 in TODO.md): a per-run CSRF token on
every POST, a foreign-Origin refusal, a Host allow-list against DNS rebinding, the
client secret is never rendered back, and files are served by database id — never
by a client-supplied path.
"""

import dataclasses
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from .. import __version__, core
from .strings import LANGUAGES, translator

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEFAULT_PORT = 8765
IDLE_MINUTES = 30


# --------------------------------------------------------------------------- #
# Background sync runner
# --------------------------------------------------------------------------- #

def _serialize(ev) -> dict:
    return {"type": type(ev).__name__, **dataclasses.asdict(ev)}


class SyncRunner:
    """Runs one core.sync() at a time on a thread and buffers its events for SSE."""

    def __init__(self, engine=core.sync):
        self.engine = engine
        self.events: list[dict] = []
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self.running:
                return False
            self.events = []
            self._done.clear()
            self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self._thread.start()
            return True

    def _run(self, cfg: dict) -> None:
        try:
            for ev in self.engine(cfg):
                self.events.append(_serialize(ev))
        except Exception as exc:  # noqa: BLE001 — surface as an event, never a 500
            self.events.append({"type": "SyncError", "download_id": "", "message": str(exc)})
        finally:
            self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def stream(self, since: int = 0):
        i = since
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self._done.is_set() and i >= len(self.events):
                return
            time.sleep(0.2)


# --------------------------------------------------------------------------- #
# OS integration (injectable so tests never touch the desktop)
# --------------------------------------------------------------------------- #

def open_path_default(path) -> None:
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os_startfile = getattr(__import__("os"), "startfile", None)
        if os_startfile:
            os_startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(open_path=None, shutdown=None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = secrets.token_urlsafe(32)          # flash() only; local, per run
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    app.runner = SyncRunner()
    app.pending_auth = None
    app.open_path = open_path or open_path_default
    app.shutdown_hook = shutdown or (lambda: None)
    app.last_activity = time.time()

    # ---- guards ---------------------------------------------------------- #

    @app.before_request
    def guard():
        app.last_activity = time.time()
        if request.host.split(":")[0] not in LOCAL_HOSTS:
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
                abort(403)
            if not secrets.compare_digest(request.form.get("csrf", ""),
                                          app.config["CSRF_TOKEN"]):
                abort(403)

    # ---- helpers --------------------------------------------------------- #

    def lang() -> str:
        value = core.read_config_raw().get("language", "ro")
        return value if value in LANGUAGES else "ro"

    @app.context_processor
    def inject():
        current = lang()
        return {"t": translator(current), "lang": current,
                "csrf": app.config["CSRF_TOKEN"], "version": __version__,
                "running": app.runner.running}

    def load_cfg_or_redirect():
        try:
            return core.load_config(), None
        except core.ConfigError:
            flash(translator(lang())("config_needed"), "info")
            return None, redirect(url_for("settings"))

    def fmt_ts(ts) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    def render_settings(status=200, auth_error=None):
        raw = core.read_config_raw()
        tok = core.token_status()
        return render_template(
            "settings.html", raw=raw, environments=list(core.REST_BASE),
            secret_set=bool(raw.get("client_secret")), auth=tok,
            auth_expires=fmt_ts(tok["expires_at"]), pending=app.pending_auth,
            auth_error=auth_error, languages=LANGUAGES,
        ), status

    # ---- pages ----------------------------------------------------------- #

    @app.get("/")
    def home():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        report = core.status_report(cfg)
        return render_template("home.html", report=report, events=app.runner.events)

    @app.get("/facturi")
    def invoices():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        month = request.args.get("luna", "").strip()
        supplier = request.args.get("furnizor", "").strip()
        rows = core.list_invoices(month or None, supplier or None)
        return render_template("invoices.html", rows=rows, months=core.invoice_months(),
                               month=month, supplier=supplier,
                               base_dir=str(Path(cfg["base_dir"]).expanduser()))

    @app.get("/pdf/<download_id>")
    def pdf(download_id):
        row = core.invoice_by_download_id(download_id)
        if not row or not row.get("pdf_path") or not Path(row["pdf_path"]).is_file():
            abort(404)
        return send_file(row["pdf_path"], mimetype="application/pdf")

    @app.get("/setari")
    def settings():
        return render_settings()

    # ---- actions (all POST, CSRF-guarded) -------------------------------- #

    @app.post("/sync")
    def start_sync():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        if not app.runner.start(cfg):
            return translator(lang())("sync_busy"), 409
        return redirect(url_for("home"), code=303)

    @app.get("/sync/events")
    def sync_events():
        since = request.args.get("since", 0, type=int)

        def generate():
            import json
            for ev in app.runner.stream(since):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/deschide-dosar")
    def open_folder():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        app.open_path(Path(cfg["base_dir"]).expanduser())
        return redirect(url_for("invoices"), code=303)

    @app.post("/setari")
    def save_settings():
        raw = core.read_config_raw()
        form = request.form
        environment = form.get("environment", "").strip()
        if environment not in core.REST_BASE:
            return translator(lang())("error"), 400
        raw.update({
            "client_id": form.get("client_id", "").strip(),
            "cif": core.normalize_cif(form.get("cif", "")),
            "environment": environment,
            "redirect_uri": form.get("redirect_uri", "").strip(),
            "base_dir": form.get("base_dir", "").strip(),
        })
        secret = form.get("client_secret", "")
        if secret:                                   # blank keeps the stored secret
            raw["client_secret"] = secret
        core.save_config(raw)
        flash(translator(lang())("settings_saved"), "ok")
        return redirect(url_for("settings"), code=303)

    @app.post("/limba")
    def set_language():
        value = request.form.get("lang", "")
        if value not in LANGUAGES:
            abort(400)
        raw = core.read_config_raw()
        raw["language"] = value
        core.save_config(raw)
        return redirect(request.form.get("next") or url_for("home"), code=303)

    @app.post("/auth/begin")
    def auth_begin():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        app.pending_auth = core.begin_auth(cfg)
        return render_settings()

    @app.post("/auth/complete")
    def auth_complete():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        if app.pending_auth is None:
            abort(400)
        try:
            core.complete_auth(cfg, app.pending_auth, request.form.get("pasted", ""))
        except core.ConfigError as exc:
            return render_settings(auth_error=str(exc))
        app.pending_auth = None
        flash(translator(lang())("auth_ok"), "ok")
        return redirect(url_for("settings"), code=303)

    @app.post("/iesire")
    def quit_app():
        app.shutdown_hook()
        return render_template("bye.html")

    return app


# --------------------------------------------------------------------------- #
# Running it
# --------------------------------------------------------------------------- #

def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free local port found.")


def run_ui(cfg: dict | None, port: int = DEFAULT_PORT, open_browser: bool = True,
           idle_minutes: int = IDLE_MINUTES) -> None:
    """Serve the UI on 127.0.0.1, open the browser, exit on Quit or after idling."""
    from werkzeug.serving import make_server

    port = _free_port(port)
    app = create_app()
    server = make_server("127.0.0.1", port, app, threaded=True)
    app.shutdown_hook = lambda: threading.Thread(target=server.shutdown, daemon=True).start()

    def watchdog():
        while True:
            time.sleep(30)
            idle = time.time() - app.last_activity
            if idle > idle_minutes * 60 and not app.runner.running:
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    server.serve_forever()
