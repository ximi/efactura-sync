"""Batch A of the 2026-09-17 review: correctness fixes, each pinned by a test that
drives the real path. Fixtures `env` (engine) and `harness` (web) live in conftest.
"""

import dataclasses
import inspect
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_rf_"))

import pytest  # noqa: E402
import requests  # noqa: E402

from efactura_sync import core, web as webmod  # noqa: E402
from efactura_sync.web import errors as weberrors  # noqa: E402
from conftest import SAMPLE_INVOICE, FakeAnaf, FakeResp, make_zip, msg, two_invoices  # noqa: E402

DATELESS = SAMPLE_INVOICE.replace(b"<cbc:IssueDate>2026-03-14</cbc:IssueDate>", b"")


def of(events, cls):
    return [e for e in events if isinstance(e, cls)]


# 1. dates -------------------------------------------------------------------

def test_message_date_accepts_anaf_and_iso_formats():
    assert core.parse_message_date("202512310900").strftime("%Y-%m-%d") == "2025-12-31"
    assert core.parse_message_date("2025-12-31T09:00:00").strftime("%Y-%m-%d") == "2025-12-31"
    assert core.parse_message_date("garbage") is None and core.parse_message_date("") is None


def test_dateless_invoice_files_under_message_month_not_today(tmp_path):
    meta = core.parse_invoice_xml(DATELESS)
    assert meta["issue_date"] is None
    directory, basename = core.build_target_paths(tmp_path, meta, "202512310900")
    assert directory == tmp_path / "2025" / "12" and basename.endswith("2025-12-31")


def test_invoice_with_no_date_anywhere_is_flagged(env):
    fake = FakeAnaf([{"id": "1", "tip": "FACTURA", "data_creare": ""}], {"1": make_zip(DATELESS)})
    events = env(fake)
    assert [n.code for n in of(events, core.Notice)] == ["date_unknown"]
    assert events[-1].new == 1


# 2. filename collisions ----------------------------------------------------

def test_colliding_basenames_get_download_id_suffix(env):
    no_id = SAMPLE_INVOICE.replace(b"<cbc:ID>FAC-2026-001</cbc:ID>", b"")
    other = no_id.replace(b"RON", b"EUR")                       # different content/hash
    fake = FakeAnaf([msg("1001"), msg("2002")], {"1001": make_zip(no_id), "2002": make_zip(other)})
    events = env(fake)
    done = of(events, core.InvoiceDone)
    assert len(done) == 2 and done[0].pdf_path != done[1].pdf_path
    assert "2002" in Path(done[1].pdf_path).name
    assert len(list((env.base / "2026" / "03").glob("*.pdf"))) == 2


# 3. hash duplicates --------------------------------------------------------

def test_hash_duplicate_is_not_redownloaded_on_the_next_run(env):
    same = {"1001": make_zip(SAMPLE_INVOICE), "2002": make_zip(SAMPLE_INVOICE)}
    env(FakeAnaf([msg("1001"), msg("2002")], same))
    fake = FakeAnaf([msg("1001"), msg("2002")], same)
    events = env(fake)
    assert not [c for c in fake.calls if c[0] == "descarcare"]
    assert events[-1].duplicates == 2 and events[-1].new == 0


# 4. corrupt config ---------------------------------------------------------

def test_corrupt_config_is_a_config_error_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(core, "TOKENS_PATH", tmp_path / "tokens.json")
    (tmp_path / "config.json").write_text("{ oops")
    (tmp_path / "tokens.json").write_text("nope")
    with pytest.raises(core.ConfigError) as ei:
        core.load_config()
    assert ei.value.code == "bad_config"
    assert core.read_config_raw() == {} and core.config_problem() == "bad_config"
    assert core.load_tokens() is None


def test_corrupt_config_pages_still_render(harness):
    (harness.tmp / "config.json").write_text("{ oops")
    assert harness.client.get("/").status_code == 302
    r = harness.client.get("/setari")
    assert r.status_code == 200 and "corupt" in r.get_data(as_text=True).lower()
    assert harness.client.get("/start?step=2").status_code == 200


