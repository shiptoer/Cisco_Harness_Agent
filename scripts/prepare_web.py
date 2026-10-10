"""Prepare an isolated web profile and runner config; never call a model."""

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import yaml

from harness_agent.harness.roles import ROLE_NAMES, role_level
from harness_agent.runner.context_check import context_violations
from harness_agent.runner.dsh import (
    REQUIRED_DISABLED_ROWS,
    DshSetupError,
    LocalConfig,
    build_patch,
    preflight,
)
from harness_agent.runner.intent_contract import ConfirmedIntent, load_confirmed_intent
from harness_agent.runner.model import ModelConfigError, load_model_config

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.0-rc.1"


def selected_file(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Expected a regular file")
    return path


def prepare(root, context, contract, role, node, python_exe):
    root = Path(root).resolve(strict=True)
    context = selected_file(root, context)
    contract = selected_file(root, contract)
    node = selected_file(root, node)
    python_exe = selected_file(root, python_exe)
    role_level(role)
    violations = context_violations(context)
    if violations:
        raise ValueError("Invalid context: " + "; ".join(f"{v.code}@{v.path}" for v in violations))
    confirmation = ConfirmedIntent.model_validate_json(contract.read_text(encoding="utf-8-sig"))
    load_confirmed_intent(contract, confirmation.intent, context)
    model = load_model_config(root / "config/model.toml")
    package = root / "spikes/dsh_mcp/node_modules/@deepseek-ai/dsh"
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if metadata.get("version") != VERSION:
        raise ValueError(f"Install the pinned dsh {VERSION} with npm ci in spikes/dsh_mcp")
    entry = selected_file(root, package / "lib/bin.js")
    bridge = selected_file(root, "scripts/web_pipeline_server.py")
    if "NETWORK_CONFIG" not in bridge.read_text(encoding="utf-8"):
        raise ValueError("Update web_pipeline_server.py from the shared launcher bundle")

    # Snapshot selected bytes so later source edits cannot change this launch's context/contract.
    out = root / "runs/web-launcher" / uuid4().hex
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / "context.json"
    confirmed = out / "confirmed.json"
    snapshot.write_bytes(context.read_bytes())
    confirmed.write_bytes(contract.read_bytes())
    load_confirmed_intent(confirmed, confirmation.intent, snapshot)
    (out / "intent.txt").write_text(confirmation.intent, encoding="utf-8", newline="\n")

    base = [{"id": row, "disabled": True} for row in REQUIRED_DISABLED_ROWS]
    base.append(
        {
            "insert": [
                {
                    "id": "mcp-shared-network",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {
                        "transport": "stdio",
                        "serverName": "harness",
                        "command": str(python_exe),
                        "args": [],
                        "env": {},
                        "toolCallTimeoutMs": 60000,
                        "failOnStartupError": True,
                    },
                }
            ]
        }
    )
    base_path = out / "base.patch.yml"
    base_path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8", newline="\n")
    config_path = out / "runner.toml"

    def quote(value):
        return json.dumps(str(value), ensure_ascii=False)

    config_path.write_text(
        "[dsh]\ncommand = "
        + json.dumps([str(node), str(entry)], ensure_ascii=False)
        + "\nbase_patch = "
        + quote(base_path)
        + '\nprompt_via = "stdin"\nextra_args = []\ntimeout_s = 300\nrequires_api_key = true\n'
        + "\n[python]\nexe = "
        + quote(python_exe)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg = LocalConfig.load(config_path)
    cfg.model_file = root / "config/model.toml"
    cfg.model()
    build_patch(cfg, "cba", snapshot, role, out)
    web = copy.deepcopy(base)
    web[-1]["insert"][0]["config"] = {
        "transport": "stdio",
        "serverName": "network_pipeline",
        "command": str(python_exe),
        "args": [str(bridge)],
        "cwd": str(root),
        "toolCallTimeoutMs": 900000,
        "failOnStartupError": True,
        "env": {
            "DEEPSEEK_API_KEY": {"__jsExpr": "process.env.DEEPSEEK_API_KEY"},
            "NETWORK_CONTEXT": str(snapshot),
            "NETWORK_CONFIRMED_INTENT": str(confirmed),
            "NETWORK_CONFIG": str(config_path),
            "NETWORK_ROLE": role,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    }
    web.append(model.patch_row())
    patch = out / "web.patch.yml"
    patch.write_text(
        yaml.safe_dump(web, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "patch": str(patch),
        "config": str(config_path),
        "node": str(node),
        "entry": str(entry),
        "context": str(snapshot),
        "contract": str(confirmed),
        "role": role,
        "intent_file": str(out / "intent.txt"),
        "run_setup": str(out),
        "home_id": hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12],
        "model": model.describe(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--role", choices=ROLE_NAMES, default="access_operator")
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    try:
        result = prepare(ROOT, args.context, args.contract, args.role, args.node, sys.executable)
        cfg = LocalConfig.load(result["config"])
        for stage in ("cba", "tsa"):
            preflight(
                cfg,
                stage,
                Path(result["context"]),
                args.role,
                Path(result["run_setup"]),
            )
    except (OSError, ValueError, DshSetupError, ModelConfigError, subprocess.SubprocessError) as exc:
        # Do not echo raw validation inputs or credentials into terminal logs.
        print(
            f"Setup failed ({type(exc).__name__}). Check files, schema/hash, dsh version and dependencies.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
