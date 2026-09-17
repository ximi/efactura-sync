# ANAF e-Factura Invoice Downloader

Download **received** invoices from ANAF's SPV (Spațiul Privat Virtual) and file them
locally as **PDFs**, organized by month, with a CSV index for accounting.

ANAF stores invoices only as UBL XML. This tool downloads the XML, converts it to a
human-readable PDF using ANAF's own public renderer, and keeps the XML + original ZIP
alongside each PDF for audit.

## What it does

- OAuth 2.0 login (authorization code + PKCE), tokens stored in `~/.anaf_invoices/tokens.json`
- Lists received invoices from the last 60 days and downloads only new ones
- De-duplicates by ANAF download id and by invoice content hash (SQLite at `~/.anaf_invoices/invoices.db`)
- Renders each invoice to PDF and files it under `base_dir/YYYY/MM/`
- Writes `base_dir/invoices.csv` for easy accounting review

Files per invoice:

```
base_dir/2026/06/{invoice_id}_{supplier}_{date}.pdf   <- primary
                 {invoice_id}_{supplier}_{date}.xml   <- source UBL
                 {invoice_id}_{supplier}_{date}.zip   <- original (incl. ANAF signature)
```

## Requirements

- Python 3.10+
- A qualified digital certificate registered in ANAF SPV (DigiSign, CertSign, AlfaTrust, …)

```bash
pip install -r requirements.txt
```

## 1. Register an ANAF OAuth application

You need a `client_id` and `client_secret`. One-time setup:

1. Log in to the ANAF portal with your qualified certificate: <https://pfinternet.anaf.ro>
2. Open the OAuth app registration page: <https://www.anaf.ro/InregOauth/>
3. Create a new application:
   - **Name** — anything, e.g. `invoice-downloader`
   - **Callback / redirect URL** — must match `redirect_uri` in your config exactly.
     `https://localhost/callback` is fine for the manual-paste flow used here.
   - **Service** — select **EFACTURA**
4. Click **Generate Client ID**. Copy the resulting **client_id** and **client_secret**.

(Reference: ANAF "Oauth — procedura de înregistrare aplicații".)

## 2. Configure

Create `~/.anaf_invoices/config.json` (the directory is created automatically on first run):

```json
{
  "client_id": "your-client-id",
  "client_secret": "your-client-secret",
  "redirect_uri": "https://localhost/callback",
  "cif": "12345678",
  "environment": "test",
  "base_dir": "~/.anaf_invoices/invoices"
}
```

- `cif` — your company fiscal code, numeric, **no `RO` prefix** and no spaces.
- `environment` — `test` while validating, `prod` for real invoices.
- `base_dir` — where PDFs are filed (optional; defaults to `~/.anaf_invoices/invoices`).

Secrets can also be supplied via environment variables, which override the file:
`ANAF_CLIENT_ID`, `ANAF_CLIENT_SECRET`, `ANAF_CIF`.

`tokens.json` is written with `0600` permissions. `config.json` is yours to create;
the script tightens it to `0600` on first load if it is more permissive.

**Never commit `config.json`** — it contains your `client_secret`. It is listed in
`.gitignore`, but keep it in `~/.anaf_invoices/` rather than the project directory
so it can't be swept into a repository by accident.

## 3. Use

```bash
# One-time login. Opens nothing automatically: it prints a URL.
# Open the URL in a browser that has your ANAF certificate, authorize, then
# paste the redirected URL (or the `code` value) back into the terminal.
python anaf_invoices.py auth

# Download new received invoices and convert them to PDF.
python anaf_invoices.py sync

# Show what has been downloaded.
python anaf_invoices.py status
```

Useful flags: `--env prod|test` (override environment), `--config /path/to/config.json`.

### Running on a schedule

`sync` is idempotent and cron-friendly — already-downloaded invoices are skipped:

```cron
*/30 * * * * cd /path/to/efactura && /path/to/python anaf_invoices.py sync >> ~/.anaf_invoices/sync.log 2>&1
```

## Notes

- **Tokens**: the access token lasts ~90 days and is auto-refreshed using the refresh
  token (~365 days). When the refresh token expires you'll be told to re-run `auth`.
  Only `auth` needs the certificate in the browser; `sync` does not.
- **PDF rendering** uses ANAF's public `transformare` service (no auth). If an invoice
  XML fails to render even with validation skipped, the invoice is still filed (XML + ZIP
  kept) and flagged; `status` reports the count under "pdf failures".
- **Test first**: run with `"environment": "test"` to validate your setup before
  switching to `prod`.

## Files & locations

| Path | Purpose |
|------|---------|
| `~/.anaf_invoices/config.json` | credentials & settings (chmod 600) |
| `~/.anaf_invoices/tokens.json` | OAuth tokens (chmod 600) |
| `~/.anaf_invoices/invoices.db` | dedup + sync state (SQLite) |
| `base_dir/YYYY/MM/…` | filed PDFs / XML / ZIP |
| `base_dir/invoices.csv` | accounting index |
