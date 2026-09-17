# eFactura Sync

Descarcă facturile **primite** din SPV (ANAF e-Factura) și le salvează local ca **PDF**,
organizate pe luni, cu un fișier CSV pentru contabilitate. Are o interfață în browser,
în română (și engleză), și rulează pe macOS și Windows fără instalare.

*Download **received** invoices from ANAF's SPV and file them locally as **PDFs**,
organized by month, with a CSV index for accounting. Browser UI in Romanian (and
English); runs on macOS and Windows with nothing to install.*

## Pentru utilizatori · For users

### 🇷🇴 Română

**Ce ai nevoie (o singură dată, înainte de instalare)**

1. **Un certificat digital calificat** (DigiSign, certSIGN, AlfaTrust…) înregistrat în SPV
   pentru firma ta, cu e-Factura activată. Fără el nu te poți autentifica la ANAF.
2. **O aplicație OAuth înregistrată la ANAF**, din care obții *Client ID* și *Client secret*:
   anaf.ro → Servicii online → Înregistrare aplicații developeri → *Înregistrare în vederea
   accesării serviciilor web* → autentificare în SPV → profil OAuth nou cu un nume oarecare,
   **Callback URL `https://localhost/callback`** și serviciul **EFACTURA** bifat → *Generare*.
   Copiază Client ID și Client secret; aplicația ți le cere la prima pornire.

**Instalare**

- **macOS:** descarcă `eFactura-Sync-macos-arm64.zip` (Mac cu Apple Silicon, 2020+) sau
  `eFactura-Sync-macos-x86_64.zip` (Mac cu Intel) din
  [Releases](https://github.com/ximi/efactura-sync/releases/latest). Dezarhivează și mută
  `eFactura Sync.app` în *Applications*. **Prima dată: click dreapta → Deschide → Deschide**
  (aplicația nu este semnată digital, macOS avertizează o singură dată).
- **Windows:** descarcă `eFactura-Sync-windows-x64.exe` și pune-l unde vrei (de exemplu pe
  Desktop). La prima pornire, la avertizarea SmartScreen: **Mai multe informații → Rulează
  oricum**.

**Prima pornire**

Se deschide browserul cu un asistent în 3 pași:
1. ce ai nevoie (rezumatul de mai sus);
2. **Client ID, Client secret, CIF-ul firmei** (doar cifre) și **dosarul pentru facturi** —
   implicit `Documents/Facturi e-Factura`; apasă *Alege…* ca să alegi altul;
3. **autentificarea la ANAF**: apasă *Începe autentificarea*, deschide adresa afișată,
   alege certificatul când browserul îl cere, iar după redirecționare copiază adresa
   completă din bara de adrese (pagina poate să nu se încarce — e normal) și lipește-o
   în aplicație → *Finalizează*.

**Utilizare zilnică**

Pornește aplicația (dublu-click) → se deschide pagina *Acasă* → **Sincronizează**. Facturile
primite din ultimele 60 de zile apar ca PDF în dosarul ales, pe ani și luni, iar pagina
*Facturi* le listează cu buton *PDF*. Fișierul `invoices.csv` din dosar este pentru
contabil. Dacă aplicația rulează deja, un nou dublu-click doar redeschide pagina. Se
închide din *Setări → Închide aplicația* sau singură după 30 de minute de inactivitate.

**Actualizare:** descarcă versiunea nouă și înlocuiește aplicația. Setările și facturile
descărcate rămân (setările în dosarul `.anaf_invoices` din dosarul tău de utilizator).

**Probleme frecvente**

- *ANAF a refuzat accesul (access_denied)*: certificatul nu a fost prezentat. Urmează
  verificările afișate în aplicație (token USB conectat, fereastra de alegere a
  certificatului, autentificare mai întâi pe pfinternet.anaf.ro în același browser).
- *Sesiunea ANAF a expirat*: autentificarea ține ~90 de zile; repet-o din *Setări*.
- Setările, jurnalul (`ui.log`) și baza de date sunt în `~/.anaf_invoices`
  (Windows: `C:\Users\<nume>\.anaf_invoices`). Datele nu pleacă nicăieri în afară de ANAF.

### 🇬🇧 English

**What you need (once, before installing)**

1. **A qualified digital certificate** registered in SPV for your company, with e-Factura enabled.
2. **An OAuth application registered with ANAF**: anaf.ro → Online services → Developer
   application registration → log in to SPV → new OAuth profile with any name, **Callback URL
   `https://localhost/callback`** and the **EFACTURA** service ticked → *Generate*. Keep the
   Client ID and Client secret; the app asks for them on first launch.

**Install**

- **macOS:** download `eFactura-Sync-macos-arm64.zip` (Apple Silicon) or
  `eFactura-Sync-macos-x86_64.zip` (Intel) from
  [Releases](https://github.com/ximi/efactura-sync/releases/latest), unzip, move
  `eFactura Sync.app` to *Applications*. **First time: right-click → Open → Open** (the app
  is not code-signed; macOS warns once).
- **Windows:** download `eFactura-Sync-windows-x64.exe`, put it anywhere. On the SmartScreen
  prompt: **More info → Run anyway**.

**First launch** — your browser opens with a 3-step setup: what you need; Client ID, Client
secret, company CIF and the invoices folder (default `Documents/Facturi e-Factura`, *Choose…*
to change); ANAF authentication (open the shown address, pick your certificate, paste the
full redirected address back — the page itself may not load, that's expected).

**Daily use** — launch the app → *Home* → **Synchronize**. Received invoices from the last
60 days land as PDFs in your folder by year/month; the *Invoices* page lists them with a
*PDF* button; `invoices.csv` is for your accountant. Launching again while it runs just
reopens the page. Quit from *Settings → Quit the app*, or it exits after 30 idle minutes.

**Update** — download the new version and replace the app; settings and invoices stay.

**Common problems** — *access_denied*: the certificate wasn't presented; follow the checks
shown in-app. *Session expired*: authentication lasts ~90 days; redo it in *Settings*.
Settings, `ui.log` and the database live in `~/.anaf_invoices`. Nothing leaves your
computer except requests to ANAF.

---

## For developers

The sections below cover the command-line tool and the code.

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
  "environment": "prod",
  "base_dir": "~/Documents/Facturi e-Factura"
}
```

- `cif` — your company fiscal code, numeric, **no `RO` prefix** and no spaces.
- `environment` — `prod` (default) for real invoices; `test` to validate a new setup.
- `base_dir` — where PDFs are filed (optional; defaults to `~/Documents/Facturi e-Factura`).

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

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
python anaf_invoices.py ui          # web UI (or: python -m efactura_sync)
```

The engine lives in `efactura_sync/core.py` and reports progress as events;
`efactura_sync/cli.py` is the terminal front end; `efactura_sync/web/` is the
browser UI. `anaf_invoices.py` is a thin compatibility shim.

### Building the desktop bundles

```bash
pyinstaller efactura_sync.spec      # dist/eFactura Sync.app  or  dist/eFactura-Sync.exe
```

Tagging a commit `vX.Y.Z` makes GitHub Actions build all three bundles and attach
them to a Release (`.github/workflows/release.yml`).

## Files & locations

| Path | Purpose |
|------|---------|
| `~/.anaf_invoices/config.json` | credentials & settings (chmod 600) |
| `~/.anaf_invoices/tokens.json` | OAuth tokens (chmod 600) |
| `~/.anaf_invoices/invoices.db` | dedup + sync state (SQLite) |
| `base_dir/YYYY/MM/…` | filed PDFs / XML / ZIP |
| `base_dir/invoices.csv` | accounting index |
