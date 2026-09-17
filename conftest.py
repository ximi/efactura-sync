"""Shared test fixtures. Deliberately imports nothing from the package: the test
modules must set ANAF_CONFIG_DIR before core is imported the first time."""

import io
import zipfile

SAMPLE_INVOICE = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:efactura.mfinante.ro:CIUS-RO:1.0.1</cbc:CustomizationID>
  <cbc:ID>FAC-2026-001</cbc:ID>
  <cbc:IssueDate>2026-03-14</cbc:IssueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>RON</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>Furnizor Demo SRL</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>RO87654321</cbc:CompanyID>
      </cac:PartyTaxScheme>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Furnizor Demo SRL</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
</Invoice>
"""

SAMPLE_CREDIT_NOTE = b"""<?xml version="1.0" encoding="UTF-8"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>CN-2026-009</cbc:ID>
  <cbc:IssueDate>2026-04-02</cbc:IssueDate>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>Alt Furnizor &amp; Co</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme><cbc:CompanyID>RO11112222</cbc:CompanyID></cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingSupplierParty>
</CreditNote>
"""


# --------------------------------------------------------------------------- #
# Fake ANAF — drives core.sync() end-to-end with no network
# --------------------------------------------------------------------------- #

def make_zip(xml: bytes, name: str = "4017.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"semnatura_{name}", b"<signature/>")
        zf.writestr(name, xml)
    return buf.getvalue()


def msg(download_id: str, tip: str = "FACTURA") -> dict:
    return {"id": download_id, "tip": tip, "data_creare": "2026-03-14T10:00:00",
            "cif_emitent": "87654321", "id_solicitare": f"req-{download_id}"}


class FakeResp:
    def __init__(self, json_data=None, content=b"", status_code=200, text="", headers=None):
        self._json, self.content, self.status_code, self.text = json_data, content, status_code, text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


class FakeAnaf:
    def __init__(self, messages, zips, paginated_ok=True, fail_download=()):
        self.messages, self.zips = messages, zips
        self.paginated_ok, self.fail_download = paginated_ok, set(fail_download)
        self.calls = []

    def api_get(self, cfg, path, params):
        self.calls.append((path, dict(params)))
        if path == "listaMesajePaginatieFactura":
            if not self.paginated_ok:
                raise RuntimeError("HTTP 500 from ANAF")
            return FakeResp({"mesaje": self.messages, "numar_total_pagini": 1})
        if path == "listaMesajeFactura":
            return FakeResp({"mesaje": self.messages})
        if path == "descarcare":
            did = params["id"]
            if did in self.fail_download:
                raise RuntimeError("connection reset")
            return FakeResp(content=self.zips[did])
        raise AssertionError(f"unexpected API path {path}")


def two_invoices() -> FakeAnaf:
    return FakeAnaf(
        messages=[msg("1001"), msg("1002")],
        zips={"1001": make_zip(SAMPLE_INVOICE), "1002": make_zip(SAMPLE_CREDIT_NOTE)},
    )


# --------------------------------------------------------------------------- #
# Shared fixtures (imports of the package happen lazily, inside the fixtures)
# --------------------------------------------------------------------------- #

import json as _json
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _no_anaf_env(monkeypatch):
    """The README documents ANAF_* overrides; a developer with them exported must
    not get a different test result (review finding, 2026-09-17)."""
    for key in ("ANAF_CLIENT_ID", "ANAF_CLIENT_SECRET", "ANAF_CIF"):
        monkeypatch.delenv(key, raising=False)


@_pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + base_dir; returns a runner that yields the event list."""
    from efactura_sync import core
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "invoices.db")
    cfg = {"environment": "test", "cif": "1", "base_dir": str(tmp_path / "inv")}

    def run(fake, pdf=lambda xml, standard: b"%PDF-fake"):
        monkeypatch.setattr(core, "api_get", fake.api_get)
        monkeypatch.setattr(core, "xml_to_pdf", pdf)
        return list(core.sync(cfg))

    run.cfg, run.base = cfg, tmp_path / "inv"
    return run


BASE_CFG = {"client_id": "cid", "client_secret": "topsecret-value", "cif": "50000000",
            "environment": "test", "redirect_uri": "https://localhost/callback"}


@_pytest.fixture
def harness(tmp_path, monkeypatch):
    """Isolated config/tokens/DB, a fake ANAF behind core.sync, injectable side effects."""
    from efactura_sync import core
    from efactura_sync.web import create_app
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(core, "TOKENS_PATH", tmp_path / "tokens.json")
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "invoices.db")
    fake = two_invoices()
    monkeypatch.setattr(core, "api_get", fake.api_get)
    monkeypatch.setattr(core, "xml_to_pdf", lambda xml, standard: b"%PDF-fake")

    calls = {"opened": [], "shutdown": 0, "pick_result": None, "pick_initial": None}

    def pick(initial):
        calls["pick_initial"] = str(initial)
        return calls["pick_result"]

    app = create_app(
        open_path=lambda p: calls["opened"].append(str(p)),
        shutdown=lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        pick_folder=pick,
    )
    app.config["TESTING"] = True

    class H:
        pass

    h = H()
    h.app, h.client, h.calls, h.fake, h.tmp = app, app.test_client(), calls, fake, tmp_path
    h.csrf = app.config["CSRF_TOKEN"]

    def write_cfg(**overrides):
        cfg = {**BASE_CFG, "base_dir": str(tmp_path / "inv"), **overrides}
        (tmp_path / "config.json").write_text(_json.dumps(cfg))
        return cfg

    def post(path, **form):
        return h.client.post(path, data={"csrf": h.csrf, **form})

    def synced():
        """Run one sync to completion through the UI; returns the SSE event list."""
        assert post("/sync").status_code == 303
        assert h.app.runner.wait(5)
        raw = h.client.get("/sync/events").get_data(as_text=True)
        events = []
        for block in raw.split("\n\n"):
            lines = block.strip().splitlines()
            if not lines or any(l.startswith("event: done") for l in lines):
                continue
            data = "".join(l[6:] for l in lines if l.startswith("data: "))
            events.append(_json.loads(data))
        return events

    h.write_cfg, h.post, h.synced = write_cfg, post, synced
    return h
