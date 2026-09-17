"""WP4: behaviour a double-clicked bundle relies on.

  - `python -m efactura_sync` (and the frozen exe) defaults to the `ui` command;
  - /ping identifies a running instance so a second launch reuses it;
  - run_ui opens the browser to an existing instance instead of starting another;
  - the PyInstaller spec bundles the templates (otherwise every page 500s).
"""

import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("ANAF_CONFIG_DIR", tempfile.mkdtemp(prefix="anaf_pkg_"))

from efactura_sync import __main__ as entry, __version__, cli, core, web  # noqa: E402


def test_main_module_defaults_to_ui(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_ui", lambda cfg, open_browser=True: seen.append("ui"))
    assert entry.run([]) == 0
    assert seen == ["ui"]


def test_main_module_passes_explicit_command(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_status", lambda cfg: seen.append("status"))
    monkeypatch.setattr(core, "load_config", lambda env_override=None: {"cif": "1", "environment": "test", "base_dir": "x"})
    assert entry.run(["status"]) == 0
    assert seen == ["status"]


def test_ping_identifies_the_app():
    app = web.create_app()
    r = app.test_client().get("/ping")
    assert r.status_code == 200
    assert r.get_json() == {"app": "efactura-sync", "version": __version__}


def test_run_ui_reuses_running_instance(monkeypatch):
    opened = []
    monkeypatch.setattr(web, "_already_running", lambda port: True)
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))

    def must_not_start(*a, **k):
        raise AssertionError("a second server must not be started")

    monkeypatch.setattr(web, "_free_port", must_not_start)
    web.run_ui(None, port=8765)
    assert opened == ["http://127.0.0.1:8765/"]


def test_already_running_is_false_on_a_closed_port():
    assert web._already_running(1) is False      # nothing listens on port 1


def test_spec_bundles_templates_and_uses_version():
    spec = Path(__file__).with_name("efactura_sync.spec").read_text(encoding="utf-8")
    assert "efactura_sync/web/templates" in spec
    assert re.search(r"__version__", spec)
    assert "console=False" in spec


def test_release_workflow_builds_all_three_targets():
    wf = Path(__file__).parent / ".github" / "workflows" / "release.yml"
    text = wf.read_text(encoding="utf-8")
    assert "macos-arm64" in text and "macos-x86_64" in text and "windows-x64" in text
    assert "efactura_sync.spec" in text
    assert "tags:" in text
