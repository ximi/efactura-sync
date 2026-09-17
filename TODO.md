# Roadmap

Agreed future work. Items here were reviewed and consciously deferred; the date is
when the decision was made.

## Deferred from the pre-release security review (2026-09-17)

- **Token storage in macOS Keychain.** Tokens live in `~/.anaf_invoices/tokens.json`
  at `0600`, the same model as the `aws`/`gcloud` CLIs. Keychain (via `security`
  or `keyring`) would be a strict upgrade for a single-user Mac. Low priority.
- **Bound memory for downloaded ZIP/XML.** `descarcare` responses are read fully into
  memory. The source is ANAF, not the supplier directly, so a zip bomb is unlikely;
  a size cap on the response (e.g. 50 MB) would close it cheaply.

## Nice-to-have

- `--base-dir` / `ANAF_BASE_DIR` override for one-off syncs into another folder.
- Optional local HTTPS catcher for the OAuth callback (`auth --port`), to avoid the
  manual paste. Needs a self-signed cert and a matching registered callback URL.
- Verify the listing-response field names (`mesaje`, `numar_total_pagini`, `id`,
  `tip`, `data_creare`) against a real `test`-environment response once `auth`
  works; they were taken from ANAF docs, not observed live.
