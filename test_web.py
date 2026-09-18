"""WP2: local web UI (Flask) over the WP1 engine.

Contract pinned here:
  - three pages: / (Acasă), /facturi, /setari; Romanian by default, English toggle;
  - every POST needs the per-run CSRF token; a foreign Origin is refused; only
    localhost Host headers are served (DNS-rebinding guard);
  - the client secret is never rendered back;
  - PDFs are served by download_id (DB lookup), never by a client-supplied path;
  - sync runs on a background thread, one at a time, streamed as SSE events;
  - quit / open-folder go through injectable callables so tests never touch the OS.
"""

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_web_"))

import pytest  # noqa: E402

from efactura_sync import core  # noqa: E402
from efactura_sync.web import create_app  # noqa: E402
from conftest import FakeResp, two_invoices  # noqa: E402

# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def test_home_without_config_redirects_to_wizard(harness):
    r = harness.client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/start")


# --------------------------------------------------------------------------- #
# WP3: first-run wizard
# --------------------------------------------------------------------------- #

def test_wizard_step1_explains_and_links_anaf(harness):
    html = harness.client.get("/start").get_data(as_text=True)
    assert "anaf.ro" in html                       # where to register the OAuth app
    assert "/start?step=2" in html                 # next step
    assert "certificat" in html.lower()            # what you need


def test_wizard_step2_is_the_settings_form_leading_to_step3(harness):
    html = harness.client.get("/start?step=2").get_data(as_text=True)
    assert 'name="client_id"' in html and 'name="cif"' in html
    assert 'value="/start?step=3"' in html         # hidden next
    r = harness.post("/setari", client_id="cid", client_secret="s", cif="RO1",
                     environment="test", redirect_uri="https://localhost/callback",
                     base_dir=str(harness.tmp / "inv"), next="/start?step=3")
    assert r.status_code == 303 and r.headers["Location"].endswith("/start?step=3")


def test_wizard_step3_needs_config_then_shows_auth(harness):
    r = harness.client.get("/start?step=3")
    assert r.status_code == 302 and r.headers["Location"].endswith("/start?step=2")
    harness.write_cfg()
    html = harness.client.get("/start?step=3").get_data(as_text=True)
    assert "Începe autentificarea" in html


def test_settings_next_must_be_local_path(harness):
    harness.write_cfg()
    r = harness.post("/setari", client_id="cid", client_secret="", cif="1",
                     environment="test", redirect_uri="x", base_dir=str(harness.tmp / "inv"),
                     next="https://evil.example/phish")
    assert r.status_code == 303 and "evil" not in r.headers["Location"]


# --------------------------------------------------------------------------- #
# WP3: no English internals reach the user
# --------------------------------------------------------------------------- #

def _engine_with(*events):
    def engine(cfg):
        yield core.SyncStarted("test", "1", 60)
        yield from events
        yield core.SyncFinished(0, 0, 0, 1, "x")
    return engine


def test_events_carry_localized_text(harness):
    harness.write_cfg()
    harness.app.runner.engine = _engine_with(
        core.SyncError("", "Not authenticated. Run `auth` first.", "not_authenticated"))
    events = harness.synced()
    err = [e for e in events if e["type"] == "SyncError"][0]
    assert "Setări" in err["text"] and "Run `auth`" not in err["text"]
    # the same line is what the page renders after reload
    html = harness.client.get("/").get_data(as_text=True)
    assert err["text"] in html and "Run `auth`" not in html


def test_events_text_follows_language(harness):
    harness.write_cfg()
    harness.post("/limba", lang="en")
    harness.app.runner.engine = _engine_with(
        core.SyncError("", "Not authenticated. Run `auth` first.", "not_authenticated"))
    err = [e for e in harness.synced() if e["type"] == "SyncError"][0]
    assert "Settings" in err["text"]


def test_unknown_error_is_generic_but_keeps_detail(harness):
    harness.write_cfg()
    harness.app.runner.engine = _engine_with(core.SyncError("1001", "boom", "unknown"))
    err = [e for e in harness.synced() if e["type"] == "SyncError"][0]
    assert "boom" not in err["text"] and "neașteptat" in err["text"]
    assert err["message"] == "boom"                # kept for technical details


def test_notice_is_localized(harness):
    harness.write_cfg()
    harness.app.runner.engine = _engine_with(
        core.Notice("Paginated listing unavailable (x); using legacy endpoint.", "legacy_listing"))
    ev = [e for e in harness.synced() if e["type"] == "Notice"][0]
    assert ev["text"] == ""                       # internal notice: kept in events, hidden from users


