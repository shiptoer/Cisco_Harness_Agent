"""Offline regression coverage for portable web setup."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("shared_web_setup", REPO / "scripts/prepare_web.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo with spaces"
    (root / "config").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts/web_pipeline_server.py").write_text("# NETWORK_CONFIG\n", encoding="utf-8")
    (root / "config/model.toml").write_bytes((REPO / "config/model.toml").read_bytes())
    package = root / "spikes/dsh_mcp/node_modules/@deepseek-ai/dsh"
    (package / "lib").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": setup.VERSION}), encoding="utf-8")
    (package / "lib/bin.js").write_text("// never executed\n", encoding="utf-8")
    node = root / "node.exe"
    node.write_bytes(b"not executed")
    context = root / "context.json"
    context.write_bytes((REPO / "data/contexts/campus-small-01.json").read_bytes())
    contract = root / "confirmed.json"
    import hashlib

    contract.write_text(
        json.dumps(
            {
                "intent": "Set description only.",
                "context_sha256": hashlib.sha256(context.read_bytes()).hexdigest(),
                "changes": [
                    {
                        "tool": "set_interface_description",
                        "args": {
                            "device": "SW2",
                            "interface": "GigabitEthernet1/2",
                            "text": "SALES-PC-03",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, context, contract, node


def launch(project):
    root, context, contract, node = project
    return setup.prepare(root, context, contract, "network_admin", node, sys.executable)


def test_setup_preserves_local_config_and_never_serializes_key(project, monkeypatch):
    root, context, _, _ = project
    local = root / "config/local.toml"
    local.write_bytes(b"existing local config")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-write-this-secret-value")
    result = launch(project)
    assert local.read_bytes() == b"existing local config"
    patch = yaml.safe_load(Path(result["patch"]).read_text())
    env = patch[-2]["insert"][0]["config"]["env"]
    assert env["DEEPSEEK_API_KEY"] == {"__jsExpr": "process.env.DEEPSEEK_API_KEY"}
    assert env["NETWORK_CONFIG"] == result["config"]
    snapshot = Path(result["context"])
    original = snapshot.read_bytes()
    context.write_bytes(b"changed after preparation")
    assert snapshot.read_bytes() == original
    for path in Path(result["run_setup"]).iterdir():
        assert b"never-write-this-secret-value" not in path.read_bytes()
    cfg = setup.LocalConfig.load(result["config"])
    assert cfg.prompt_via == "stdin"
    assert cfg.requires_api_key is True


@pytest.mark.parametrize("problem", ["hash", "schema", "role", "version", "bridge"])
def test_bad_setup_fails_before_writing_runtime_files(project, problem):
    root, context, contract, node = project
    role = "network_admin"
    if problem == "hash":
        data = json.loads(contract.read_text())
        data["context_sha256"] = "0" * 64
        contract.write_text(json.dumps(data))
    elif problem == "schema":
        data = json.loads(context.read_text())
        data["devices"][2]["interfaces"][6]["description"] = "X" * 241
        context.write_text(json.dumps(data))
    elif problem == "role":
        role = "unknown_role"
    elif problem == "version":
        path = root / "spikes/dsh_mcp/node_modules/@deepseek-ai/dsh/package.json"
        path.write_text('{"version":"0.0.0"}')
    else:
        (root / "scripts/web_pipeline_server.py").write_text("# old bridge\n")
    with pytest.raises(ValueError):
        setup.prepare(root, context, contract, role, node, sys.executable)
    assert not (root / "runs").exists()


def test_repeated_launch_does_not_overwrite_previous_patch(project):
    first = launch(project)
    original = Path(first["patch"]).read_bytes()
    second = launch(project)
    assert first["run_setup"] != second["run_setup"]
    assert first["home_id"] == second["home_id"]
    assert Path(first["patch"]).read_bytes() == original


def test_cli_checks_both_stages_without_launching_model(project, monkeypatch, capsys):
    root, context, contract, node = project
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_web.py",
            "--context",
            str(context),
            "--contract",
            str(contract),
            "--node",
            str(node),
            "--role",
            "network_admin",
        ],
    )
    calls = []

    def fake_preflight(cfg, stage, context, role, out):
        calls.append(stage)
        assert role == "network_admin"
        assert context.is_file()
        return []

    monkeypatch.setattr(setup, "preflight", fake_preflight)
    assert setup.main() == 0
    assert calls == ["cba", "tsa"]
    result = json.loads(capsys.readouterr().out)
    assert Path(result["patch"]).is_file()
