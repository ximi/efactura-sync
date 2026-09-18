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
import re
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
from urllib.parse import urlparse, urlsplit

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.exceptions import HTTPException

from .. import __version__, core
from .errors import (describe, describe_more, fmt_date, fmt_datetime, fmt_month,
                     render_event, technical_detail)
from .strings import LANGUAGES, translator

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 8765
PORT_RANGE = 20                     # 8765..8784: where we may listen and where we look
IDLE_MINUTES = 30
FINISH_GRACE_SECONDS = 300          # keep serving after a run so the summary can render

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Background sync runner
# --------------------------------------------------------------------------- #

def _serialize(ev) -> dict:
    return {"type": type(ev).__name__, **dataclasses.asdict(ev)}


class _Run:
    """One sync's event buffer. A stream binds to a run, so a stream that outlives
    the run never reads into the next one (review fix 2026-09-17)."""

    def __init__(self):
        self.events: list[dict] = []
        self.done = threading.Event()


class SyncRunner:
    """Runs one core.sync() at a time on a thread and buffers its events for SSE."""

    def __init__(self, engine=core.sync):
        self.engine = engine
        self._run = _Run()
        self._run.done.set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.finished_at = 0.0

    @property
    def events(self) -> list[dict]:
        return self._run.events

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self.running:
                return False
            run = _Run()
            self._run = run
            self._thread = threading.Thread(target=self._drive, args=(cfg, run), daemon=True)
            self._thread.start()
            return True

    def _drive(self, cfg: dict, run: _Run) -> None:
        try:
            for ev in self.engine(cfg):
                run.events.append(_serialize(ev))
        except Exception as exc:  # noqa: BLE001 — surface as an event, never a 500
            run.events.append({"type": "SyncError", "download_id": "", "message": str(exc),
                               "code": core.error_code(exc)})
        finally:
            self.finished_at = time.time()
            run.done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._run.done.wait(timeout)

    def stream(self, since: int = 0, touch=None):
        run = self._run
        i = max(0, since)
        while True:
            while i < len(run.events):
                yield run.events[i]
                i += 1
            if touch:
                touch()                     # a live stream is activity for the watchdog
            if run.done.is_set() and i >= len(run.events):
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


def _applescript_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def pick_folder_default(initial) -> str | None:
    """Native folder dialog, opened by the local app on behalf of the browser page
    (a web page cannot read folder paths itself). None when cancelled/unavailable."""
    initial = str(initial or Path.home())
    try:
        if sys.platform == "darwin":
            default = (f' default location POSIX file "{_applescript_str(initial)}"'
                       if Path(initial).is_dir() else "")
            # No "System Events" wrapper: that costs an Automation-permission prompt
            # on an unsigned app; a bare `choose folder` needs none.
            script = ('POSIX path of (choose folder with prompt '
                      f'"Alege dosarul pentru facturi"{default})')
            out = subprocess.run(["osascript", "-e", script], capture_output=True,
                                 text=True, timeout=600)
            return out.stdout.strip().rstrip("/") or None if out.returncode == 0 else None
        if sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  f"$d.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}'; "
                  "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                                 capture_output=True, text=True, timeout=600,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return out.stdout.strip() or None
        out = subprocess.run(["zenity", "--file-selection", "--directory",
                              f"--filename={initial}/"], capture_output=True, text=True,
                             timeout=600)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — no dialog available: the text field still works
        return None


# One leading slash, then only path/query characters: no "//host", no "/\\host"
# (browsers read a backslash as an authority start), no CR/LF header tricks.
_NEXT_RE = re.compile(r"/(?!/)[A-Za-z0-9_\-./?=&%]*")


# Anything that looks like a token or key (long unbroken alnum/base64 runs) is
# masked before a log line can leave the machine in a support request.
_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def _redact(line: str) -> str:
    return _SECRET_RE.sub("[redacted]", line)


