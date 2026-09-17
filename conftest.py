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
    def __init__(self, json_data=None, content=b"", status_code=200, text=""):
        self._json, self.content, self.status_code, self.text = json_data, content, status_code, text

    def json(self):
        return self._json


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
