"""Entry point for `python -m efactura_sync` and for the PyInstaller bundles.

With no arguments it opens the web UI — that is what a double-click means.
Any explicit subcommand (`sync`, `status`, `auth`, `ui --no-browser`) still works,
so the same bundle can be driven from a terminal or a scheduler.
"""

import sys

# Absolute import on purpose: PyInstaller executes this file as a top-level
# script (no parent package), where a relative import fails at launch.
# Found by the bundle smoke test, 2026-09-17.
from efactura_sync.cli import main


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return main(argv or ["ui"])


if __name__ == "__main__":
    sys.exit(run())