def _safe_next(value: str | None, fallback: str) -> str:
    """Only same-app paths may be redirect targets."""
    return value if value and _NEXT_RE.fullmatch(value) else fallback


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(open_path=None, shutdown=None, pick_folder=None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = secrets.token_urlsafe(32)          # flash() only; local, per run
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    app.runner = SyncRunner()
    app.pending_auth = None
    app.last_auth_code = None          # opens "advanced" when the callback URL is at fault
    app.jinja_env.filters["fmt_date"] = fmt_date
    app.jinja_env.filters["fmt_datetime"] = fmt_datetime
    app.open_path = open_path or open_path_default
    app.pick_folder = pick_folder or pick_folder_default
    app.shutdown_hook = shutdown or (lambda: None)
    app.last_activity = time.time()

    # ---- guards ---------------------------------------------------------- #

    @app.before_request
    def guard():
        # Activity for the idle watchdog: only requests a user (or same-origin page)
        # makes. A foreign page can fire no-cors GETs forever; they carry
        # Sec-Fetch-Site: cross-site and must not keep the server alive.
        fetch_site = request.headers.get("Sec-Fetch-Site", "same-origin")
        if request.path != "/ping" and fetch_site in ("same-origin", "none"):
            app.last_activity = time.time()
        def refuse(reason: str):
            # Why a request was refused is the one thing support needs; nothing
            # here is secret (no token values, no form data).
            log.warning("refused %s %s: %s", request.method, request.path, reason)
            abort(403)

        if (urlsplit("//" + request.host).hostname or "") not in LOCAL_HOSTS:
            refuse(f"host {request.host!r} not local")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
                refuse(f"origin {origin!r} not local")
            # Compare bytes: compare_digest rejects non-ASCII str with a TypeError (500).
            if not secrets.compare_digest(request.form.get("csrf", "").encode("utf-8"),
                                          app.config["CSRF_TOKEN"].encode("utf-8")):
                refuse("csrf token missing or stale" if request.form.get("csrf")
                       else f"no csrf field (content-type {request.content_type!r})")

    @app.after_request
    def harden(resp):
        # Clickjacking: a hostile page could frame 127.0.0.1 and place one of our
        # CSRF-valid buttons under a decoy; refuse to be framed at all.
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin, NOT no-referrer: with no-referrer Chromium sends `Origin: null`
        # on same-origin form POSTs, which the Origin guard rightly refuses — every
        # button 403'd. Found live 2026-09-18; the test client cannot see this.
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    # ---- helpers --------------------------------------------------------- #

    def lang() -> str:
        value = core.read_config_raw().get("language", "ro")
        return value if value in LANGUAGES else "ro"

    def t():
        return translator(lang())

    @app.context_processor
    def inject():
        current = lang()
        tr = translator(current)
        return {"t": tr, "lang": current, "fmt_month": lambda v: fmt_month(v, tr),
                "csrf": app.config["CSRF_TOKEN"], "version": __version__,
                "running": app.runner.running,
                "firms": raw_firms(), "current_firm": core.selected_firm(core.read_config_raw())}

    def raw_firms():
        return core.read_config_raw().get("firms", [])

    # ---- error pages: never Werkzeug's English boilerplate ------------------ #

    ERROR_KEYS = {400: "err_bad_request", 403: "err_page_expired", 404: "err_not_found",
                  500: "err_server"}

    @app.errorhandler(HTTPException)
    def http_error(exc):
        tr = t()
        desc = exc.description if isinstance(exc.description, str) else ""
        key = desc if tr.has(desc) else ERROR_KEYS.get(exc.code, "err_server")
        return render_template("error.html", message=tr(key), code=exc.code), exc.code

    @app.errorhandler(Exception)
    def any_error(exc):
        log.exception("unhandled error")
        return render_template("error.html", message=t()("err_server"), code=500), 500

    @app.get("/diagnostic")
    def diagnostic():
        """Plain text a user can paste into a support request. Never a secret, never
        the CUI: versions, platform, paths, auth state, last run, last log lines."""
        tr = t()
        raw = core.read_config_raw()
        tok = core.token_status()
        lines = [
            f"eFactura Sync {__version__}",
            f"python: {sys.version.split()[0]}  platform: {sys.platform}  frozen: {bool(getattr(sys, 'frozen', False))}",
            f"config dir: {core.CONFIG_DIR}",
            f"environment: {raw.get('environment', core.DEFAULT_ENVIRONMENT)}  language: {raw.get('language', 'ro')}",
            f"authenticated: {'yes' if tok['authenticated'] else 'no'}"
            + (f"  expires: {fmt_ts(tok['expires_at'])}" if tok.get("expires_at") else ""),
        ]
        try:
            report = core.status_report(core.load_config())
            lines.append(f"last sync: {report.last_run or '-'}  invoices: {report.total}  "
                         f"without pdf: {report.pdf_failed}")
        except core.ConfigError as exc:
            lines.append(f"config: {exc.code}")
        events = [render_event(e, tr) for e in app.runner.events]
        if events:
            lines += ["", "last run:"] + [f"  {e}" for e in events if e][-20:]
        log_path = core.CONFIG_DIR / "ui.log"
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            lines += ["", "ui.log (last 30 lines):"] + [f"  {_redact(l)}" for l in tail]
        text = "\n".join(lines) + "\n"
        for firm in raw.get("firms", []):           # company ids stay on the machine
            if firm.get("cif"):
                text = text.replace(firm["cif"], "[cui]")
        return Response(text, mimetype="text/plain")

    @app.get("/favicon.svg")
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
               '<rect width="32" height="32" rx="7" fill="#1d5fd1"/>'
               '<path d="M9 8h14v3H9zm0 6h14v3H9zm0 6h9v3H9z" fill="#fff"/></svg>')
        return Response(svg, mimetype="image/svg+xml")

    def load_cfg_or_redirect():
        try:
            return core.load_config(), None
        except core.ConfigError:
            flash(t()("config_needed"), "info")
            return None, redirect(url_for("start"))

    def fmt_ts(ts) -> str:
        return fmt_date(datetime.fromtimestamp(ts)) if ts else ""

    def form_ctx(errors=None, next_url=None, values=None) -> dict:
        stored = core.read_config_raw()
        firm = core.selected_firm(stored) or {}
        raw = {"environment": core.DEFAULT_ENVIRONMENT,
               "redirect_uri": core.DEFAULT_REDIRECT_URI,
               "base_dir": str(core.DEFAULT_BASE_DIR),
               **stored, "cif": firm.get("cif", ""),
               "base_dir": firm.get("base_dir") or str(core.DEFAULT_BASE_DIR),
               "firm_name": firm.get("name") or core.LEGACY_FIRM_NAME}
        if values:
            raw = {**raw, **values}
        return {"raw": raw, "errors": errors or {}, "next_url": next_url,
                "environments": list(core.REST_BASE),
                "secret_set": bool(core.read_config_raw().get("client_secret")),
                "config_problem": core.config_problem(),
                "adv_open": app.last_auth_code in ("oauth_invalid_request", "oauth_invalid_client")}

    def auth_ctx(auth_exc: core.ConfigError | None = None, wizard=False, auth_text=None) -> dict:
        tok = core.token_status()
        tr = t()
        return {"auth": tok, "auth_expires": fmt_ts(tok["expires_at"]),
                "pending": app.pending_auth, "wizard": wizard,
                "auth_error": auth_text or (describe(auth_exc.code, tr) if auth_exc else None),
                "auth_more": describe_more(auth_exc.code, tr) if auth_exc else None,
                "auth_detail": technical_detail(str(auth_exc)) if auth_exc else None}

    def render_settings(status=200, auth_exc=None, errors=None, values=None, auth_text=None):
        return render_template("settings.html", **form_ctx(errors, None, values),
                               **auth_ctx(auth_exc, auth_text=auth_text)), status

    def render_wizard(step: int, status=200, auth_exc=None, errors=None, values=None,
                      auth_text=None):
        ctx = {"step": step}
        if step == 2:
            ctx.update(form_ctx(errors, url_for("start", step=3), values))
        if step == 3:
            ctx.update(auth_ctx(auth_exc, wizard=True, auth_text=auth_text))
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
        events = app.runner.events
        lines = [line for line in (render_event(e, tr) for e in events) if line]
        last_error = next((render_event(e, tr) for e in reversed(events)
                           if e.get("type") == "SyncError"), None)
        return render_template("home.html", report=report, lines=lines,
                               auth=core.token_status(),
                               last_error=None if app.runner.running else last_error)

    @app.get("/start")
    def start():
        step = request.args.get("step", 1, type=int)
        if step == 3:
            try:
                core.load_config()
            except core.ConfigError:
                flash(t()("config_needed"), "info")
                return redirect(url_for("start", step=2))
        return render_wizard(step if step in (1, 2, 3) else 1)

    @app.get("/facturi")
    def invoices():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        month = request.args.get("luna", "").strip()
        supplier = request.args.get("furnizor", "").strip()
        rows = core.list_invoices(cfg["firm_id"], month or None, supplier or None)
        return render_template("invoices.html", rows=rows, months=core.invoice_months(cfg["firm_id"]),
                               month=month, supplier=supplier,
                               base_dir=str(Path(cfg["base_dir"]).expanduser()))

    @app.get("/pdf/<download_id>")
    def pdf(download_id):
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        row = core.invoice_by_download_id(cfg["firm_id"], download_id)
        if not row or not row.get("pdf_path") or not Path(row["pdf_path"]).is_file():
            abort(404, description="err_pdf_missing")
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
            flash(t()("sync_busy"), "info")   # a double-click, not an error page
        return redirect(url_for("home"), code=303)

    @app.get("/sync/events")
    def sync_events():
        since = request.args.get("since", 0, type=int)
        tr = t()

        def generate():
            touch = lambda: setattr(app, "last_activity", time.time())  # noqa: E731
            for ev in app.runner.stream(since, touch=touch):
                payload = {**ev, "text": render_event(ev, tr)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/alege-dosar")
    def choose_folder():
        initial = request.form.get("initial") or str(core.DEFAULT_BASE_DIR)
        return {"path": app.pick_folder(initial)}

    @app.post("/deschide-dosar")
    def open_folder():
        cfg, resp = load_cfg_or_redirect()
        if resp:
            return resp
        folder = Path(cfg["base_dir"]).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)   # exists only after a first sync
            app.open_path(folder)
        except Exception:  # noqa: BLE001 — a missing opener must not 500
            log.exception("could not open %s", folder)
            flash(t()("folder_open_failed", path=str(folder)), "info")
        else:
            flash(t()("folder_opened"), "ok")
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
        folder = values["base_dir"]
        if not folder or not Path(folder).expanduser().is_absolute():
            errors["base_dir"] = tr("val_folder")
        if not values["client_id"]:
            errors["client_id"] = tr("val_required")
        if not values["cif"]:
            errors["cif"] = tr("val_required")
        elif not values["cif"].isdigit():
            errors["cif"] = tr("val_cif_digits")
        if not form.get("client_secret") and not core.read_config_raw().get("client_secret"):
            errors["client_secret"] = tr("val_required")
        if values["environment"] not in core.REST_BASE:
            errors["environment"] = tr("val_environment")
        if errors:
            if next_url.startswith("/start"):
                return render_wizard(2, 400, errors=errors, values=values)
            return render_settings(400, errors=errors, values=values)

        raw = core.read_config_raw()
        firm_values = {"cif": values.pop("cif"), "base_dir": values.pop("base_dir")}
        raw.update(values)
        firm = core.selected_firm(raw)
        name = form.get("name", "").strip()
        if firm:                                     # WP7a: the form edits the selected firm
            firm.update(firm_values)
            if name:
                firm["name"] = name
        else:
            core.add_firm(raw, name or core.LEGACY_FIRM_NAME, firm_values["cif"],
                          firm_values["base_dir"])
        secret = form.get("client_secret", "")
        if secret:                                   # blank keeps the stored secret
            raw["client_secret"] = secret
        core.save_config(raw)
        flash(tr("settings_saved"), "ok")
        return redirect(next_url, code=303)

    # ---- firms (WP7c): the header switcher and the list in Setări -------------- #

    @app.post("/firma")
    def switch_firm():
        raw = core.read_config_raw()
        try:
            core.select_firm(raw, request.form.get("firm", ""))
        except core.ConfigError:
            pass                                     # unknown id: leave the selection as is
        else:
            core.save_config(raw)
        return redirect(_safe_next(request.form.get("next"), url_for("home")), code=303)

    @app.post("/firme/adauga")
    def add_firm():
        tr = t()
        name = request.form.get("name", "").strip()
        cif = core.normalize_cif(request.form.get("cif", ""))
        if not name or not cif or not cif.isdigit():
            return render_settings(400, errors={"firm_new": tr("val_firm_new")})
        raw = core.read_config_raw()
        firm = core.add_firm(raw, name, cif)
        raw["selected_firm"] = firm["id"]            # you add a firm to work on it
        core.save_config(raw)
        flash(tr("firm_added", name=name), "ok")
        return redirect(url_for("settings"), code=303)

    @app.post("/firme/sterge")
    def remove_firm():
        tr = t()
        raw = core.read_config_raw()
        firm_id = request.form.get("firm", "")
        if len(raw.get("firms", [])) <= 1:
            flash(tr("firm_last"), "info")
        elif any(f["id"] == firm_id for f in raw["firms"]):
            core.remove_firm(raw, firm_id)
            core.save_config(raw)
            flash(tr("firm_removed"), "ok")
        return redirect(url_for("settings"), code=303)

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
        wizard = bool(request.form.get("wizard"))
        if app.pending_auth is None:
            text = t()("auth_not_pending")
            return (render_wizard(3, 400, auth_text=text) if wizard
                    else render_settings(400, auth_text=text))
        try:
            core.complete_auth(cfg, app.pending_auth, request.form.get("pasted", ""))
        except core.ConfigError as exc:
            app.last_auth_code = exc.code
            if exc.code in ("state_mismatch", "no_code"):
                app.pending_auth = None        # a fresh state/URL pair on "start over"
            if wizard:
                return render_wizard(3, auth_exc=exc)
            return render_settings(auth_exc=exc)
        app.pending_auth = None
        app.last_auth_code = None
        flash(t()("wiz_done" if wizard else "auth_ok"), "ok")
        return redirect(url_for("home") if wizard else url_for("settings"), code=303)

    @app.post("/iesire")
    def quit_app():
        if app.runner.running:
            flash(t()("quit_busy"), "info")
            return redirect(url_for("settings"), code=303)
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


