"""WP7a (2026-09-18): multiple firms — one selected firm at a time.

Config gains `firms` + `selected_firm`; legacy single-`cif` configs normalize to one
firm. The DB gains `firm_id` with `(firm_id, download_id)` keys; schema 1 → 2 is the
first real migration, exercised here against a v1-shaped database built from the
old DDL.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_firms_"))

import pytest  # noqa: E402

from efactura_sync import core  # noqa: E402
from conftest import SAMPLE_INVOICE, make_zip, msg, two_invoices, FakeAnaf  # noqa: E402

V1_DDL = """
CREATE TABLE invoices (download_id TEXT PRIMARY KEY, invoice_id TEXT, supplier_name TEXT,
  supplier_cif TEXT, issue_date TEXT, message_date TEXT, tip TEXT, doc_type TEXT,
  xml_sha256 TEXT, pdf_path TEXT, xml_path TEXT, zip_path TEXT, pdf_ok INTEGER DEFAULT 1,
  note TEXT, downloaded_at TEXT);
CREATE INDEX idx_invoices_hash ON invoices(xml_sha256);
CREATE TABLE duplicates (download_id TEXT, invoice_id TEXT, reason TEXT, seen_at TEXT);
CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE skipped (download_id TEXT PRIMARY KEY, reason TEXT, seen_at TEXT);
INSERT INTO sync_state VALUES ('schema_version', '1');
INSERT INTO sync_state VALUES ('last_run', '2026-09-01T10:00:00');
INSERT INTO invoices (download_id, invoice_id, supplier_name, issue_date, xml_sha256, pdf_ok)
  VALUES ('1001', 'FAC-1', 'S', '2026-03-14', 'h1', 1);
