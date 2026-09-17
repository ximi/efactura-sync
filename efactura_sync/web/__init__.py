"""Local web UI over the engine. Three pages (decision 2026-09-17): Acasă, Facturi,
Setări, plus a first-run wizard at /start. Served on 127.0.0.1 only and opened in
the default browser.

Safety model for a localhost server (risk 4 in TODO.md): a per-run CSRF token on
every POST, a foreign-Origin refusal, a Host allow-list against DNS rebinding, the
client secret is never rendered back, and files are served by database id — never
by a client-supplied path.
"""

import dataclasses
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from .. import __version__, core
from .errors import describe, render_event, technical_detail
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
            self.events.append({"type": "SyncError", "download_id": "", "message": str(exc),
                                "code": core.error_code(exc)})
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


def _safe_next(value: str | None, fallback: str) -> str:
    """Only same-app paths may be redirect targets."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


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

    def t():
        return translator(lang())

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
            return None, redirect(url_for("start"))

    def fmt_ts(ts) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    def form_ctx(errors=None, next_url=None, values=None) -> dict:
        raw = core.read_config_raw()
        if values:
            raw = {**raw, **values}
        return {"raw": raw, "errors": errors or {}, "next_url": next_url,
                "environments": list(core.REST_BASE),
                "secret_set": bool(core.read_config_raw().get("client_secret"))}

    def auth_ctx(auth_exc: core.ConfigError | None = None, wizard=False) -> dict:
        tok = core.token_status()
        return {"auth": tok, "auth_expires": fmt_ts(tok["expires_at"]),
                "pending": app.pending_auth, "wizard": wizard,
                "auth_error": describe(auth_exc.code, t()) if auth_exc else None,
                "auth_detail": technical_detail(str(auth_exc)) if auth_exc else None}

    def render_settings(status=200, auth_exc=None, errors=None, values=None):
        return render_template("settings.html", **form_ctx(errors, None, values),
                               **auth_ctx(auth_exc)), status

    def render_wizard(step: int, status=200, auth_exc=None, errors=None, values=None):
        ctx = {"step": step}
        if step == 2:
            ctx.update(form_ctx(errors, url_for("start", step=3), values))
        if step == 3:
            ctx.update(auth_ctx(auth_exc, wizard=True))
        return render_template("start.html", **ctx), status

    # ---- pages ----------------------------------------------------------- #

    @app.get("/ping")
    def ping():
        # Lets a second launch recognise a running instance (see run_ui).
        return {"app": "efactura-sync", "version": __version__}

    @app.get("/")
    def home():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        report = core.status_report(cfg)
        tr = t()
        lines = [render_event(e, tr) for e in app.runner.events]
        return render_template("home.html", report=report, lines=lines)

    @app.get("/start")
    def start():
        step = request.args.get("step", 1, type=int)
        if step == 3:
            try:
                core.load_config()
            except core.ConfigError:
                return redirect(url_for("start", step=2))
        return render_wizard(step if step in (1, 2, 3) else 1)

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
            return t()("sync_busy"), 409
        return redirect(url_for("home"), code=303)

    @app.get("/sync/events")
    def sync_events():
        since = request.args.get("since", 0, type=int)
        tr = t()

        def generate():
            for ev in app.runner.stream(since):
                payload = {**ev, "text": render_event(ev, tr)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
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
        tr = t()
        form = request.form
        next_url = _safe_next(form.get("next"), url_for("settings"))
        values = {
            "client_id": form.get("client_id", "").strip(),
            "cif": core.normalize_cif(form.get("cif", "")),
            "environment": form.get("environment", "").strip(),
            "redirect_uri": form.get("redirect_uri", "").strip(),
            "base_dir": form.get("base_dir", "").strip(),
        }
        errors = {}
        if not values["client_id"]:
            errors["client_id"] = tr("val_required")
        if not values["cif"]:
            errors["cif"] = tr("val_required")
        elif not values["cif"].isdigit():
            errors["cif"] = tr("val_cif_digits")
        if values["environment"] not in core.REST_BASE:
            errors["environment"] = tr("val_environment")
        if errors:
            if next_url.startswith("/start"):
                return render_wizard(2, 400, errors=errors, values=values)
            return render_settings(400, errors=errors, values=values)

        raw = core.read_config_raw()
        raw.update(values)
        secret = form.get("client_secret", "")
        if secret:                                   # blank keeps the stored secret
            raw["client_secret"] = secret
        core.save_config(raw)
        flash(tr("settings_saved"), "ok")
        return redirect(next_url, code=303)

    @app.post("/limba")
    def set_language():
        value = request.form.get("lang", "")
        if value not in LANGUAGES:
            abort(400)
        raw = core.read_config_raw()
        raw["language"] = value
        core.save_config(raw)
        return redirect(_safe_next(request.form.get("next"), url_for("home")), code=303)

    @app.post("/auth/begin")
    def auth_begin():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        app.pending_auth = core.begin_auth(cfg)
        if request.form.get("wizard"):
            return render_wizard(3)
        return render_settings()

    @app.post("/auth/complete")
    def auth_complete():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        if app.pending_auth is None:
            abort(400)
        wizard = bool(request.form.get("wizard"))
        try:
            core.complete_auth(cfg, app.pending_auth, request.form.get("pasted", ""))
        except core.ConfigError as exc:
            if wizard:
                return render_wizard(3, auth_exc=exc)
            return render_settings(auth_exc=exc)
        app.pending_auth = None
        flash(t()("wiz_done" if wizard else "auth_ok"), "ok")
        return redirect(url_for("home") if wizard else url_for("settings"), code=303)

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


def _already_running(port: int) -> bool:
    """True if *our* app answers on the port (not just anything listening)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=0.5) as resp:
            return json.load(resp).get("app") == "efactura-sync"
    except Exception:  # noqa: BLE001 — closed port, other app, timeout: all "no"
        return False


def _setup_frozen_logging() -> None:
    """A windowed bundle has no console: send logs to a file, never crash on print."""
    if not getattr(sys, "frozen", False):
        return
    core.ensure_config_dir()
    logging.basicConfig(filename=str(core.CONFIG_DIR / "ui.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def run_ui(cfg: dict | None, port: int = DEFAULT_PORT, open_browser: bool = True,
           idle_minutes: int = IDLE_MINUTES) -> None:
    """Serve the UI on 127.0.0.1, open the browser, exit on Quit or after idling.

    A second launch (double-clicking the app again) reuses the running instance:
    it just opens the browser to it instead of starting another server.
    """
    _setup_frozen_logging()
    if _already_running(port):
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return

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