def test_cli_reports_corrupt_config_cleanly(harness, capsys):
    (harness.tmp / "config.json").write_text("{ oops")
    from efactura_sync import cli
    assert cli.main(["status"]) == 2
    assert "Error:" in capsys.readouterr().err


# 5. base_dir validation ----------------------------------------------------

def test_settings_rejects_empty_or_relative_folder(harness):
    harness.write_cfg()
    for bad in ("", "Facturi"):
        r = harness.post("/setari", client_id="cid", client_secret="", cif="1",
                         environment="prod", redirect_uri="x", base_dir=bad)
        assert r.status_code == 400 and "dosar" in r.get_data(as_text=True).lower()


def test_load_config_replaces_empty_base_dir_with_default(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps(
        {"client_id": "c", "client_secret": "s", "cif": "1", "base_dir": ""}))
    assert core.load_config()["base_dir"] == str(core.DEFAULT_BASE_DIR)


# 6. idle watchdog ----------------------------------------------------------

def test_idle_watchdog_gives_grace_after_a_run_and_counts_streams(harness):
    app, now = harness.app, 1_000_000.0
    app.last_activity = now - 3600
    app.runner.finished_at = now - 10
    assert webmod._idle_expired(app, now, 30) is False       # just finished: grace period
    app.runner.finished_at = now - 1000
    assert webmod._idle_expired(app, now, 30) is True
    harness.write_cfg()
    harness.synced()
    app.last_activity = 0
    list(app.runner.stream(0, touch=lambda: setattr(app, "last_activity", 42)))
    assert app.last_activity == 42                            # streaming is activity


# 7/8. api_get, xml_to_pdf, pagination, classification -----------------------

def test_api_get_refreshes_exactly_once_after_401(monkeypatch):
    refreshes = []
    monkeypatch.setattr(core, "get_access_token",
                        lambda cfg, force_refresh=False: refreshes.append(force_refresh) or "T")
    seq = iter([FakeResp(status_code=401), FakeResp(status_code=503), FakeResp(status_code=200)])
    monkeypatch.setattr(core.requests, "get", lambda url, **kw: next(seq))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core.api_get({"environment": "test"}, "x", {}).status_code == 200
    assert refreshes.count(True) == 1


def test_api_get_exhausted_retries_is_an_anaf_http_error(monkeypatch):
    monkeypatch.setattr(core, "get_access_token", lambda cfg, force_refresh=False: "T")
    monkeypatch.setattr(core.requests, "get", lambda url, **kw: FakeResp(status_code=503))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        core.api_get({"environment": "test"}, "x", {})
    assert core.error_code(ei.value) == "anaf_http"


def test_xml_to_pdf_retries_without_validation_then_reports(monkeypatch):
    err = lambda: FakeResp(json_data={"stare": "nok", "Messages": [{"message": "schema"}]},
                           headers={"Content-Type": "application/json"})
    ok = FakeResp(content=b"%PDF-x", headers={"Content-Type": "application/pdf"})
    seq, urls = [err(), ok], []
    monkeypatch.setattr(core.requests, "post",
                        lambda url, data, headers, timeout: (urls.append(url), seq.pop(0))[1])
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core.xml_to_pdf(b"<x/>", "FACT1") == b"%PDF-x"
    assert urls[0].endswith("/FACT1") and urls[1].endswith("/FACT1/DA")
    seq[:] = [err(), err()]
    with pytest.raises(RuntimeError, match="schema"):
        core.xml_to_pdf(b"<x/>", "FACT1")


def test_paginated_listing_walks_all_pages(env):
    class Paged(FakeAnaf):
        def api_get(self, cfg, path, params):
            self.calls.append((path, dict(params)))
            if path == "listaMesajePaginatieFactura":
                page = params["pagina"]
                return FakeResp({"mesaje": [msg("1001")] if page == 1 else [msg("1002")],
                                 "numar_total_pagini": 2})
            return FakeResp(content=self.zips[params["id"]])

    fake = Paged([], {"1001": make_zip(SAMPLE_INVOICE), "2002": b""})
    fake.zips["1002"] = make_zip(SAMPLE_INVOICE.replace(b"FAC-2026-001", b"FAC-2"))
    events = env(fake)
    assert [c[1]["pagina"] for c in fake.calls if c[0] == "listaMesajePaginatieFactura"] == [1, 2]
    assert events[-1].new == 2