def _find_running(preferred: int) -> int | None:
    """Our instance may sit on a fallback port; look wherever we might have bound."""
    for port in range(preferred, preferred + PORT_RANGE):
        if _already_running(port):
            return port
    return None


def _idle_expired(app, now: float, idle_minutes: int) -> bool:
    """Idle means: no request, no live stream, no running sync — and a grace period
    after a run so a long sync's summary page can still render."""
    return (now - app.last_activity > idle_minutes * 60
            and not app.runner.running
            and now - app.runner.finished_at > FINISH_GRACE_SECONDS)


def run_ui(port: int = DEFAULT_PORT, open_browser: bool = True,
           idle_minutes: int = IDLE_MINUTES) -> None:
    """Serve the UI on 127.0.0.1, open the browser, exit on Quit or after idling.

    A second launch (double-clicking the app again) reuses the running instance:
    it just opens the browser to it instead of starting another server.
    """
    core.setup_frozen_logging()
    found = _find_running(port)
    if found:
        log.info("instance already running on port %d; opening browser", found)
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{found}/")
        return

    try:
        from werkzeug.serving import make_server

        port = _free_port(port)
        app = create_app()
        server = make_server("127.0.0.1", port, app, threaded=True)
    except Exception as exc:
        # The only trace a windowed bundle leaves: reason in the message, so a
        # grep of ui.log finds it without reading the traceback.
        log.exception("UI failed to start: %s", exc)
        raise
    app.shutdown_hook = lambda: threading.Thread(target=server.shutdown, daemon=True).start()

    def watchdog():
        while True:
            time.sleep(30)
            if _idle_expired(app, time.time(), idle_minutes):
                log.info("idle for %d minutes; shutting down", idle_minutes)
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log.info("eFactura Sync %s listening on %s", __version__, url)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    server.serve_forever()