def test_settings_validation_errors_are_friendly(harness):
    harness.write_cfg()
    r = harness.post("/setari", client_id="", client_secret="", cif="abc",
                     environment="test", redirect_uri="https://localhost/callback",
                     base_dir=str(harness.tmp / "inv"))
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert 'name="cif"' in html                    # form re-rendered, not a bare error
    assert "doar cifre" in html                    # cif message (ro)
    assert "obligatoriu" in html                   # client_id required (ro)


def test_auth_error_is_localized_with_technical_detail(harness):
    harness.write_cfg()
    harness.post("/auth/begin")
    pending = harness.app.pending_auth
    html = harness.post(
        "/auth/complete",
        pasted=f"https://localhost/callback?error=access_denied&state={pending.state}",
    ).get_data(as_text=True)
    assert "ANAF a refuzat accesul" in html            # the localized headline itself
    assert "Check the certificate side" not in html   # the English hint must not leak
    assert "access_denied" in html                     # technical detail still available


def test_home_renders_status_in_romanian(harness):
    harness.write_cfg()
    html = harness.client.get("/").get_data(as_text=True)
    assert "Sincronizează" in html and "Facturi" in html and "Setări" in html
    assert 'name="csrf"' in html


def test_language_toggle_persists_and_switches_copy(harness):
    harness.write_cfg()
    assert harness.post("/limba", lang="en").status_code == 303
    html = harness.client.get("/").get_data(as_text=True)
    assert "Synchronize" in html and "Sincronizează" not in html
    assert json.loads((harness.tmp / "config.json").read_text())["language"] == "en"


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #

def test_post_without_csrf_is_refused(harness):
    harness.write_cfg()
    assert harness.client.post("/sync", data={}).status_code == 403
    assert harness.client.post("/sync", data={"csrf": "wrong"}).status_code == 403


