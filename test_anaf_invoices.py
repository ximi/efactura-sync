"""Offline unit tests for anaf_invoices.py helpers.

Run: python -m pytest test_anaf_invoices.py  (or: python test_anaf_invoices.py)
These tests do not touch the network or the user's real ~/.anaf_invoices.
"""

import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path

# Point the config dir at a throwaway location BEFORE importing the module,
# so connect_db() never touches the real ~/.anaf_invoices.
_TMP = tempfile.mkdtemp(prefix="anaf_test_")
os.environ["ANAF_CONFIG_DIR"] = _TMP

from efactura_sync import core as ai  # noqa: E402
from conftest import SAMPLE_INVOICE, SAMPLE_CREDIT_NOTE  # noqa: E402


def test_parse_invoice():
    meta = ai.parse_invoice_xml(SAMPLE_INVOICE)
    assert meta["invoice_id"] == "FAC-2026-001"
    assert meta["issue_date"] == "2026-03-14"
    assert meta["supplier_name"] == "Furnizor Demo SRL"
    assert meta["supplier_cif"] == "RO87654321"
    assert meta["doc_type"] == "invoice"
    assert meta["standard"] == "FACT1"


def test_parse_credit_note():
    meta = ai.parse_invoice_xml(SAMPLE_CREDIT_NOTE)
    assert meta["invoice_id"] == "CN-2026-009"
    assert meta["doc_type"] == "credit_note"
    assert meta["standard"] == "FCN"
    assert meta["supplier_name"] == "Alt Furnizor & Co"


def test_sanitize():
    assert ai.sanitize("Furnizor Demo SRL") == "Furnizor_Demo_SRL"
    assert ai.sanitize("Alt Furnizor & Co") == "Alt_Furnizor_Co"
    assert ai.sanitize("../../etc/passwd") == "etc_passwd"
    assert ai.sanitize("") == "x"
    assert len(ai.sanitize("a" * 200, max_len=40)) == 40


def test_build_target_paths():
    base = Path("/tmp/base")
    meta = ai.parse_invoice_xml(SAMPLE_INVOICE)
    directory, basename = ai.build_target_paths(base, meta, "2026-03-14T10:00:00")
    assert directory == base / "2026" / "03"
    assert basename == "FAC-2026-001_Furnizor_Demo_SRL_2026-03-14"


def test_build_target_paths_falls_back_to_message_date():
    base = Path("/tmp/base")
    meta = dict(ai.parse_invoice_xml(SAMPLE_INVOICE))
    meta["issue_date"] = None
    directory, basename = ai.build_target_paths(base, meta, "2025-12-31T09:00:00")
    assert directory == base / "2025" / "12"
    assert basename.endswith("2025-12-31")


def _make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_select_invoice_xml_skips_signature():
    zip_bytes = _make_zip({
        "semnatura_4017.xml": b"<signature/>",
        "4017.xml": SAMPLE_INVOICE,
    })
    name, data = ai.select_invoice_xml(zip_bytes)
    assert name == "4017.xml"
    assert data == SAMPLE_INVOICE


def test_extract_code():
    assert ai.extract_code("ABC123") == "ABC123"
    assert ai.extract_code(
        "https://localhost/callback?code=XYZ789&state=foo") == "XYZ789"


def test_extract_code_reports_anaf_error_callback():
    """A real access_denied callback must surface ANAF's error, not be sent as a code."""
    url = "https://localhost/callback?error=access_denied&state=synthetic-state-0001"
    try:
        ai.extract_code(url)
    except ai.ConfigError as exc:
        assert "access_denied" in str(exc)
        assert "certificate authentication did not complete" in str(exc)
    else:
        raise AssertionError("expected ConfigError for an access_denied callback")


def test_extract_code_error_with_description():
    url = "https://localhost/callback?error=invalid_client&error_description=bad+id"
    try:
        ai.extract_code(url)
    except ai.ConfigError as exc:
        assert "invalid_client" in str(exc) and "bad id" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_parse_callback_params_accepts_bare_query():
    params = ai.parse_callback_params("code=ABC&state=xyz")
    assert params["code"] == ["ABC"] and params["state"] == ["xyz"]


