# Roadmap

Agreed work, in order. Each work package (WP) is independently check-green
(`python test_anaf_invoices.py` → all pass, CLI smoke) and shippable on its own.
Dates are when the decision was made.

## Decisions (2026-09-17)

- **Goal:** release for others, macOS + Windows, usable without a terminal.
- **UI:** local web UI (Flask), served on 127.0.0.1 only, opened in the default browser.
- **Distribution:** PyInstaller bundles (macOS arm64 + x86_64 `.app`, Windows `.exe`)
  built by GitHub Actions on tagged releases, attached to GitHub Releases.
- **Signing:** v1 ships unsigned; README documents the one-time Gatekeeper /
  SmartScreen bypass. Sign later if adoption warrants.
- **Language:** Romanian default, English toggle (translation table; every string twice).
- **Structure:** restructure the single file into a package `efactura_sync/`
  (`core.py`, `cli.py`, `web/`). `anaf_invoices.py` stays as a thin shim so existing
  cron lines keep working.
- **OAuth:** manual paste of the redirected URL stays (ANAF requires an `https`
  callback; auto-capture is out of scope). The UI makes the paste a form field and
  renders the `access_denied` diagnosis.

## Progress

- WP1 done 2026-09-17 (`f05626f`). WP2 done 2026-09-17 (`3a97598`). WP3 done 2026-09-17.
- Found live during WP3: `ui` refused to start without a config (exit 2) — fixed;
  the UI now always starts so the wizard can run. Regression test in `test_web.py`.

## WP1 — Engine extraction (½–1 day)

- Move logic into `efactura_sync/core.py` unchanged; `cli.py` owns argparse and all
  printing; `anaf_invoices.py` becomes `from efactura_sync.cli import main`.
- `sync()` becomes a generator of typed events (`SyncStarted`, `InvoiceDone`,
  `Duplicate`, `PdfFailed`, `SyncError`, `SyncFinished`). CLI prints them; the UI
  streams them. No `print()` left in core.
- `auth` splits into `begin_auth() -> (url, pending)` and
  `complete_auth(pending, pasted)`; CLI keeps the `input()`.
- Tests: existing suite passes unchanged; new event-stream tests drive `sync()`
  against a fake ANAF (monkeypatched `api_get` / `xml_to_pdf`) — new / duplicate /
  pdf-failed / error paths.

## WP2 — Web UI core (1–1½ days)

- Flask app in `efactura_sync/web/`: `/` dashboard, `/facturi` table (filter by
  month/supplier, open PDF, open folder), `/sync` (POST; one sync at a time; progress
  via SSE from the WP1 event generator on a background thread), `/setari`,
  `/autentificare` (URL + paste field + error hints), `/iesire` (quit).
- Safety: bind 127.0.0.1 only; CSRF token on every POST; Origin check; the client
  secret is never rendered back (masked); idle auto-exit after 30 min; `ui`
  subcommand opens the browser.
- Templates: Jinja, small inline CSS, no CDN (works offline). `strings.py` holds
  ro/en; language stored in config, `?lang=` toggle.
- Tests: Flask test client per route; SSE stream against the fake engine; CSRF
  rejection.

## WP3 — First-run wizard + copy (½–1 day)

- Missing config → wizard: (1) what you need + ANAF registration steps with links,
  (2) credentials, CIF, environment, folder, (3) authentication. Friendly validation.
- One error-mapping helper: raw HTTP statuses / English internals never reach the UI.
  Seen live in WP2 (2026-09-17): a sync with no tokens logs "Not authenticated. Run
  `auth` first." and "Paginated listing unavailable (…); using legacy endpoint." —
  both English internals. Map `ConfigError`/`RuntimeError` classes to ro/en copy with
  the next action ("Autentifică-te în Setări").
- Windows-safe file opening (`os.startfile`) and no-op `chmod`.

## WP4 — Packaging + CI (1 day)

- PyInstaller spec (`--windowed`, bundle templates/static), entry
  `efactura_sync/__main__.py` → `ui` mode. Version constant + `--version`.
- GitHub Actions: tests on push (ubuntu/macos/windows); on tag `v*` build matrix
  macos-14 (arm64), macos-13 (x86_64), windows-latest; upload artifacts to the Release.
- README end-user section (RO + EN): download, bypass dialogs, first run.

## WP5 — Polish (½ day)

- Verify Quit + idle exit on both OS. Log file in the config dir. "Copy diagnostic
  info" button (versions, OS, last error — never secrets).

## Risks to accept before starting

1. **Unsigned binaries.** Scary first-run dialogs; some managed Windows machines block
   unsigned executables outright. Mitigation: document; sign later.
2. **Antivirus false positives** are common for PyInstaller `.exe`. Mitigation:
   document; signing reduces it.
3. **Separate macOS builds** (arm64 + x86_64) rather than one universal binary,
   because of lxml wheels. Two downloads for Mac users.
4. **Local server attack surface.** A malicious web page could target
   `127.0.0.1:port`. Mitigation in WP2: CSRF tokens, Origin check, secret never
   echoed, no file paths accepted from the client.
5. **Listing field names still unverified** against live ANAF (blocked by the
   certificate issue). A follow-up fix after the first real sync is likely.
6. **Two languages** means every string is maintained twice (chosen knowingly).

## Deferred (from the 2026-09-17 security review)

- Token storage in macOS Keychain / Windows Credential Manager (currently `0600`
  JSON, same model as `aws`/`gcloud`).
- Size cap on downloaded ZIP/XML (source is ANAF, low likelihood).
- `--base-dir` / `ANAF_BASE_DIR` override — subsumed by the Settings page.
