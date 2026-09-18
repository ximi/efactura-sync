"""The three UX items deferred from the 2026-09-17 review (done 2026-09-18)."""

import os
import subprocess
import tempfile

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_ux_"))

import pytest  # noqa: E402

from efactura_sync import core, web as webmod  # noqa: E402


# 1. localized native picker prompts --------------------------------------------

def test_picker_receives_the_localized_prompt(harness):
    harness.write_cfg()
    harness.post("/alege-dosar")
    assert harness.calls["pick_prompt"] == "Alege dosarul pentru facturi"
    harness.post("/limba", lang="en")
    harness.post("/alege-dosar")
    assert harness.calls["pick_prompt"] == "Choose the invoices folder"


def test_native_pickers_use_the_prompt(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/x\n", stderr="")

    monkeypatch.setattr(webmod.subprocess, "run", fake_run)
    monkeypatch.setattr(webmod.sys, "platform", "darwin")
    webmod.pick_folder_default("/tmp", prompt='Choose "the" folder')
    assert 'Choose \\"the\\" folder' in " ".join(seen["argv"])          # escaped inside AppleScript
    monkeypatch.setattr(webmod.sys, "platform", "win32")
    webmod.pick_folder_default("C:/x", prompt="Alege 'dosarul'")
    assert "$d.Description = 'Alege ''dosarul'''" in " ".join(seen["argv"])   # escaped for PowerShell


# 2. copy the ANAF address ---------------------------------------------------------

def test_auth_url_has_a_copy_button(harness):
    harness.write_cfg()
    html = harness.post("/auth/begin").get_data(as_text=True)
    assert 'id="copy-url"' in html and "Copiază adresa" in html and "navigator.clipboard" in html


# 3. retry the PDF ----------------------------------------------------------------

def _sync_without_pdf(harness, monkeypatch):
    harness.write_cfg()
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, s: (_ for _ in ()).throw(RuntimeError("PDF conversion failed: x")))
    harness.synced()
    return core.load_config()["firm_id"]


def test_retry_pdf_for_one_invoice(harness, monkeypatch):
    fid = _sync_without_pdf(harness, monkeypatch)
    assert core.invoice_by_download_id(fid, "1001")["pdf_ok"] == 0
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, s: b"%PDF-retry")
    r = harness.post("/pdf/1001/reincearca")
    assert r.status_code == 303
    row = core.invoice_by_download_id(fid, "1001")
    assert row["pdf_ok"] == 1 and row["pdf_path"].endswith(".pdf")
    assert open(row["pdf_path"], "rb").read() == b"%PDF-retry"
    assert harness.client.get("/pdf/1001").status_code == 200
    assert harness.post("/pdf/nope/reincearca").status_code == 404


def test_retry_pdf_reports_failure_without_500(harness, monkeypatch):
    fid = _sync_without_pdf(harness, monkeypatch)
    r = harness.client.post("/pdf/1001/reincearca", data={"csrf": harness.csrf}, follow_redirects=True)
    assert r.status_code == 200 and "nu a putut genera" in r.get_data(as_text=True)
    assert core.invoice_by_download_id(fid, "1001")["pdf_ok"] == 0


def test_missing_pdf_filter_retry_all_and_home_link(harness, monkeypatch):
    fid = _sync_without_pdf(harness, monkeypatch)
    home = harness.client.get("/").get_data(as_text=True)
    assert 'href="/facturi?fara_pdf=1"' in home
    html = harness.client.get("/facturi?fara_pdf=1").get_data(as_text=True)
    assert "FAC-2026-001" in html and "CN-2026-009" in html
    assert 'action="/pdf/reincearca-toate"' in html and 'action="/pdf/1001/reincearca"' in html
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, s: b"%PDF-all")
    assert harness.post("/pdf/reincearca-toate").status_code == 303
    assert core.list_invoices(fid, missing_pdf=True) == []
    assert "Nicio factură nu corespunde" in harness.client.get("/facturi?fara_pdf=1").get_data(as_text=True)


def test_retry_pdf_core_updates_csv_row(harness, monkeypatch):
    fid = _sync_without_pdf(harness, monkeypatch)
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, s: b"%PDF-csv")
    ok, msg = core.retry_pdf(fid, "1001")
    assert ok and msg == ""
    import csv
    with open(core.load_config()["base_dir"] + "/invoices.csv", newline="", encoding="utf-8") as fh:
        rows = {r["download_id"]: r for r in csv.DictReader(fh)}
    assert rows["1001"]["pdf_ok"] == "1" and rows["1001"]["pdf_path"].endswith(".pdf")
    assert rows["1002"]["pdf_ok"] == "0"
