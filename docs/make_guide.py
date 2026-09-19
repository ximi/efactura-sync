"""Regenerate the user guide's screenshots and PDF (docs/GHID.md → docs/GHID.pdf).

Run from the repo root:  .venv/bin/python docs/make_guide.py
Needs: pip install -r requirements-docs.txt && playwright install chromium

The app runs on a throwaway config dir with seeded demo data (two firms, a few
invoices, one without PDF), so every screenshot is reproducible and contains no
real data. Light colour scheme, Romanian, 1000 px wide, 2× scale.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SHOTS = DOCS / "screenshots"
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="efactura-guide-"))
os.environ["ANAF_CONFIG_DIR"] = str(TMP)

from efactura_sync import core  # noqa: E402
from efactura_sync.web import create_app  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FIRMS = [{"id": "a", "name": "Alpha Consult SRL", "cif": "12345678", "base_dir": str(TMP / "Facturi e-Factura" / "Alpha Consult SRL")},
         {"id": "b", "name": "Beta Trading SRL", "cif": "87654321", "base_dir": str(TMP / "Facturi e-Factura" / "Beta Trading SRL")}]
INVOICES = [  # firm, download_id, invoice_id, supplier, cif, issue_date, doc_type, pdf_ok
    ("a", "8331", "FA-2026-0417", "Orange România SA", "RO9010105", "2026-09-12", "invoice", 1),
    ("a", "8302", "2026-000913", "Claudia Design SRL", "RO31852211", "2026-09-02", "invoice", 1),
    ("a", "8291", "PLU-77812", "Plus Office Supplies SRL", "RO18223400", "2026-08-31", "invoice", 0),
    ("a", "8140", "ST-000045", "Claudia Design SRL", "RO31852211", "2026-08-16", "credit_note", 1),
    ("a", "7990", "PLU-77650", "Plus Office Supplies SRL", "RO18223400", "2026-07-31", "invoice", 1),
    ("b", "9001", "BT-1027", "Dedeman SRL", "RO2816464", "2026-09-05", "invoice", 1),
]


def write_config(firms=None, selected="a"):
    cfg = {"client_id": "a1b2c3d4e5f6", "client_secret": "secret", "environment": "prod",
           "redirect_uri": "https://localhost/callback", "language": "ro"}
    if firms:
        cfg.update({"firms": firms, "selected_firm": selected})
    (TMP / "config.json").write_text(json.dumps(cfg))


def seed_db():
    conn = core.connect_db()
    for firm, did, inv, sup, cif, date, kind, ok in INVOICES:
        base = Path(next(f["base_dir"] for f in FIRMS if f["id"] == firm))
        d = base / date[:4] / date[5:7]
        d.mkdir(parents=True, exist_ok=True)
        stem = d / f"{core.sanitize(inv, 40)}_{core.sanitize(sup, 40)}_{date}"
        stem.with_suffix(".xml").write_text("<Invoice/>")
        if ok:
            stem.with_suffix(".pdf").write_bytes(b"%PDF-1.4 demo\n%%EOF\n")
        conn.execute(
            "INSERT OR REPLACE INTO invoices (firm_id, download_id, invoice_id, supplier_name, supplier_cif, "
            "issue_date, message_date, doc_type, pdf_ok, pdf_path, xml_path, downloaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (firm, did, inv, sup, cif, date, date, kind, ok, str(stem.with_suffix(".pdf")) if ok else "",
             str(stem.with_suffix(".xml")), f"{date}T09:12:00"))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('last_run:a', '2026-09-18T09:12:00')")
    conn.commit(); conn.close()


def demo_engine(cfg):
    yield core.SyncStarted("prod", cfg["cif"], 60)
    yield core.MessagesListed(7)
    yield core.Duplicate("8302", "already downloaded")
    yield core.Duplicate("8291", "already downloaded")
    yield core.InvoiceDone("8331", "FA-2026-0417", "Orange România SA", "2026-09-12", True, "x", "y")
    yield core.InvoiceDone("8340", "2026-000921", "Claudia Design SRL", "2026-09-15", True, "x", "y")
    yield core.PdfFailed("8351", "PLU-77901", "PDF conversion failed")
    yield core.InvoiceDone("8351", "PLU-77901", "Plus Office Supplies SRL", "2026-09-16", False, "", "y")
    yield core.SyncFinished(3, 2, 1, 0, cfg["base_dir"])


def main():
    if SHOTS.exists():
        shutil.rmtree(SHOTS)
    SHOTS.mkdir(parents=True)
    app = create_app(pick_folder=lambda initial, prompt=None: None)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1000, "height": 480}, device_scale_factor=2,
                                  color_scheme="light", locale="ro-RO")
        page = ctx.new_page()

        def shot(name, path="/", full=True, clip=None, wait=None):
            page.goto(base + path)
            if wait:
                page.wait_for_selector(wait)
            page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full and clip is None, clip=clip)
            print("  ", name)

        def csrf():
            return page.evaluate("document.querySelector('input[name=csrf]').value")

        # --- first run (no config) ---
        (TMP / "config.json").unlink(missing_ok=True)
        shot("01-wizard-pas1", "/start")
        shot("02-wizard-pas2", "/start?step=2")

        # --- one firm configured (a first run), not authenticated ---
        write_config(firms=FIRMS[:1])
        seed_db()
        shot("03-wizard-pas3", "/start?step=3")
        page.goto(base + "/start?step=3")
        page.click("text=Începe autentificarea")
        page.wait_for_selector("textarea[name=pasted]")
        page.screenshot(path=str(SHOTS / "04-autentificare-adresa.png"), full_page=True); print("   04")
        page.fill("textarea[name=pasted]", "https://localhost/callback?error=access_denied&state=…")
        page.click("text=Finalizează")
        page.wait_for_selector("text=ANAF a refuzat accesul")
        page.screenshot(path=str(SHOTS / "05-autentificare-eroare.png"), full_page=True); print("   05")
        shot("06-acasa-neautentificat", "/")

        # --- authenticated, after a sync ---
        core.save_tokens({"access_token": "demo", "refresh_token": "demo", "expires_at": 4102444800,
                          "obtained_at": 0, "token_type": "Bearer"})
        write_config(firms=FIRMS)                 # now the accountant case: two firms
        app.pending_auth = None
        app.runner.engine = demo_engine
        app.runner.start(core.load_config()); app.runner.wait(5)
        shot("07-acasa", "/")
        shot("08-facturi", "/facturi")
        shot("09-facturi-fara-pdf", "/facturi?fara_pdf=1")
        shot("10-setari", "/setari")
        page.goto(base + "/")
        page.screenshot(path=str(SHOTS / "11-selector-firma.png"),
                        clip={"x": 560, "y": 0, "width": 440, "height": 56}); print("   11")
        browser.close()
    server.shutdown()

    # --- Markdown → PDF (same Chromium) ---
    import markdown
    md = (DOCS / "GHID.md").read_text(encoding="utf-8")
    body = markdown.markdown(md, extensions=["tables", "toc", "sane_lists"],
                             extension_configs={"toc": {"toc_depth": "2-3"}})
    html = f"""<!doctype html><html lang="ro"><head><meta charset="utf-8">
    <base href="{DOCS.as_uri()}/"><style>
      @page {{ size: A4; margin: 18mm 16mm; }}
      body {{ font: 11pt/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1b1b1f; }}
      h1 {{ font-size: 24pt; margin: 0 0 4pt; }} h2 {{ font-size: 16pt; margin-top: 22pt; border-bottom: 1px solid #ddd; padding-bottom: 3pt; page-break-after: avoid; }}
      h3 {{ font-size: 12.5pt; margin-top: 14pt; page-break-after: avoid; }}
      img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 6px; margin: 6pt 0 10pt; page-break-inside: avoid; }}
      code {{ background: #f1f1f4; padding: 1px 4px; border-radius: 4px; font-size: 10pt; }}
      pre {{ background: #f1f1f4; padding: 8pt; border-radius: 6px; font-size: 9.5pt; white-space: pre-wrap; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 10pt; }} th, td {{ border: 1px solid #ddd; padding: 4pt 6pt; text-align: left; vertical-align: top; }}
      blockquote {{ border-left: 4px solid #1d5fd1; margin: 8pt 0; padding: 4pt 10pt; background: #f4f7ff; }}
      .toc {{ background: #f7f7f8; padding: 8pt 14pt; border-radius: 6px; }} .toc ul {{ margin: 4pt 0; }}
      a {{ color: #1d5fd1; }}
    </style></head><body>{body}</body></html>"""
    (TMP / "ghid.html").write_text(html, encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((TMP / "ghid.html").as_uri())
        page.pdf(path=str(DOCS / "GHID.pdf"), format="A4", print_background=True,
                 display_header_footer=True, header_template="<span></span>",
                 footer_template='<div style="font-size:8pt;color:#888;width:100%;text-align:center">'
                                 'eFactura Sync — Ghid de utilizare · pagina <span class="pageNumber"></span>/<span class="totalPages"></span></div>',
                 margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        browser.close()
    print("wrote", DOCS / "GHID.pdf")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
