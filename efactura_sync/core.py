"""
Engine for downloading and organizing received ANAF e-Factura invoices.

This module owns configuration, OAuth, the ANAF REST calls, the SQLite dedup
store, UBL parsing, and the filing logic. It never prints or prompts: progress
is reported as typed events yielded by `sync()`, and interactive steps are split
into begin/complete pairs so that both the CLI and the web UI can drive them.
(WP1 decision, 2026-09-17: core reports, front ends render.)

ANAF only stores invoices as UBL XML; the human-readable PDF is produced by
ANAF's public `transformare` service.
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Union
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from lxml import etree

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CONFIG_DIR = Path(os.environ.get("ANAF_CONFIG_DIR", Path.home() / ".anaf_invoices"))
CONFIG_PATH = CONFIG_DIR / "config.json"
TOKENS_PATH = CONFIG_DIR / "tokens.json"
DB_PATH = CONFIG_DIR / "invoices.db"

AUTH_URL = "https://logincert.anaf.ro/anaf-oauth2/v1/authorize"
TOKEN_URL = "https://logincert.anaf.ro/anaf-oauth2/v1/token"

# Authenticated REST API (listing + download).
REST_BASE = {
    "prod": "https://api.anaf.ro/prod/FCTEL/rest",
    "test": "https://api.anaf.ro/test/FCTEL/rest",
}
# Public, unauthenticated XML -> PDF renderer (same host for prod/test).
TRANSFORM_BASE = "https://webservicesp.anaf.ro/prod/FCTEL/rest/transformare"

LOOKBACK_DAYS = 60
TOKEN_SKEW_SECONDS = 300          # refresh this long before nominal expiry
HTTP_TIMEOUT = 60
POLITE_SLEEP = 1.0                # seconds between API calls
MAX_RETRIES = 4

# UBL namespaces (CIUS-RO is UBL 2.1).
NS = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


class ConfigError(Exception):
    """Raised when configuration or tokens are missing/invalid."""


# --------------------------------------------------------------------------- #
# Events yielded by sync()
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SyncStarted:
    environment: str
    cif: str
    lookback_days: int


@dataclass(frozen=True)
class MessagesListed:
    count: int


@dataclass(frozen=True)
class Notice:
    message: str


@dataclass(frozen=True)
class Duplicate:
    download_id: str
    reason: str


@dataclass(frozen=True)
class PdfFailed:
    download_id: str
    invoice_id: str
    message: str


@dataclass(frozen=True)
class InvoiceDone:
    download_id: str
    invoice_id: str
    supplier_name: str
    issue_date: str | None
    pdf_ok: bool
    pdf_path: str
    xml_path: str


@dataclass(frozen=True)
class SyncError:
    download_id: str
    message: str


@dataclass(frozen=True)
class SyncFinished:
    new: int
    duplicates: int
    pdf_failed: int
    errors: int
    base_dir: str


SyncEvent = Union[SyncStarted, MessagesListed, Notice, Duplicate, PdfFailed,
                  InvoiceDone, SyncError, SyncFinished]


# --------------------------------------------------------------------------- #
# Config & token storage
# --------------------------------------------------------------------------- #

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def load_config(env_override: str | None = None) -> dict:
    """Load config.json, apply env-var overrides, and validate."""
    if not CONFIG_PATH.exists():
        raise ConfigError(
            f"No config found at {CONFIG_PATH}.\n"
            f"Create it (see README.md). Minimal example:\n"
            '  {\n'
            '    "client_id": "...",\n'
            '    "client_secret": "...",\n'
            '    "redirect_uri": "https://localhost/callback",\n'
            '    "cif": "12345678",\n'
            '    "environment": "test"\n'
            '  }'
        )
    # Security review 2026-09-17: config.json carries client_secret but is created by
    # the user, so nothing else guarantees its mode. Tighten to 0600 if group/other
    # can read it. Idempotent; a failure here must not block a sync.
    try:
        if stat.S_IMODE(CONFIG_PATH.stat().st_mode) & 0o077:
            os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    # Environment-variable overrides.
    cfg["client_id"] = os.environ.get("ANAF_CLIENT_ID", cfg.get("client_id"))
    cfg["client_secret"] = os.environ.get("ANAF_CLIENT_SECRET", cfg.get("client_secret"))
    cfg["cif"] = str(os.environ.get("ANAF_CIF", cfg.get("cif", ""))).strip()
    if env_override:
        cfg["environment"] = env_override

    cfg.setdefault("environment", "test")
    cfg.setdefault("redirect_uri", "https://localhost/callback")
    cfg.setdefault("base_dir", str(CONFIG_DIR / "invoices"))

    missing = [k for k in ("client_id", "client_secret", "cif") if not cfg.get(k)]
    if missing:
        raise ConfigError(
            f"Missing required config value(s): {', '.join(missing)}.\n"
            f"Set them in {CONFIG_PATH} or via environment variables "
            f"(ANAF_CLIENT_ID, ANAF_CLIENT_SECRET, ANAF_CIF). See README.md."
        )
    if cfg["environment"] not in REST_BASE:
        raise ConfigError(f"environment must be one of {list(REST_BASE)}")

    cfg["cif"] = normalize_cif(cfg["cif"])
    return cfg


def normalize_cif(value: str) -> str:
    """ANAF wants the bare number: strip a leading RO and whitespace."""
    return re.sub(r"^RO", "", str(value or "").strip(), flags=re.IGNORECASE).strip()


def read_config_raw() -> dict:
    """The config file as written, without validation. {} if absent (WP2: settings form)."""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_config(raw: dict) -> None:
    """Atomically write config.json with owner-only permissions."""
    ensure_config_dir()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def load_tokens() -> dict | None:
    if not TOKENS_PATH.exists():
        return None
    with TOKENS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_tokens(tokens: dict) -> None:
    ensure_config_dir()
    tmp = TOKENS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(tokens, fh, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(TOKENS_PATH)


# --------------------------------------------------------------------------- #
# OAuth (authorization code + PKCE, split into begin / complete)
# --------------------------------------------------------------------------- #

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = _b64url(os.urandom(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(cfg: dict, code_challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": "EFACTURA",
        "state": state,
        "token_content_type": "jwt",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


# ANAF reports authorization problems by redirecting back with ?error=...
# access_denied is by far the most common and its real cause is never obvious,
# so spell the diagnosis out rather than echoing the bare code.
OAUTH_ERROR_HINTS = {
    "access_denied": (
        "This means the certificate authentication did not complete.\n"
        "Your client_id and redirect_uri are fine — ANAF only redirects back and\n"
        "echoes the state if it recognized them. Check the certificate side:\n"
        "  1. Is the USB token plugged in and its middleware/driver running?\n"
        "  2. Did the browser actually prompt you to CHOOSE a certificate?\n"
        "     No popup = the failure. Disable the popup blocker and retry.\n"
        "  3. Log in to https://pfinternet.anaf.ro with the certificate FIRST,\n"
        "     then re-open the authorization URL in that SAME browser.\n"
        "  4. Is the certificate registered in SPV with rights for this CIF\n"
        "     (legal/designated/authorized representative), and is e-Factura\n"
        "     activated for the company?\n"
        "  5. ANAF officially supports Windows + Chrome. Certificate middleware\n"
        "     on macOS is a frequent cause of the missing certificate prompt."
    ),
    "invalid_client": (
        "ANAF did not accept the client_id. Re-check it against the OAuth profile\n"
        "in the ANAF portal (and that the profile has the EFACTURA service ticked)."
    ),
    "invalid_request": (
        "ANAF rejected the authorization request parameters. Most often the\n"
        "redirect_uri in config.json does not exactly match the Callback URL\n"
        "registered in your ANAF OAuth profile (trailing slash, http vs https)."
    ),
}


def _oauth_error_message(err: str, desc: str = "") -> str:
    msg = f"ANAF returned an OAuth error: {err}"
    if desc:
        msg += f" — {desc}"
    hint = OAUTH_ERROR_HINTS.get(err)
    if hint:
        msg += "\n\n" + hint
    return msg


def parse_callback_params(pasted: str) -> dict:
    """Parse params out of a full redirect URL or a bare query fragment."""
    qs = parse_qs(urlparse(pasted).query)
    if not qs and ("code=" in pasted or "error=" in pasted):
        qs = parse_qs(pasted.lstrip("?&"))
    return qs


def extract_code(pasted: str) -> str:
    """Accept either a bare code or the full redirected URL and return the code.

    Raises ConfigError if the callback carries an OAuth error, so ANAF's actual
    complaint is surfaced instead of being posted to the token endpoint as a code.
    """
    pasted = pasted.strip()
    params = parse_callback_params(pasted)
    if "error" in params:
        raise ConfigError(_oauth_error_message(
            params["error"][0], params.get("error_description", [""])[0]))
    if "code" in params:
        return params["code"][0]
    return pasted


def _store_token_response(resp_json: dict) -> dict:
    """Normalize a token endpoint response and persist it."""
    expires_in = int(resp_json.get("expires_in", 0) or 0)
    tokens = {
        "access_token": resp_json["access_token"],
        "refresh_token": resp_json.get("refresh_token"),
        "token_type": resp_json.get("token_type", "Bearer"),
        "obtained_at": int(time.time()),
        "expires_at": int(time.time()) + expires_in if expires_in else None,
    }
    save_tokens(tokens)
    return tokens


@dataclass(frozen=True)
class PendingAuth:
    """State a front end must hold between showing the URL and receiving the paste."""
    url: str
    state: str
    code_verifier: str


def begin_auth(cfg: dict) -> PendingAuth:
    verifier, challenge = make_pkce()
    state = _b64url(os.urandom(16))
    return PendingAuth(build_authorize_url(cfg, challenge, state), state, verifier)


def complete_auth(cfg: dict, pending: PendingAuth, pasted: str) -> dict:
    """Exchange the pasted redirect URL (or bare code) for tokens and store them."""
    pasted = pasted.strip()
    params = parse_callback_params(pasted)
    # Report an ANAF error first: with no code, the state check is just noise.
    if "error" in params:
        raise ConfigError(_oauth_error_message(
            params["error"][0], params.get("error_description", [""])[0]))
    returned_state = params.get("state", [None])[0]
    if returned_state and returned_state != pending.state:
        raise ConfigError("State mismatch — aborting (possible CSRF). Re-run `auth`.")
    code = extract_code(pasted)
    if not code:
        raise ConfigError("No authorization code found in the pasted value.")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"],
        "code_verifier": pending.code_verifier,
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT,
                         headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise ConfigError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return _store_token_response(resp.json())


def refresh_tokens(cfg: dict, tokens: dict) -> dict:
    if not tokens.get("refresh_token"):
        raise ConfigError("No refresh token available. Re-run `auth`.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT,
                         headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise ConfigError(
            f"Token refresh failed ({resp.status_code}): {resp.text}\nRe-run `auth`."
        )
    new = resp.json()
    # ANAF may not return a new refresh token; keep the existing one if so.
    new.setdefault("refresh_token", tokens["refresh_token"])
    return _store_token_response(new)


def get_access_token(cfg: dict, force_refresh: bool = False) -> str:
    tokens = load_tokens()
    if not tokens:
        raise ConfigError("Not authenticated. Run `auth` first.")
    expired = (
        tokens.get("expires_at") is not None
        and time.time() >= tokens["expires_at"] - TOKEN_SKEW_SECONDS
    )
    if force_refresh or expired:
        tokens = refresh_tokens(cfg, tokens)
    return tokens["access_token"]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def api_get(cfg: dict, path: str, params: dict) -> requests.Response:
    """GET against the authenticated REST API with retry + token refresh."""
    base = REST_BASE[cfg["environment"]]
    url = f"{base}/{path}"
    refreshed = False
    for attempt in range(MAX_RETRIES):
        token = get_access_token(cfg, force_refresh=refreshed)
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 401 and not refreshed:
            refreshed = True  # token may be stale; refresh once and retry
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = min(2 ** attempt, 30)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(POLITE_SLEEP)
        return resp
    raise RuntimeError(f"Request to {url} failed after {MAX_RETRIES} attempts "
                       f"(last status {resp.status_code}).")


def xml_to_pdf(xml_bytes: bytes, standard: str) -> bytes:
    """Convert an invoice UBL XML to PDF via ANAF's public renderer.

    Tries with validation first; on a JSON error response retries with /DA
    (skip validation). Raises RuntimeError with the ANAF message if both fail.
    """
    headers = {"Content-Type": "text/plain"}
    last_msg = ""
    for suffix in ("", "/DA"):
        url = f"{TRANSFORM_BASE}/{standard}{suffix}"
        resp = requests.post(url, data=xml_bytes, headers=headers, timeout=HTTP_TIMEOUT)
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "application/pdf" in ctype:
            return resp.content
        # Error: ANAF returns JSON describing the problem.
        try:
            payload = resp.json()
            msgs = payload.get("Messages") or payload.get("messages") or []
            last_msg = "; ".join(m.get("message", "") for m in msgs) or json.dumps(payload)
        except ValueError:
            last_msg = resp.text[:500]
        time.sleep(POLITE_SLEEP)
    raise RuntimeError(f"PDF conversion failed: {last_msg}")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def connect_db() -> sqlite3.Connection:
    ensure_config_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            download_id   TEXT PRIMARY KEY,
            invoice_id    TEXT,
            supplier_name TEXT,
            supplier_cif  TEXT,
            issue_date    TEXT,
            message_date  TEXT,
            tip           TEXT,
            doc_type      TEXT,
            xml_sha256    TEXT,
            pdf_path      TEXT,
            xml_path      TEXT,
            zip_path      TEXT,
            pdf_ok        INTEGER DEFAULT 1,
            note          TEXT,
            downloaded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_invoices_hash ON invoices(xml_sha256);
        CREATE INDEX IF NOT EXISTS idx_invoices_invid ON invoices(invoice_id);

        CREATE TABLE IF NOT EXISTS duplicates (
            download_id TEXT,
            invoice_id  TEXT,
            reason      TEXT,
            seen_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    return conn


def db_has_download(conn: sqlite3.Connection, download_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM invoices WHERE download_id = ?", (download_id,))
    return cur.fetchone() is not None


def db_has_hash(conn: sqlite3.Connection, xml_sha256: str) -> bool:
    cur = conn.execute("SELECT 1 FROM invoices WHERE xml_sha256 = ?", (xml_sha256,))
    return cur.fetchone() is not None


def log_duplicate(conn: sqlite3.Connection, download_id: str, invoice_id: str,
                  reason: str) -> None:
    conn.execute(
        "INSERT INTO duplicates (download_id, invoice_id, reason, seen_at) "
        "VALUES (?, ?, ?, ?)",
        (download_id, invoice_id, reason, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def list_invoices(month: str | None = None, supplier: str | None = None) -> list[dict]:
    """Invoices for the UI table, newest first. month = 'YYYY-MM', supplier = substring."""
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT download_id, invoice_id, supplier_name, supplier_cif, issue_date, "
            "doc_type, pdf_ok, pdf_path FROM invoices "
            "WHERE (? = '' OR substr(issue_date, 1, 7) = ?) "
            "AND (? = '' OR lower(supplier_name) LIKE ?) "
            "ORDER BY issue_date DESC, downloaded_at DESC",
            (month or "", month or "", supplier or "", f"%{(supplier or '').lower()}%"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def invoice_months() -> list[str]:
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(issue_date, 1, 7) AS m FROM invoices "
            "WHERE issue_date IS NOT NULL ORDER BY m DESC"
        ).fetchall()
        return [r["m"] for r in rows if r["m"]]
    finally:
        conn.close()


def invoice_by_download_id(download_id: str) -> dict | None:
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT * FROM invoices WHERE download_id = ?", (download_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def token_status() -> dict:
    """Whether we hold tokens and when the access token nominally expires."""
    tokens = load_tokens()
    if not tokens or not tokens.get("access_token"):
        return {"authenticated": False, "expires_at": None}
    return {"authenticated": True, "expires_at": tokens.get("expires_at")}


# --------------------------------------------------------------------------- #
# XML parsing & file organization
# --------------------------------------------------------------------------- #

# Security review 2026-09-17: invoice XML is authored by the supplier, a third
# party. lxml >= 5 already ignores external entities by default; pinning it here
# keeps the guarantee independent of the installed version. no_network blocks
# DTD/entity fetches; huge_tree stays off to bound entity expansion.
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def parse_invoice_xml(xml_bytes: bytes) -> dict:
    """Extract key fields from a UBL Invoice or CreditNote.

    Returns a dict with invoice_id, issue_date, supplier_name, supplier_cif,
    doc_type ('invoice' | 'credit_note') and standard ('FACT1' | 'FCN').
    """
    root = etree.fromstring(xml_bytes, parser=_XML_PARSER)
    tag = etree.QName(root).localname  # 'Invoice' or 'CreditNote'
    is_credit_note = tag == "CreditNote"

    def first(xpath: str) -> str | None:
        vals = root.xpath(xpath, namespaces=NS)
        if not vals:
            return None
        v = vals[0]
        text = v if isinstance(v, str) else v.text
        return text.strip() if text else None

    invoice_id = first("./cbc:ID/text()")
    issue_date = first("./cbc:IssueDate/text()")
    supplier_name = first(
        "./cac:AccountingSupplierParty/cac:Party/cac:PartyName/cbc:Name/text()"
    )
    # Fall back to the legal registration name if PartyName is absent.
    if not supplier_name:
        supplier_name = first(
            "./cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/"
            "cbc:RegistrationName/text()"
        )
    supplier_cif = first(
        "./cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID/text()"
    )
    if not supplier_cif:
        supplier_cif = first(
            "./cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID/text()"
        )

    return {
        "invoice_id": invoice_id or "UNKNOWN",
        "issue_date": issue_date,
        "supplier_name": supplier_name or "unknown_supplier",
        "supplier_cif": supplier_cif or "",
        "doc_type": "credit_note" if is_credit_note else "invoice",
        "standard": "FCN" if is_credit_note else "FACT1",
    }


_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize(value: str, max_len: int = 60) -> str:
    """Make a string safe for use in a filename component."""
    value = (value or "").strip()
    value = _SANITIZE_RE.sub("_", value)
    value = value.strip("._-") or "x"
    return value[:max_len]


def select_invoice_xml(zip_bytes: bytes) -> tuple[str, bytes]:
    """Return (name, bytes) of the invoice XML inside the ANAF ZIP.

    The ZIP contains the invoice plus a `semnatura_*.xml` signature; pick the
    one that is not the signature.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        candidates = [n for n in xml_names
                      if not Path(n).name.lower().startswith("semnatura")]
        chosen = candidates[0] if candidates else (xml_names[0] if xml_names else None)
        if not chosen:
            raise RuntimeError("No XML file found inside the downloaded ZIP.")
        return chosen, zf.read(chosen)


