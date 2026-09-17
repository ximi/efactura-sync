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

- WP1 done 2026-09-17 (`f05626f`). WP2 done 2026-09-17 (`3a97598`). WP3 done
  2026-09-17 (`801df42`). WP4 done 2026-09-17 (macOS bundle built and smoke-tested
  locally; Windows and Intel builds run only in CI).
- Found live during WP3: `ui` refused to start without a config (exit 2) — fixed;
  the UI now always starts so the wizard can run. Regression test in `test_web.py`.
- Found by the WP4 bundle smoke test: PyInstaller runs `__main__.py` as a top-level
  script, so its relative import crashed the app at launch; now absolute.
- v0.1.0 tagged 2026-09-17: CI green on ubuntu/macos/windows; Release built all
  three bundles on the first run (`macos-15-intel` label confirmed).
- **Still unverified:** the Windows `.exe` at runtime (`os.startfile`, no-console
  logging) — it builds, nobody has launched it; antivirus/SmartScreen behaviour on
  the unsigned `.exe`; the Intel-Mac `.app` at runtime.

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

## Review 2026-09-17 (4 passes: security, code quality/tests, UX/copy, ops)

- Batch A (correctness) done: ANAF `data_creare` parsed as YYYYMMDDHHMM; filename
  collisions disambiguated by download id; content duplicates remembered (no
  re-download); corrupt config/tokens degrade gracefully; base_dir validated;
  watchdog grace after a run + streams count as activity; refresh-once after 401;
  exhausted retries → `anaf_http`; legacy fallback only on HTTP errors; CSV written
  before commit (+ `csv_locked` message); instance probe across the port range;
  startup logged; per-run SSE buffers; non-ASCII csrf → 403; IPv6 host; schema once
  per process + `schema_version`; CLI/web render every event; `--env`/`--config`
  coherent; open-folder creates the folder; quit refused mid-sync; frozen logging
  for all commands; quiet folder pickers.
- Batch B (security hardening) done: X-Frame-Options/CSP frame-ancestors, nosniff,
  no-referrer; `_safe_next` allows only `/path?query` (no backslash/CRLF); a pasted
  callback must carry our `state` (bare codes still PKCE-bound); `/ping` and
  cross-site GETs no longer keep the server alive; config/tokens created O_EXCL 0600;
  technical detail capped; workflows least-privilege (write only in the release
  job), third-party action SHA-pinned, tag must equal `__version__`; app hidden
  from the Dock (LSUIElement).
- Batch C (UX) pending — see the review list.

## Deferred (from the 2026-09-17 security review)

- Lockfile / hash-pinned dependencies for reproducible release builds.
- Windows: `Documents` may be OneDrive-redirected (query the known folder); the
  `0600` model is a no-op on NTFS (document; consider DPAPI for tokens).
- A failed matrix leg blocks the whole release; recover with "re-run failed jobs".
- CSRF token is readable by any same-user local process — accepted: such a
  process can read `config.json` directly anyway.
- `schema_version` is recorded; real migrations arrive with the first schema change.

- Token storage in macOS Keychain / Windows Credential Manager (currently `0600`
  JSON, same model as `aws`/`gcloud`).
- Size cap on downloaded ZIP/XML (source is ANAF, low likelihood).
- `--base-dir` / `ANAF_BASE_DIR` override — subsumed by the Settings page.
