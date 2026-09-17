"""WP1: the sync engine reports progress as typed events instead of printing.

These tests drive core.sync() end-to-end against a fake ANAF (api_get and
xml_to_pdf are monkeypatched), so the real filing/DB/CSV paths run while no
network is touched. They also pin the WP1 contract: no print() in core, auth
split into begin/complete, status as data, and the anaf_invoices.py shim.
"""

import csv
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_ev_"))

import pytest  # noqa: E402

from efactura_sync import core  # noqa: E402
from conftest import (  # noqa: E402
    SAMPLE_INVOICE, FakeAnaf, FakeResp, make_zip, msg, two_invoices,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + base_dir; returns a runner that yields the event list."""
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "invoices.db")
    cfg = {"environment": "test", "cif": "1", "base_dir": str(tmp_path / "inv")}

    def run(fake, pdf=lambda xml, standard: b"%PDF-fake"):
        monkeypatch.setattr(core, "api_get", fake.api_get)
        monkeypatch.setattr(core, "xml_to_pdf", pdf)
        return list(core.sync(cfg))

    run.cfg, run.base = cfg, tmp_path / "inv"
    return run


def of(events, cls):
    return [e for e in events if isinstance(e, cls)]


# --------------------------------------------------------------------------- #
# sync() event contract
# --------------------------------------------------------------------------- #

def test_new_invoices_are_filed_and_reported(env):
    events = env(two_invoices())

    assert isinstance(events[0], core.SyncStarted)
    assert isinstance(events[-1], core.SyncFinished)
    fin = events[-1]
    assert (fin.new, fin.duplicates, fin.pdf_failed, fin.errors) == (2, 0, 0, 0)

    done = of(events, core.InvoiceDone)
    assert {d.invoice_id for d in done} == {"FAC-2026-001", "CN-2026-009"}
    assert all(d.pdf_ok for d in done)

    pdf = env.base / "2026" / "03" / "FAC-2026-001_Furnizor_Demo_SRL_2026-03-14.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    assert pdf.with_suffix(".xml").exists() and pdf.with_suffix(".zip").exists()
    with (env.base / "invoices.csv").open(newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 2


def test_second_run_reports_duplicates_and_downloads_nothing(env):
    env(two_invoices())
    fake = two_invoices()
    events = env(fake)

    fin = events[-1]
    assert (fin.new, fin.duplicates) == (0, 2)
    assert {d.download_id for d in of(events, core.Duplicate)} == {"1001", "1002"}
    assert not [c for c in fake.calls if c[0] == "descarcare"]


def test_pdf_failure_keeps_xml_and_flags_invoice(env):
    def broken_pdf(xml, standard):
        raise RuntimeError("PDF conversion failed: schema error")

    fake = FakeAnaf([msg("1001")], {"1001": make_zip(SAMPLE_INVOICE)})
    events = env(fake, pdf=broken_pdf)

    failed = of(events, core.PdfFailed)
    assert len(failed) == 1 and failed[0].invoice_id == "FAC-2026-001"
    assert "schema error" in failed[0].message
    done = of(events, core.InvoiceDone)
    assert len(done) == 1 and done[0].pdf_ok is False
    assert events[-1].pdf_failed == 1 and events[-1].new == 1

    xml = env.base / "2026" / "03" / "FAC-2026-001_Furnizor_Demo_SRL_2026-03-14.xml"
    assert xml.exists() and not xml.with_suffix(".pdf").exists()
    conn = core.connect_db()
    assert conn.execute("SELECT pdf_ok FROM invoices").fetchone()[0] == 0


def test_download_error_is_reported_and_sync_continues(env):
    fake = two_invoices()
    fake.fail_download = {"1001"}
    events = env(fake)

    errs = of(events, core.SyncError)
    assert len(errs) == 1 and errs[0].download_id == "1001"
    assert "connection reset" in errs[0].message
    assert (events[-1].errors, events[-1].new) == (1, 1)


def test_non_invoice_messages_are_skipped(env):
    fake = FakeAnaf([msg("1001", tip="MESAJ CUMPARATOR")], {})
    events = env(fake)
    assert not of(events, core.InvoiceDone)
    assert not [c for c in fake.calls if c[0] == "descarcare"]
    assert events[-1].new == 0


def test_legacy_listing_fallback_emits_notice(env):
    fake = FakeAnaf([msg("1001")], {"1001": make_zip(SAMPLE_INVOICE)}, paginated_ok=False)
    events = env(fake)

    notices = of(events, core.Notice)
    assert len(notices) == 1 and "legacy" in notices[0].message.lower()
    assert [c[0] for c in fake.calls][:2] == ["listaMesajePaginatieFactura", "listaMesajeFactura"]
    assert events[-1].new == 1


def test_sync_records_last_run(env):
    env(two_invoices())
    assert core.get_state(core.connect_db(), "last_run")


def test_core_has_no_print_calls():
    """WP1 rule: core reports via events; only cli.py prints."""
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*print\(", src, re.MULTILINE)
    assert "input(" not in src


# --------------------------------------------------------------------------- #
# auth split
# --------------------------------------------------------------------------- #

def test_begin_and_complete_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "TOKENS_PATH", tmp_path / "tokens.json")
    cfg = {"client_id": "cid", "client_secret": "sec", "redirect_uri": "https://localhost/callback"}
    posted = {}

    def fake_post(url, data, timeout, headers):
        posted.update(data)
        return FakeResp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    monkeypatch.setattr(core.requests, "post", fake_post)

    pending = core.begin_auth(cfg)
    assert pending.url.startswith(core.AUTH_URL) and pending.state in pending.url

    tokens = core.complete_auth(cfg, pending,
                                f"https://localhost/callback?code=THECODE&state={pending.state}")
    assert posted["code"] == "THECODE" and posted["code_verifier"] == pending.code_verifier
    assert tokens["access_token"] == "AT"
    assert (tmp_path / "tokens.json").exists()


def test_complete_auth_rejects_state_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "TOKENS_PATH", tmp_path / "tokens.json")
    cfg = {"client_id": "cid", "client_secret": "sec", "redirect_uri": "https://localhost/callback"}
    pending = core.begin_auth(cfg)
    with pytest.raises(core.ConfigError, match="State mismatch"):
        core.complete_auth(cfg, pending, "https://localhost/callback?code=X&state=WRONG")


def test_complete_auth_surfaces_anaf_error(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "TOKENS_PATH", tmp_path / "tokens.json")
    cfg = {"client_id": "cid", "client_secret": "sec", "redirect_uri": "https://localhost/callback"}
    pending = core.begin_auth(cfg)
    with pytest.raises(core.ConfigError, match="access_denied"):
        core.complete_auth(cfg, pending,
                           f"https://localhost/callback?error=access_denied&state={pending.state}")


# --------------------------------------------------------------------------- #
# status as data, and the CLI shim
# --------------------------------------------------------------------------- #

def test_status_report_is_data(env):
    env(two_invoices())
    report = core.status_report(env.cfg)
    assert report.total == 2 and report.pdf_failed == 0
    assert report.last_run
    assert {r.invoice_id for r in report.recent} == {"FAC-2026-001", "CN-2026-009"}


def test_shim_keeps_old_entry_point_working():
    import anaf_invoices
    from efactura_sync import cli
    assert anaf_invoices.main is cli.main