def build_target_paths(base_dir: Path, meta: dict, message_date: str) -> tuple[Path, str]:
    """Return (directory, basename) for filing this invoice."""
    date_str = meta.get("issue_date") or (message_date[:10] if message_date else "")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        dt = datetime.now()
    directory = base_dir / f"{dt:%Y}" / f"{dt:%m}"
    basename = "_".join([
        sanitize(meta["invoice_id"], 40),
        sanitize(meta["supplier_name"], 40),
        f"{dt:%Y-%m-%d}",
    ])
    return directory, basename


CSV_FIELDS = [
    "invoice_id", "supplier_name", "supplier_cif", "issue_date", "message_date",
    "tip", "doc_type", "download_id", "pdf_path", "xml_path", "xml_sha256",
    "pdf_ok", "downloaded_at",
]

# Security review 2026-09-17: supplier_name and invoice_id are supplier-controlled,
# and invoices.csv exists to be opened in Excel/LibreOffice, where a cell starting
# with = + - @ (or tab/CR) executes as a formula. A leading apostrophe makes the
# spreadsheet treat it as text and display the value unchanged.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def append_csv(base_dir: Path, row: dict) -> None:
    csv_path = base_dir / "invoices.csv"
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow({k: _csv_safe(v) for k, v in row.items()})


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def _list_paginated(cfg: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=LOOKBACK_DAYS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    messages: list[dict] = []
    page = 1
    while True:
        resp = api_get(cfg, "listaMesajePaginatieFactura", {
            "startTime": start_ms,
            "endTime": end_ms,
            "cif": cfg["cif"],
            "pagina": page,
            "filtru": "P",
        })
        data = resp.json()
        batch = data.get("mesaje") or []
        messages.extend(batch)
        total_pages = int(data.get("numar_total_pagini", 1) or 1)
        if page >= total_pages or not batch:
            break
        page += 1
    return messages


def _list_legacy(cfg: dict) -> list[dict]:
    resp = api_get(cfg, "listaMesajeFactura", {
        "zile": LOOKBACK_DAYS,
        "cif": cfg["cif"],
        "filtru": "P",
    })
    return resp.json().get("mesaje") or []


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #

def sync(cfg: dict) -> Iterator[SyncEvent]:
    """Download new received invoices and file them, yielding progress events.

    Always ends with SyncFinished unless an exception escapes before listing.
    Per-invoice problems are reported as events and never abort the run.
    """
    base_dir = Path(cfg["base_dir"]).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db()
    try:
        yield SyncStarted(cfg["environment"], cfg["cif"], LOOKBACK_DAYS)

        try:
            messages = _list_paginated(cfg)
        except Exception as exc:  # noqa: BLE001 — fall back to the legacy endpoint
            yield Notice(f"Paginated listing unavailable ({exc}); using legacy endpoint.")
            messages = _list_legacy(cfg)
        yield MessagesListed(len(messages))

        new_count = dup_count = pdf_failed = error_count = 0

        for msg in messages:
            download_id = str(msg.get("id") or "").strip()
            tip = (msg.get("tip") or "").upper()
            message_date = msg.get("data_creare") or ""
            if not download_id:
                continue
            # Only inbound invoices (skip buyer messages / error notifications).
            if tip and tip not in ("FACTURA", "FACTURA PRIMITA"):
                continue
            if db_has_download(conn, download_id):
                reason = "already downloaded (download_id)"
                log_duplicate(conn, download_id, "", reason)
                dup_count += 1
                yield Duplicate(download_id, reason)
                continue

            try:
                zresp = api_get(cfg, "descarcare", {"id": download_id})
                zip_bytes = zresp.content

                xml_name, xml_bytes = select_invoice_xml(zip_bytes)
                content_hash = hashlib.sha256(xml_bytes).hexdigest()
                if db_has_hash(conn, content_hash):
                    reason = "duplicate content (xml hash)"
                    log_duplicate(conn, download_id, "", reason)
                    dup_count += 1
                    yield Duplicate(download_id, reason)
                    continue

                meta = parse_invoice_xml(xml_bytes)
                directory, basename = build_target_paths(base_dir, meta, message_date)
                directory.mkdir(parents=True, exist_ok=True)

                xml_path = directory / f"{basename}.xml"
                zip_path = directory / f"{basename}.zip"
                pdf_path = directory / f"{basename}.pdf"
                xml_path.write_bytes(xml_bytes)
                zip_path.write_bytes(zip_bytes)

                pdf_ok = True
                note = ""
                try:
                    pdf_bytes = xml_to_pdf(xml_bytes, meta["standard"])
                    pdf_path.write_bytes(pdf_bytes)
                except Exception as pexc:  # noqa: BLE001 — keep XML, flag the PDF
                    pdf_ok = False
                    note = str(pexc)[:300]
                    pdf_failed += 1
                    yield PdfFailed(download_id, meta["invoice_id"], note)

                now_iso = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """INSERT INTO invoices
                       (download_id, invoice_id, supplier_name, supplier_cif, issue_date,
                        message_date, tip, doc_type, xml_sha256, pdf_path, xml_path,
                        zip_path, pdf_ok, note, downloaded_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (download_id, meta["invoice_id"], meta["supplier_name"],
                     meta["supplier_cif"], meta["issue_date"], message_date, tip,
                     meta["doc_type"], content_hash,
                     str(pdf_path) if pdf_ok else "", str(xml_path), str(zip_path),
                     int(pdf_ok), note, now_iso),
                )
                conn.commit()
                append_csv(base_dir, {
                    "invoice_id": meta["invoice_id"],
                    "supplier_name": meta["supplier_name"],
                    "supplier_cif": meta["supplier_cif"],
                    "issue_date": meta["issue_date"] or "",
                    "message_date": message_date,
                    "tip": tip,
                    "doc_type": meta["doc_type"],
                    "download_id": download_id,
                    "pdf_path": str(pdf_path) if pdf_ok else "",
                    "xml_path": str(xml_path),
                    "xml_sha256": content_hash,
                    "pdf_ok": int(pdf_ok),
                    "downloaded_at": now_iso,
                })
                new_count += 1
                yield InvoiceDone(download_id, meta["invoice_id"], meta["supplier_name"],
                                  meta["issue_date"], pdf_ok,
                                  str(pdf_path) if pdf_ok else "", str(xml_path))

            except Exception as exc:  # noqa: BLE001 — record and continue with next msg
                error_count += 1
                yield SyncError(download_id, str(exc))

        set_state(conn, "last_run", datetime.now().isoformat(timespec="seconds"))
        yield SyncFinished(new_count, dup_count, pdf_failed, error_count, str(base_dir))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RecentInvoice:
    invoice_id: str
    supplier_name: str
    issue_date: str | None
    pdf_ok: bool


@dataclass(frozen=True)
class StatusReport:
    environment: str
    cif: str
    last_run: str | None
    total: int
    this_month: int
    pdf_failed: int
    duplicate_hits: int
    base_dir: str
    db_path: str
    recent: list[RecentInvoice] = field(default_factory=list)


def status_report(cfg: dict, recent_limit: int = 10) -> StatusReport:
    conn = connect_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"]
        this_month = conn.execute(
            "SELECT COUNT(*) AS c FROM invoices WHERE substr(issue_date,1,7) = ?",
            (datetime.now().strftime("%Y-%m"),),
        ).fetchone()["c"]
        dup_total = conn.execute("SELECT COUNT(*) AS c FROM duplicates").fetchone()["c"]
        pdf_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM invoices WHERE pdf_ok = 0"
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT invoice_id, supplier_name, issue_date, pdf_ok "
            "FROM invoices ORDER BY downloaded_at DESC LIMIT ?", (recent_limit,)
        ).fetchall()
        recent = [RecentInvoice(r["invoice_id"], r["supplier_name"], r["issue_date"],
                                bool(r["pdf_ok"])) for r in rows]
        last_run = get_state(conn, "last_run")
    finally:
        conn.close()
    return StatusReport(
        environment=cfg["environment"], cif=cfg["cif"],
        last_run=last_run,
        total=total, this_month=this_month, pdf_failed=pdf_failed,
        duplicate_hits=dup_total,
        base_dir=str(Path(cfg["base_dir"]).expanduser()), db_path=str(DB_PATH),
        recent=recent,
    )