def test_listing_http_error_still_falls_back_to_legacy(env):
    fake = two_invoices()
    real = fake.api_get

    def http_404(cfg, path, params):
        if path == "listaMesajePaginatieFactura":
            raise requests.exceptions.HTTPError("404", response=FakeResp(status_code=404))
        return real(cfg, path, params)

    fake.api_get = http_404
    events = env(fake)
    assert [n.code for n in of(events, core.Notice)] == ["legacy_listing"] and events[-1].new == 2


# 9. CSV locked -------------------------------------------------------------

def test_csv_failure_is_reported_as_locked_and_retried_next_run(env, monkeypatch):
    original = core.append_csv

    def locked(base_dir, row):
        raise PermissionError(13, "in use", str(base_dir / "invoices.csv"))

    monkeypatch.setattr(core, "append_csv", locked)
    events = env(two_invoices())
    errs = of(events, core.SyncError)
    assert errs and all(e.code == "csv_locked" for e in errs)
    assert core.connect_db().execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    monkeypatch.setattr(core, "append_csv", original)
    assert env(two_invoices())[-1].new == 2


# 10/11. launching ----------------------------------------------------------

def test_run_ui_reuses_an_instance_on_a_fallback_port(monkeypatch):
    monkeypatch.setattr(webmod, "_already_running", lambda port: port == 8767)
    opened = []
    monkeypatch.setattr(webmod.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(webmod, "_free_port", lambda p: (_ for _ in ()).throw(AssertionError("no server")))
    webmod.run_ui(port=8765)
    assert opened == ["http://127.0.0.1:8767/"]


def test_run_ui_logs_startup_and_failures(monkeypatch, caplog):
    monkeypatch.setattr(webmod, "_already_running", lambda p: False)

    class Srv:
        def __init__(self, *a, **k): pass
        def serve_forever(self): pass
        def shutdown(self): pass

    monkeypatch.setattr("werkzeug.serving.make_server", lambda *a, **k: Srv())
    monkeypatch.setattr(webmod, "_free_port", lambda p: p)
    with caplog.at_level(logging.INFO):
        webmod.run_ui(port=8799, open_browser=False)
    assert any("8799" in r.message and core.__name__.split(".")[0] in r.name for r in caplog.records)

    monkeypatch.setattr(webmod, "_free_port",
                        lambda p: (_ for _ in ()).throw(RuntimeError("No free local port found.")))
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        webmod.run_ui(port=8799, open_browser=False)
    assert any("No free local port" in r.message for r in caplog.records)


def test_run_ui_has_no_dead_cfg_parameter():
    assert "cfg" not in inspect.signature(webmod.run_ui).parameters


def test_frozen_logging_is_set_up_for_every_command(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(core, "setup_frozen_logging", lambda: seen.append(True))
    from efactura_sync import __main__ as entry, cli
    monkeypatch.setattr(cli, "cmd_ui", lambda **kw: None)
    assert entry.run([]) == 0 and seen == [True]


# 12. smaller ---------------------------------------------------------------

def test_sse_stream_is_bound_to_one_run(harness):
    harness.write_cfg()
    runner = harness.app.runner
    runner.start(harness.write_cfg()); assert runner.wait(5)
    gen = runner.stream(0)
    first = next(gen)
    run1_len = len(runner.events)
    runner.engine = lambda cfg: iter([core.SyncStarted("t", "1", 60)] * 20 + [core.SyncFinished(0, 0, 0, 0, "x")])
    runner.start(harness.write_cfg()); assert runner.wait(5)
    rest = list(gen)
    assert first["type"] == "SyncStarted" and len(rest) == run1_len - 1
    assert rest[-1]["type"] == "SyncFinished"


def test_non_ascii_csrf_is_403_not_500(harness):
    harness.write_cfg()
    assert harness.client.post("/limba", data={"csrf": "é", "lang": "en"}).status_code == 403


def test_ipv6_loopback_host_is_accepted(harness):
    harness.write_cfg()
    assert harness.client.get("/", headers={"Host": "[::1]:8765"}).status_code == 200


def test_schema_version_recorded_and_ddl_once_per_process(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "a.db")
    core._SCHEMA_READY.clear()
    core.connect_db().close()
    assert core.get_state(core.connect_db(), "schema_version") == "1"
    assert str(tmp_path / "a.db") in core._SCHEMA_READY


def _dummy(cls):
    vals = {}
    for f in dataclasses.fields(cls):
        t = str(f.type)
        vals[f.name] = 1 if t == "int" else (True if t == "bool" else "x")
    return cls(**vals)


def test_every_event_type_is_rendered_by_both_front_ends(capsys, monkeypatch):
    from efactura_sync import cli
    from efactura_sync.web.strings import translator
    events = [_dummy(c) for c in core.SyncEvent.__args__]
    t = translator("ro")
    for ev, cls in zip(events, core.SyncEvent.__args__):
        text = weberrors.render_event({"type": cls.__name__, **dataclasses.asdict(ev)}, t)
        assert text and text != cls.__name__, cls.__name__
    monkeypatch.setattr(core, "sync", lambda cfg: iter(events))
    cli.cmd_sync({})
    assert len(capsys.readouterr().out.strip().splitlines()) >= len(events)


def test_ui_honours_env_override(harness, monkeypatch):
    harness.write_cfg(environment="prod")
    from efactura_sync import cli
    monkeypatch.setattr(core, "ENV_OVERRIDE", None)
    monkeypatch.setattr(webmod, "run_ui", lambda **kw: None)
    assert cli.main(["--env", "test", "ui"]) == 0
    assert core.load_config()["environment"] == "test"


def test_config_flag_moves_all_paths(tmp_path, monkeypatch):
    import json
    for name in ("CONFIG_DIR", "CONFIG_PATH", "TOKENS_PATH", "DB_PATH"):
        monkeypatch.setattr(core, name, getattr(core, name))
    d = tmp_path / "other"; d.mkdir()
    (d / "config.json").write_text(json.dumps({"client_id": "c", "client_secret": "s", "cif": "1",
                                               "base_dir": str(tmp_path / "inv")}))
    from efactura_sync import cli
    assert cli.main(["--config", str(d / "config.json"), "status"]) == 0
    assert core.DB_PATH == d / "invoices.db" and core.TOKENS_PATH == d / "tokens.json"


def test_open_folder_creates_missing_folder_and_confirms(harness):
    harness.write_cfg()
    base = harness.tmp / "inv"
    assert not base.exists()
    r = harness.post("/deschide-dosar")
    assert r.status_code == 303 and base.is_dir()
    assert "dosarul" in harness.client.get("/facturi").get_data(as_text=True).lower()


def test_quit_is_refused_while_a_sync_runs(harness):
    harness.write_cfg()
    gate = threading.Event()

    def slow(cfg):
        yield core.SyncStarted("t", "1", 60)
        gate.wait(5)
        yield core.SyncFinished(0, 0, 0, 0, "x")

    harness.app.runner.engine = slow
    assert harness.post("/sync").status_code == 303
    r = harness.post("/iesire")
    assert r.status_code == 303 and harness.calls["shutdown"] == 0
    gate.set(); assert harness.app.runner.wait(5)
    assert harness.post("/iesire").status_code == 200 and harness.calls["shutdown"] == 1


def test_folder_pickers_are_quiet(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout="/x\n", stderr="")

    monkeypatch.setattr(webmod.subprocess, "run", fake_run)
    monkeypatch.setattr(webmod.sys, "platform", "win32")
    webmod.pick_folder_default("C:/x")
    assert seen["kw"].get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    monkeypatch.setattr(webmod.sys, "platform", "darwin")
    webmod.pick_folder_default("/tmp")
    assert "System Events" not in " ".join(seen["argv"])


def test_anaf_env_vars_are_isolated_from_the_suite():
    assert not any(k in os.environ for k in ("ANAF_CLIENT_ID", "ANAF_CLIENT_SECRET", "ANAF_CIF"))
