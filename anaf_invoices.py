#!/usr/bin/env python3
"""Compatibility entry point — the implementation lives in the efactura_sync package.

Kept so existing cron lines (`python anaf_invoices.py sync`) keep working.
"""

from efactura_sync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
