"""Multi-request setup extends, rather than replaces, the fixed-contract launcher."""

import sys
from pathlib import Path

import pytest
import yaml
from test_shared_web_launcher import project as project
from test_shared_web_launcher import setup


def test_multi_has_no_fixed_contract_and_keeps_tools_restricted(project):
    root, context, _, node = project
    (root / "scripts/web_multi_server.py").write_text("# NETWORK_CONFIG\n", encoding="utf-8")
    result = setup.prepare(root, context, None, "network_admin", node, sys.executable, multi=True)
    assert result["mode"] == "multi"
    assert result["contract"] is None and result["intent_file"] is None
    rows = yaml.safe_load(Path(result["patch"]).read_text(encoding="utf-8"))
    disabled = {r["id"] for r in rows if r.get("disabled")}
    assert set(setup.REQUIRED_DISABLED_ROWS) <= disabled
    bridge = rows[-2]["insert"][0]["config"]
    assert bridge["args"] == [str(root / "scripts/web_multi_server.py")]
    assert "NETWORK_CONFIRMED_INTENT" not in bridge["env"]
    assert not (Path(result["run_setup"]) / "confirmed.json").exists()


def test_multi_rejects_fixed_contract(project):
    root, context, contract, node = project
    with pytest.raises(ValueError, match="fixed contract"):
        setup.prepare(root, context, contract, "network_admin", node, sys.executable, multi=True)


def test_multi_cli_preflight_only(project, monkeypatch, capsys):
    root, context, _, node = project
    (root / "scripts/web_multi_server.py").write_text("# NETWORK_CONFIG\n", encoding="utf-8")
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(
        sys, "argv", ["prepare_web.py", "--context", str(context), "--multi", "--node", str(node)]
    )
    stages = []
    monkeypatch.setattr(setup, "preflight", lambda cfg, stage, *args, **kwargs: stages.append(stage))
    assert setup.main() == 0
    assert stages == ["cba", "tsa"]
    assert '"mode": "multi"' in capsys.readouterr().out
