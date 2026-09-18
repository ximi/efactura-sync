"""Map engine codes to user copy (WP3, 2026-09-17).

Rule: no English internal, HTTP status or exception text is ever the primary
message a user sees. Events and errors carry a machine `code`; this module turns
codes into localized text via the strings table. The raw message is kept on the
event for diagnostics, never shown as the headline.
"""

from datetime import datetime


def describe(code: str, t) -> str:
    """Localized headline for an error code; generic fallback for unknown codes."""
    if code.startswith("oauth_"):
        key = f"err_{code}"
        return t(key) if t.has(key) else t("err_oauth_other", code=code[len("oauth_"):])
    key = f"err_{code}"
    return t(key) if t.has(key) else t("err_unknown")


def describe_more(code: str, t) -> str | None:
    """Optional longer help for a code (shown collapsed), or None."""
    key = f"err_{code}_more"
    return t(key) if t.has(key) else None


def technical_detail(message: str) -> str:
    """First line of a raw message, capped — enough to search support docs; never a
    hint block, never a whole ANAF response body."""
    if not message:
        return ""
    line = message.strip().splitlines()[0]
    return line if len(line) <= 160 else line[:157] + "…"


# ---- dates the Romanian way ------------------------------------------------

def fmt_date(value) -> str:
    """'2026-03-14' or a datetime -> '14.03.2026'; anything unparseable unchanged."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    try:
        return datetime.fromisoformat(str(value)[:19]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def fmt_datetime(value) -> str:
    """'2026-09-17T14:33:02' -> '17.09.2026, 14:33'."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)[:19]).strftime("%d.%m.%Y, %H:%M")
    except ValueError:
        return str(value)


def fmt_month(value: str, t) -> str:
    """'2026-03' -> 'martie 2026' (localized month names from the strings table)."""
    try:
        year, month = value.split("-")[:2]
        return f"{t('months').split(',')[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return value


# ---- log lines ---------------------------------------------------------------

def render_event(ev: dict, t) -> str:
    """One log line for a serialized sync event; "" for lines users need not see."""
    kind = ev.get("type")
    if kind == "SyncStarted":
        if ev.get("environment") == "prod":
            return t("ev_started", days=ev["lookback_days"])
        return t("ev_started_test", days=ev["lookback_days"], cif=ev["cif"])
    if kind == "MessagesListed":
        return t("ev_listed", count=ev["count"])
    if kind == "Notice":
        code = ev.get("code", "")
        if code == "legacy_listing":          # internal fallback, meaningless to a user
            return ""
        key = f"notice_{code}"
        return t(key) if t.has(key) else t("notice_generic")
    if kind == "Duplicate":
        return t("ev_dup", id=ev["download_id"])
    if kind == "PdfFailed":
        return "⚠ " + t("ev_pdf_failed", invoice=ev["invoice_id"])
    if kind == "InvoiceDone":
        line = t("ev_done", invoice=ev["invoice_id"], supplier=ev["supplier_name"],
                 date=fmt_date(ev.get("issue_date")))
        if ev.get("pdf_ok"):
            return "✓ " + line
        return "⚠ " + line + " — " + t("no_pdf")
    if kind == "SyncError":
        head = describe(ev.get("code", "unknown"), t)
        prefix = f"{ev['download_id']}: " if ev.get("download_id") else ""
        return "✗ " + prefix + head
    if kind == "SyncFinished":
        if not ev["new"] and not ev["errors"]:
            return t("ev_finished_none")
        return t("ev_finished", new=ev["new"], dup=ev["duplicates"], pdf=ev["pdf_failed"],
                 err=ev["errors"])
    return t("err_unknown")