INSERT INTO skipped VALUES ('2002', 'dup', '2026-09-01');
"""


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name in ("CONFIG_DIR", "CONFIG_PATH", "TOKENS_PATH", "DB_PATH"):
        monkeypatch.setattr(core, name, tmp_path / {"CONFIG_DIR": "", "CONFIG_PATH": "config.json",
                                                    "TOKENS_PATH": "tokens.json", "DB_PATH": "invoices.db"}[name])
    monkeypatch.setattr(core, "FIRM_OVERRIDE", None)
    core._SCHEMA_READY.clear()
    return tmp_path


def write_raw(tmp, **raw):
    (tmp / "config.json").write_text(json.dumps(raw))


# ---- config ----------------------------------------------------------------

def test_legacy_config_normalizes_to_one_selected_firm(paths):
    write_raw(paths, client_id="c", client_secret="s", cif="RO12345678", base_dir="/x/inv", language="ro")
    raw = core.read_config_raw()
    assert len(raw["firms"]) == 1
    firm = raw["firms"][0]
    assert firm["cif"] == "12345678" and firm["base_dir"] == "/x/inv" and firm["name"]
    assert raw["selected_firm"] == firm["id"] and "cif" not in raw
    cfg = core.load_config()
    assert (cfg["cif"], cfg["base_dir"], cfg["firm_id"]) == ("12345678", "/x/inv", firm["id"])


def test_load_config_uses_selected_firm_and_override(paths):
    write_raw(paths, client_id="c", client_secret="s", selected_firm="b",
              firms=[{"id": "a", "name": "Alpha SRL", "cif": "1", "base_dir": "/a"},
                     {"id": "b", "name": "Beta SRL", "cif": "2", "base_dir": "/b"}])
    cfg = core.load_config()
    assert (cfg["firm_id"], cfg["firm_name"], cfg["cif"], cfg["base_dir"]) == ("b", "Beta SRL", "2", "/b")
    core.FIRM_OVERRIDE = "1"                        # by cif
    assert core.load_config()["firm_id"] == "a"
    core.FIRM_OVERRIDE = "a"                        # by id
    assert core.load_config()["firm_id"] == "a"
    core.FIRM_OVERRIDE = "nope"
    with pytest.raises(core.ConfigError) as ei:
        core.load_config()
    assert ei.value.code == "unknown_firm"


def test_unknown_selection_falls_back_to_first_firm(paths):
    write_raw(paths, client_id="c", client_secret="s", selected_firm="gone",
              firms=[{"id": "a", "name": "Alpha", "cif": "1", "base_dir": "/a"}])
    assert core.load_config()["firm_id"] == "a"


def test_no_firms_is_missing_values(paths):
    write_raw(paths, client_id="c", client_secret="s", firms=[])
    with pytest.raises(core.ConfigError) as ei:
        core.load_config()
    assert ei.value.code == "missing_values"


def test_firm_helpers(paths):
    write_raw(paths, client_id="c", client_secret="s")
    raw = core.read_config_raw()
    f1 = core.add_firm(raw, "Alpha / Beta SRL", "RO111")
    assert f1["cif"] == "111" and f1["base_dir"] == str(core.DEFAULT_BASE_DIR / "Alpha Beta SRL")
    assert raw["selected_firm"] == f1["id"]           # first firm becomes selected
    f2 = core.add_firm(raw, "Gamma", "222", base_dir="/g")
    assert raw["selected_firm"] == f1["id"] and f2["base_dir"] == "/g"
    core.select_firm(raw, f2["id"])
    assert raw["selected_firm"] == f2["id"]
    core.remove_firm(raw, f2["id"])
    assert [f["id"] for f in raw["firms"]] == [f1["id"]] and raw["selected_firm"] == f1["id"]
    core.save_config(raw)
    saved = json.loads((paths / "config.json").read_text())
    assert "firms" in saved and "cif" not in saved


# ---- DB migration ------------------------------------------------------------

def _v1_db(path):
    conn = sqlite3.connect(path); conn.executescript(V1_DDL); conn.close()


def test_v1_database_migrates_to_v2_and_adopts_rows(paths):
    _v1_db(paths / "invoices.db")
    write_raw(paths, client_id="c", client_secret="s", cif="12345678", base_dir="/x")
    firm_id = core.load_config()["firm_id"]
    conn = core.connect_db()
    assert core.get_state(conn, "schema_version") == "2"
    assert core.get_state(conn, "last_run") == "2026-09-01T10:00:00"        # state kept
    assert [tuple(r) for r in conn.execute("SELECT firm_id, invoice_id FROM invoices")] == [(firm_id, "FAC-1")]
    assert conn.execute("SELECT firm_id FROM skipped").fetchone()[0] == firm_id
    assert core.db_has_download(conn, firm_id, "1001") and core.db_has_download(conn, firm_id, "2002")
    # composite key: the same ANAF id under another firm is a different row
    conn.execute("INSERT INTO invoices (firm_id, download_id, invoice_id) VALUES ('other', '1001', 'X')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
    conn.close()
    core._SCHEMA_READY.clear()
    conn = core.connect_db()                                                  # idempotent
    assert core.get_state(conn, "schema_version") == "2"
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2


def test_migration_leaves_rows_unassigned_when_ambiguous(paths):
    _v1_db(paths / "invoices.db")
    write_raw(paths, client_id="c", client_secret="s",
              firms=[{"id": "a", "name": "A", "cif": "1", "base_dir": "/a"},
                     {"id": "b", "name": "B", "cif": "2", "base_dir": "/b"}])
    conn = core.connect_db()
    assert conn.execute("SELECT firm_id FROM invoices").fetchone()[0] == ""
    assert not core.db_has_download(conn, "a", "1001")


def test_fresh_database_is_v2(paths):
    conn = core.connect_db()
    assert core.get_state(conn, "schema_version") == "2"
    cols = [r[1] for r in conn.execute("PRAGMA table_info(invoices)")]
    assert "firm_id" in cols


# ---- engine per firm ---------------------------------------------------------

def test_sync_isolates_firms(paths, monkeypatch):
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, s: b"%PDF-x")
    def run(firm_id, cif, fake):
        monkeypatch.setattr(core, "api_get", fake.api_get)
        cfg = {"environment": "test", "cif": cif, "base_dir": str(paths / firm_id), "firm_id": firm_id}
        return list(core.sync(cfg))
    e1 = run("f1", "1", two_invoices())
    e2 = run("f2", "2", two_invoices())                # same download ids, other firm
    assert e1[-1].new == 2 and e2[-1].new == 2          # not treated as duplicates
    assert (paths / "f1" / "invoices.csv").exists() and (paths / "f2" / "invoices.csv").exists()
    assert core.status_report({"environment": "test", "cif": "1", "base_dir": str(paths / "f1"), "firm_id": "f1"}).total == 2
    assert len(core.list_invoices("f1")) == 2 and len(core.list_invoices("f2")) == 2
    assert core.invoice_by_download_id("f1", "1001")["invoice_id"] == "FAC-2026-001"
    assert core.invoice_by_download_id("zz", "1001") is None
    assert run("f1", "1", two_invoices())[-1].duplicates == 2   # per-firm dedup still works


# ---- WP7b: CLI ----------------------------------------------------------------

def two_firms(paths):
    write_raw(paths, client_id="c", client_secret="s", selected_firm="a", environment="test",
              firms=[{"id": "a", "name": "Alpha SRL", "cif": "1", "base_dir": str(paths / "a")},
                     {"id": "b", "name": "Beta SRL", "cif": "2", "base_dir": str(paths / "b")}])


def test_cli_firm_flag_selects_by_cif_or_id(paths, capsys, monkeypatch):
    two_firms(paths)
    from efactura_sync import cli
    assert cli.main(["--firm", "2", "status"]) == 0
    out = capsys.readouterr().out
    assert "Beta SRL" in out and "Alpha" not in out
    assert core.FIRM_OVERRIDE == "2"
    monkeypatch.setattr(core, "FIRM_OVERRIDE", None)
    assert cli.main(["--firm", "zz", "status"]) == 2          # unknown firm → clean error


# ---- WP7c: UI ------------------------------------------------------------------

def _firms_cfg(harness):
    (harness.tmp / "config.json").write_text(json.dumps({
        "client_id": "c", "client_secret": "s", "environment": "test", "selected_firm": "a",
        "firms": [{"id": "a", "name": "Alpha SRL", "cif": "1", "base_dir": str(harness.tmp / "a")},
                  {"id": "b", "name": "Beta SRL", "cif": "2", "base_dir": str(harness.tmp / "b")}]}))


def test_switcher_lists_firms_and_switches(harness):
    _firms_cfg(harness)
    html = harness.client.get("/").get_data(as_text=True)
    assert 'name="firm"' in html and ">Alpha SRL<" in html and ">Beta SRL<" in html
    assert 'value="a" selected' in html
    r = harness.post("/firma", firm="b", next="/facturi")
    assert r.status_code == 303 and r.headers["Location"].endswith("/facturi")
    assert json.loads((harness.tmp / "config.json").read_text())["selected_firm"] == "b"
    assert 'value="b" selected' in harness.client.get("/").get_data(as_text=True)
    assert harness.post("/firma", firm="zz").status_code == 303   # unknown: ignored, not 500


def test_single_firm_shows_name_without_a_select(harness):
    harness.write_cfg()
    html = harness.client.get("/").get_data(as_text=True)
    assert 'name="firm"' not in html and "Firma mea" in html


def test_pages_operate_on_the_selected_firm(harness):
    _firms_cfg(harness)
    harness.synced()                                   # syncs Alpha (selected)
    assert (harness.tmp / "a" / "invoices.csv").exists() and not (harness.tmp / "b").exists()
    assert "FAC-2026-001" in harness.client.get("/facturi").get_data(as_text=True)
    harness.post("/firma", firm="b")
    html = harness.client.get("/facturi").get_data(as_text=True)
    assert "FAC-2026-001" not in html and "Beta SRL" in html
    assert harness.client.get("/pdf/1001").status_code == 404   # Alpha's file is not Beta's


def test_settings_edits_the_selected_firm_only(harness):
    _firms_cfg(harness)
    r = harness.post("/setari", client_id="c", client_secret="", cif="RO111", name="Alpha Nou SRL",
                     environment="test", redirect_uri="x", base_dir=str(harness.tmp / "a2"))
    assert r.status_code == 303
    firms = {f["id"]: f for f in json.loads((harness.tmp / "config.json").read_text())["firms"]}
    assert (firms["a"]["name"], firms["a"]["cif"], firms["a"]["base_dir"]) == ("Alpha Nou SRL", "111", str(harness.tmp / "a2"))
    assert firms["b"] == {"id": "b", "name": "Beta SRL", "cif": "2", "base_dir": str(harness.tmp / "b")}
    html = harness.client.get("/setari").get_data(as_text=True)
    assert 'value="Alpha Nou SRL"' in html and 'value="111"' in html


def test_add_and_remove_firms(harness):
    _firms_cfg(harness)
    r = harness.post("/firme/adauga", name="Gamma / SRL", cif="RO333")
    assert r.status_code == 303
    raw = json.loads((harness.tmp / "config.json").read_text())
    g = [f for f in raw["firms"] if f["name"] == "Gamma / SRL"][0]
    assert g["cif"] == "333" and g["base_dir"].endswith("Gamma SRL") and raw["selected_firm"] == g["id"]
    assert harness.post("/firme/adauga", name="", cif="4").status_code == 400
    assert harness.post("/firme/adauga", name="X", cif="abc").status_code == 400
    assert harness.post("/firme/sterge", firm=g["id"]).status_code == 303
    raw = json.loads((harness.tmp / "config.json").read_text())
    assert [f["id"] for f in raw["firms"]] == ["a", "b"] and raw["selected_firm"] == "a"
    for fid in ("a", "b"):
        harness.post("/firme/sterge", firm=fid)
    raw = json.loads((harness.tmp / "config.json").read_text())
    assert len(raw["firms"]) == 1                          # the last firm cannot be removed
    assert "ultima firmă" in harness.client.get("/setari").get_data(as_text=True)


def test_settings_lists_firms_with_actions(harness):
    _firms_cfg(harness)
    html = harness.client.get("/setari").get_data(as_text=True)
    assert 'action="/firme/adauga"' in html and 'action="/firme/sterge"' in html
    assert html.count('name="firm"') >= 3                  # switcher + per-row actions
    assert "Adaugă firmă" in html


def test_wizard_names_the_first_firm(harness):
    r = harness.post("/setari", client_id="c", client_secret="s", cif="RO9", name="Prima SRL",
                     environment="prod", redirect_uri="x", base_dir=str(harness.tmp / "p"), next="/start?step=3")
    assert r.status_code == 303
    raw = json.loads((harness.tmp / "config.json").read_text())
    assert raw["firms"][0]["name"] == "Prima SRL" and raw["firms"][0]["cif"] == "9"
    html = harness.client.get("/").get_data(as_text=True)
    assert "Prima SRL" in html


def test_wizard_step2_has_a_name_field_with_a_default(harness):
    html = harness.client.get("/start?step=2").get_data(as_text=True)
    assert 'name="name"' in html and 'value="Firma mea"' in html