def test_foreign_origin_is_refused_even_with_token(harness):
    harness.write_cfg()
    r = harness.client.post("/sync", data={"csrf": harness.csrf},
                            headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = harness.client.post("/limba", data={"csrf": harness.csrf, "lang": "en"},
                            headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 303


def test_non_local_host_header_is_refused(harness):
    harness.write_cfg()
    r = harness.client.get("/", headers={"Host": "attacker.example"})
    assert r.status_code == 403


def test_settings_never_echo_the_client_secret(harness):
    harness.write_cfg()
    html = harness.client.get("/setari").get_data(as_text=True)
    assert "topsecret-value" not in html
    assert 'name="client_secret"' in html


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def test_settings_post_writes_config_keeps_secret_when_blank(harness):
    harness.write_cfg()
    r = harness.post("/setari", client_id="cid2", client_secret="", cif="RO60000000",
                     environment="prod", redirect_uri="https://localhost/callback",
                     base_dir=str(harness.tmp / "other"))
    assert r.status_code == 303
    saved = json.loads((harness.tmp / "config.json").read_text())
    assert saved["client_id"] == "cid2"
    assert saved["client_secret"] == "topsecret-value"   # blank means keep
    assert saved["firms"][0]["cif"] == "60000000"        # RO prefix stripped; WP7a: on the firm
    assert saved["environment"] == "prod"
    import sys
    if sys.platform != "win32":                    # POSIX file modes only
        assert oct((harness.tmp / "config.json").stat().st_mode & 0o777) == "0o600"


def test_settings_post_replaces_secret_when_given(harness):
    harness.write_cfg()
    harness.post("/setari", client_id="cid", client_secret="newsecret", cif="1",
                 environment="test", redirect_uri="https://localhost/callback",
                 base_dir=str(harness.tmp / "inv"))
    assert json.loads((harness.tmp / "config.json").read_text())["client_secret"] == "newsecret"


def test_settings_rejects_bad_environment(harness):
    harness.write_cfg()
    r = harness.post("/setari", client_id="cid", client_secret="", cif="1",
                     environment="staging", redirect_uri="x", base_dir="y")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Sync + events
# --------------------------------------------------------------------------- #

def test_sync_runs_in_background_and_streams_events(harness):
    harness.write_cfg()
    events = harness.synced()
    types = [e["type"] for e in events]
    assert types[0] == "SyncStarted" and types[-1] == "SyncFinished"
    assert types.count("InvoiceDone") == 2
    assert events[-1]["new"] == 2


def test_second_sync_start_while_running_is_refused(harness, monkeypatch):
    harness.write_cfg()
    import threading
    gate = threading.Event()

    def slow_engine(cfg):
        yield core.SyncStarted("test", "1", 60)
        gate.wait(5)
        yield core.SyncFinished(0, 0, 0, 0, "x")

    harness.app.runner.engine = slow_engine
    assert harness.post("/sync").status_code == 303
    r = harness.post("/sync")                       # double-click: flash, not a bare page
    assert r.status_code == 303
    assert "deja în curs" in harness.client.get("/").get_data(as_text=True)
    gate.set()
    assert harness.app.runner.wait(5)


def test_engine_crash_is_reported_as_event_not_500(harness):
    harness.write_cfg()

    def crashing_engine(cfg):
        yield core.SyncStarted("test", "1", 60)
        raise RuntimeError("token expired")

    harness.app.runner.engine = crashing_engine
    events = harness.synced()
    assert events[-1]["type"] == "SyncError" and "token expired" in events[-1]["message"]


def test_home_shows_recent_invoices_after_sync(harness):
    harness.write_cfg()
    harness.synced()
    html = harness.client.get("/").get_data(as_text=True)
    assert "FAC-2026-001" in html and "Furnizor Demo SRL" in html


# --------------------------------------------------------------------------- #
# Invoices table + PDF
# --------------------------------------------------------------------------- #

def test_invoices_table_lists_and_filters(harness):
    harness.write_cfg()
    harness.synced()
    html = harness.client.get("/facturi").get_data(as_text=True)
    assert "FAC-2026-001" in html and "CN-2026-009" in html

    html = harness.client.get("/facturi?luna=2026-03").get_data(as_text=True)
    assert "FAC-2026-001" in html and "CN-2026-009" not in html

    html = harness.client.get("/facturi?furnizor=alt").get_data(as_text=True)
    assert "CN-2026-009" in html and "FAC-2026-001" not in html


def test_pdf_served_by_download_id_only(harness):
    harness.write_cfg()
    harness.synced()
    r = harness.client.get("/pdf/1001")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert r.headers["Content-Type"] == "application/pdf"
    assert harness.client.get("/pdf/nope").status_code == 404
    assert harness.client.get("/pdf/../../etc/passwd").status_code == 404


def test_open_folder_uses_injected_opener(harness):
    harness.write_cfg()
    assert harness.post("/deschide-dosar").status_code == 303
    assert harness.calls["opened"] == [str(harness.tmp / "inv")]


# --------------------------------------------------------------------------- #
# Authentication via the UI
# --------------------------------------------------------------------------- #

def test_auth_begin_shows_url_and_complete_stores_tokens(harness, monkeypatch):
    harness.write_cfg()
    posted = {}

    def fake_post(url, data, timeout, headers):
        posted.update(data)
        return FakeResp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    monkeypatch.setattr(core.requests, "post", fake_post)

    html = harness.post("/auth/begin").get_data(as_text=True)
    assert core.AUTH_URL in html and 'name="pasted"' in html
    pending = harness.app.pending_auth
    assert pending is not None

    r = harness.post("/auth/complete",
                     pasted=f"https://localhost/callback?code=C1&state={pending.state}")
    assert r.status_code == 303
    assert posted["code"] == "C1"
    assert (harness.tmp / "tokens.json").exists()
    assert harness.app.pending_auth is None


def test_auth_complete_renders_anaf_error_hint(harness):
    harness.write_cfg()
    harness.post("/auth/begin")
    pending = harness.app.pending_auth
    html = harness.post(
        "/auth/complete",
        pasted=f"https://localhost/callback?error=access_denied&state={pending.state}",
    ).get_data(as_text=True)
    assert "access_denied" in html and "ANAF a refuzat accesul" in html


def test_settings_shows_auth_state(harness):
    harness.write_cfg()
    html = harness.client.get("/setari").get_data(as_text=True)
    assert "Neautentificat" in html
    core.save_tokens({"access_token": "a", "refresh_token": "r", "expires_at": 4102444800,
                      "obtained_at": 0, "token_type": "Bearer"})
    html = harness.client.get("/setari").get_data(as_text=True)
    assert "Autentificat" in html and "Neautentificat" not in html


# --------------------------------------------------------------------------- #
# Quit + CLI entry point
# --------------------------------------------------------------------------- #

def test_quit_calls_injected_shutdown(harness):
    harness.write_cfg()
    assert harness.post("/iesire").status_code == 200
    assert harness.calls["shutdown"] == 1


def test_cli_ui_starts_without_config_so_the_wizard_can_run(harness, monkeypatch):
    """Found live (2026-09-17): `ui` exited 2 on a fresh machine, before the wizard
    could ever be shown. The UI must start with no config."""
    from efactura_sync import cli, web
    assert not (harness.tmp / "config.json").exists()
    seen = {}
    monkeypatch.setattr(web, "run_ui", lambda **kw: seen.setdefault("called", True))
    assert cli.main(["ui"]) == 0
    assert seen["called"]


def test_cli_ui_passes_browser_flag_through(harness, monkeypatch):
    harness.write_cfg()
    from efactura_sync import cli, web
    seen = []
    monkeypatch.setattr(web, "run_ui", lambda **kw: seen.append(kw.get("open_browser")))
    assert cli.main(["ui"]) == 0
    assert cli.main(["ui", "--no-browser"]) == 0
    assert seen == [True, False]


def test_cli_ui_port_flag(harness, monkeypatch):
    from efactura_sync import cli, web
    seen = []
    monkeypatch.setattr(web, "run_ui", lambda **kw: seen.append(kw.get("port")))
    assert cli.main(["ui", "--no-browser", "--port", "8790"]) == 0
    assert cli.main(["ui", "--no-browser"]) == 0
    assert seen == [8790, None]


# --------------------------------------------------------------------------- #
# Settings form ergonomics (design feedback 2026-09-17): sensible defaults,
# advanced section, native folder picker, no browser autofill of stale values.
# --------------------------------------------------------------------------- #

def test_form_defaults_are_sensible_on_first_run(harness):
    html = harness.client.get("/start?step=2").get_data(as_text=True)
    assert "Facturi e-Factura" in html                          # default folder prefilled
    assert 'value="prod" selected' in html                      # Producție by default
    assert 'value="https://localhost/callback"' in html         # callback prefilled
    assert "Setări avansate" in html                            # env + callback tucked away
    assert html.index("Setări avansate") > html.index('name="base_dir"')


def test_form_disables_browser_autofill(harness):
    harness.write_cfg()
    html = harness.client.get("/setari").get_data(as_text=True)
    assert 'autocomplete="off"' in html
    assert 'name="client_secret"' in html and 'autocomplete="new-password"' in html


def test_pick_folder_uses_injected_picker_and_returns_json(harness):
    harness.write_cfg()
    harness.calls["pick_result"] = "/Users/someone/Documents/Facturi"
    r = harness.post("/alege-dosar", initial="/Users/someone")
    assert r.status_code == 200 and r.get_json() == {"path": "/Users/someone/Documents/Facturi"}
    assert harness.calls["pick_initial"] == "/Users/someone"


def test_pick_folder_cancel_returns_null(harness):
    harness.write_cfg()
    harness.calls["pick_result"] = None
    assert harness.post("/alege-dosar").get_json() == {"path": None}


def test_pick_folder_requires_csrf(harness):
    harness.write_cfg()
    assert harness.client.post("/alege-dosar", data={}).status_code == 403


# --------------------------------------------------------------------------- #
# Table/log polish (design feedback 2026-09-17)
# --------------------------------------------------------------------------- #

def test_log_hidden_until_there_is_something_to_show(harness):
    harness.write_cfg()
    html = harness.client.get("/").get_data(as_text=True)
    assert 'id="log"' not in html and ">Jurnal<" not in html
    harness.synced()
    html = harness.client.get("/").get_data(as_text=True)
    assert 'id="log"' in html


def test_invoice_table_has_no_type_column_but_flags_credit_notes(harness):
    harness.write_cfg()
    harness.synced()                                  # one invoice + one credit note
    html = harness.client.get("/facturi").get_data(as_text=True)
    assert "<th>Tip</th>" not in html
    assert html.count('class="badge"') == 1 and ">storno<" in html
    assert "factură</" not in html                    # no per-row "invoice" noise


def test_invoice_dates_do_not_wrap(harness):
    harness.write_cfg()
    harness.synced()
    html = harness.client.get("/facturi").get_data(as_text=True)
    assert '<td class="nowrap">14.03.2026</td>' in html      # Romanian date format (decision 2026-09-17)
    assert html.count('<td class="nowrap">') == 3 * 2          # date, invoice, action × 2 rows


def test_home_has_no_environment_card_but_flags_test_mode(harness):
    harness.write_cfg(environment="prod")
    html = harness.client.get("/").get_data(as_text=True)
    assert "· CIF" not in html and ">Mediu<" not in html and "Mediu de test" not in html
    harness.write_cfg(environment="test")
    html = harness.client.get("/").get_data(as_text=True)
    assert "Mediu de test" in html and "· CIF" not in html


def test_supplier_cif_is_on_its_own_line(harness):
    harness.write_cfg()
    harness.synced()
    html = harness.client.get("/facturi").get_data(as_text=True)
    assert 'Furnizor Demo SRL<div class="muted cif">CUI RO87654321</div>' in html
