"""Command-line front end. The only module that prints or prompts."""

import argparse
import sys
from pathlib import Path

from . import __version__, core


def cmd_auth(cfg: dict) -> None:
    pending = core.begin_auth(cfg)

    print("\n=== ANAF e-Factura authentication ===\n")
    print("1. Open this URL in a browser where your ANAF qualified certificate is available:\n")
    print(f"   {pending.url}\n")
    print("2. Authenticate with your certificate and authorize the application.")
    print(f"3. You will be redirected to {cfg['redirect_uri']} (the page may fail to load — "
          "that is fine).")
    print("4. Copy the FULL redirected URL from the address bar (or just the 'code' value).\n")

    pasted = input("Paste the redirected URL or code here: ")
    core.complete_auth(cfg, pending, pasted)
    print("\n✓ Authentication successful. Tokens stored in "
          f"{core.TOKENS_PATH}.\n  You can now run: anaf_invoices.py sync")


def cmd_sync(cfg: dict) -> None:
    for ev in core.sync(cfg):
        if isinstance(ev, core.SyncStarted):
            print(f"Listing received invoices (last {ev.lookback_days} days, "
                  f"environment={ev.environment}, cif={ev.cif})...")
        elif isinstance(ev, core.Notice):
            print(f"  {ev.message}")
        elif isinstance(ev, core.MessagesListed):
            print(f"  {ev.count} message(s) returned by ANAF.")
        elif isinstance(ev, core.Duplicate):
            print(f"  · skipped {ev.download_id}: {ev.reason}")
        elif isinstance(ev, core.PdfFailed):
            print(f"  ! PDF conversion failed for {ev.invoice_id}: {ev.message}")
        elif isinstance(ev, core.InvoiceDone):
            status = "PDF" if ev.pdf_ok else "XML-only"
            print(f"  ✓ {ev.invoice_id} — {ev.supplier_name} ({ev.issue_date}) [{status}]")
        elif isinstance(ev, core.SyncError):
            print(f"  ✗ error on download_id={ev.download_id}: {ev.message}")
        elif isinstance(ev, core.SyncFinished):
            print("\nSummary:")
            print(f"  new invoices : {ev.new}")
            print(f"  duplicates   : {ev.duplicates}")
            print(f"  pdf-failed   : {ev.pdf_failed} (XML kept)")
            print(f"  errors       : {ev.errors}")
            print(f"  files in     : {ev.base_dir}")


def cmd_status(cfg: dict) -> None:
    r = core.status_report(cfg)
    print("=== ANAF e-Factura — status ===")
    print(f"  environment      : {r.environment}")
    print(f"  cif              : {r.cif}")
    print(f"  last sync        : {r.last_run or 'never'}")
    print(f"  total invoices   : {r.total}")
    print(f"  this month       : {r.this_month}")
    print(f"  pdf failures     : {r.pdf_failed}")
    print(f"  duplicate hits   : {r.duplicate_hits}")
    print(f"  base directory   : {r.base_dir}")
    print(f"  database         : {r.db_path}")
    if r.recent:
        print("\n  most recent:")
        for inv in r.recent:
            flag = "" if inv.pdf_ok else "  [XML-only]"
            print(f"    {inv.issue_date or '????-??-??'}  {inv.invoice_id:<20} "
                  f"{inv.supplier_name}{flag}")


def cmd_ui(open_browser: bool = True, port: int | None = None) -> None:
    from . import web  # Flask is only imported when the UI is actually requested
    kwargs = {"open_browser": open_browser}
    if port:
        kwargs["port"] = port
    web.run_ui(**kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anaf_invoices.py",
        description="Download and organize received ANAF e-Factura invoices as PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="Path to config.json (overrides default).")
    parser.add_argument("--env", choices=list(core.REST_BASE),
                        help="Override environment (prod/test).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="One-time OAuth login (manual code paste).")
    sub.add_parser("sync", help="Download new received invoices and convert to PDF.")
    sub.add_parser("status", help="Show download statistics.")
    ui = sub.add_parser("ui", help="Open the local web interface in your browser.")
    ui.add_argument("--no-browser", action="store_true",
                    help="Start the server without opening a browser window.")
    ui.add_argument("--port", type=int, default=None,
                    help="Local port to serve on (default 8765, next free if taken).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config:
        # One directory for everything: config, tokens and the dedup DB belong together.
        config_path = Path(args.config).expanduser()
        core.set_config_dir(config_path.parent)
        core.CONFIG_PATH = config_path
    if args.env:
        core.ENV_OVERRIDE = args.env        # honoured by every later load_config()

    try:
        core.ensure_config_dir()
        if args.command == "ui":
            # The UI has a first-run wizard, so a missing/invalid config must not
            # stop it from starting (found live 2026-09-17).
            cmd_ui(open_browser=not args.no_browser, port=args.port)
            return 0
        cfg = core.load_config(env_override=args.env)
        if args.command == "auth":
            cmd_auth(cfg)
        elif args.command == "sync":
            cmd_sync(cfg)
        elif args.command == "status":
            cmd_status(cfg)
    except core.ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0
