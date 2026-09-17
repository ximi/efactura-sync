"""Map engine codes to user copy (WP3, 2026-09-17).

Rule: no English internal, HTTP status or exception text is ever the primary
message a user sees. Events and errors carry a machine `code`; this module turns
codes into localized text via the strings table. The raw message is kept on the
event for diagnostics, never shown as the headline.
"""


def describe(code: str, t) -> str:
    """Localized headline for an error code; generic fallback for unknown codes."""
    if code.startswith("oauth_"):
        key = f"err_{code}"
        return t(key) if t.has(key) else t("err_oauth_other", code=code[len("oauth_"):])
    key = f"err_{code}"
    return t(key) if t.has(key) else t("err_unknown")


def technical_detail(message: str) -> str:
    """First line of a raw message — enough to search support docs, no hint blocks."""
    return (message or "").strip().splitlines()[0] if message else ""


def render_event(ev: dict, t) -> str:
    """One log line for a serialized sync event."""
    kind = ev.get("type")
    if kind == "SyncStarted":
        return t("ev_started", days=ev["lookback_days"], env=ev["environment"], cif=ev["cif"])
    if kind == "MessagesListed":
        return t("ev_listed", count=ev["count"])
    if kind == "Notice":
        key = f"notice_{ev.get('code', '')}"
        return t(key) if t.has(key) else t("err_unknown")
    if kind == "Duplicate":
        return t("ev_dup", id=ev["download_id"])
    if kind == "PdfFailed":
        return "! " + t("ev_pdf_failed", invoice=ev["invoice_id"])
    if kind == "InvoiceDone":
        mark = "✓ " if ev.get("pdf_ok") else "· "
        return mark + t("ev_done", invoice=ev["invoice_id"], supplier=ev["supplier_name"],
                        date=ev.get("issue_date") or "")
    if kind == "SyncError":
        head = describe(ev.get("code", "unknown"), t)
        prefix = f"{ev['download_id']}: " if ev.get("download_id") else ""
        return "✗ " + prefix + head
    if kind == "SyncFinished":
        return t("ev_finished", new=ev["new"], dup=ev["duplicates"], pdf=ev["pdf_failed"],
                 err=ev["errors"])
    return kind or ""