def test_pkce_challenge_is_b64url_unpadded():
    verifier, challenge = ai.make_pkce()
    assert "=" not in challenge and "+" not in challenge and "/" not in challenge
    expected = ai._b64url(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected


def test_build_authorize_url():
    cfg = {"client_id": "cid", "redirect_uri": "https://localhost/callback"}
    url = ai.build_authorize_url(cfg, "chal", "st")
    assert url.startswith(ai.AUTH_URL + "?")
    assert "response_type=code" in url
    assert "scope=EFACTURA" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url


def _csv_row(base: Path, **overrides) -> dict:
    row = {f: "" for f in ai.CSV_FIELDS}
    row.update({"invoice_id": "INV-1", "supplier_name": "Plain SRL", "pdf_ok": 1})
    row.update(overrides)
    ai.append_csv(base, row)
    import csv as _csv
    with (base / "invoices.csv").open(newline="", encoding="utf-8") as fh:
        return list(_csv.DictReader(fh))[-1]


def test_csv_neutralizes_formula_cells():
    """Supplier-controlled text must not open as a live formula in Excel/LibreOffice.

    Security review 2026-09-17: supplier_name comes from third-party XML and the
    CSV exists to be opened by an accountant, so a leading = + - @ (or tab/CR)
    must be neutralized. Filenames are already sanitized separately.
    """
    base = Path(tempfile.mkdtemp())
    hostile = '=HYPERLINK("http://evil","click")'
    row = _csv_row(base, supplier_name=hostile, invoice_id="+1234")
    assert not row["supplier_name"].startswith("="), row["supplier_name"]
    assert not row["invoice_id"].startswith("+"), row["invoice_id"]
    # The original text is preserved behind the guard, not destroyed.
    assert hostile in row["supplier_name"]
    for prefix in ("-", "@", "\t", "\r"):
        r = _csv_row(base, supplier_name=prefix + "x")
        assert not r["supplier_name"].startswith(prefix)


def test_csv_leaves_ordinary_cells_untouched():
    base = Path(tempfile.mkdtemp())
    row = _csv_row(base, supplier_name="Furnizor Demo SRL", invoice_id="FAC-2026-001")
    assert row["supplier_name"] == "Furnizor Demo SRL"
    assert row["invoice_id"] == "FAC-2026-001"


import sys as _sys
import pytest as _pytest


@_pytest.mark.skipif(_sys.platform == "win32", reason="POSIX file modes")
def test_load_config_tightens_loose_permissions():
    """config.json holds client_secret; a 0644 file must be tightened to 0600 on load.

    Security review 2026-09-17: the README promised this but the script never
    writes config.json, so nothing enforced it.
    """
    import json, stat
    cfg_path = ai.CONFIG_PATH
    cfg_path.write_text(json.dumps({
        "client_id": "cid", "client_secret": "sec", "cif": "RO12345678",
    }))
    os.chmod(cfg_path, 0o644)
    try:
        cfg = ai.load_config()
        mode = stat.S_IMODE(cfg_path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        assert cfg["cif"] == "12345678"  # RO prefix stripped
    finally:
        cfg_path.unlink()


def test_parse_never_resolves_external_entities():
    """Hostile supplier XML must never pull local file contents into parsed fields.

    lxml >= 5.0 no longer resolves external entities by default; this pins the
    guarantee to our parser configuration rather than to the library's default.
    """
    secret = Path(tempfile.mkdtemp()) / "secret.txt"
    secret.write_text("LEAKED-TOKEN-CONTENTS")
    hostile = f"""<?xml version="1.0"?>
<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file://{secret}">]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>X</cbc:ID><cbc:IssueDate>2026-01-01</cbc:IssueDate>
 <cac:AccountingSupplierParty><cac:Party><cac:PartyName>
   <cbc:Name>&xxe;</cbc:Name></cac:PartyName></cac:Party></cac:AccountingSupplierParty>
</Invoice>""".encode()
    try:
        meta = ai.parse_invoice_xml(hostile)
    except Exception:  # noqa: BLE001 — refusing to parse is an acceptable outcome
        return
    assert "LEAKED" not in (meta["supplier_name"] or "")


def test_db_dedup_by_download_id_and_hash():
    db_path = Path(_TMP) / "invoices.db"
    if db_path.exists():
        db_path.unlink()
    conn = ai.connect_db()
    content_hash = hashlib.sha256(SAMPLE_INVOICE).hexdigest()
    conn.execute(
        "INSERT INTO invoices (download_id, invoice_id, xml_sha256) VALUES (?,?,?)",
        ("1001", "FAC-2026-001", content_hash),
    )
    conn.commit()

    assert ai.db_has_download(conn, "1001") is True
    assert ai.db_has_download(conn, "9999") is False
    assert ai.db_has_hash(conn, content_hash) is True
    assert ai.db_has_hash(conn, "deadbeef") is False

    # Duplicate logging.
    ai.log_duplicate(conn, "1001", "FAC-2026-001", "already downloaded")
    n = conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
    assert n == 1

    # sync_state round-trip.
    ai.set_state(conn, "last_run", "2026-06-18T12:00:00")
    assert ai.get_state(conn, "last_run") == "2026-06-18T12:00:00"
    conn.close()


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    raise SystemExit(1 if failed else 0)
