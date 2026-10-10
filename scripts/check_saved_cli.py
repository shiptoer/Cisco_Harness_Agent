"""Check saved results and exact CLI without calling the model."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

GOLDEN = {
    "clean-vlan30": {
        "SW2.cfg": (
            "vlan 30\n"
            " name FINANCE\n"
            "interface GigabitEthernet1/1\n"
            " switchport mode access\n"
            " switchport access vlan 30\n"
            "end\n"
        ),
    },
    "clean-acl-entry": {
        "SW1.cfg": (
            "ip access-list extended SERVERS-PROTECT\n"
            " 60 permit tcp 10.10.20.0 0.0.0.255 "
            "host 10.10.50.20 eq 22\n"
            "end\n"
        ),
    },
    "clean-static-route": {
        "R1.cfg": ("ip route 10.20.0.0 255.255.0.0 10.0.12.2\nend\n"),
    },
}

EXPECTED_RESULT = {
    "status": "ok",
    "verdict": "accept",
    "stopped_by": None,
    "error_code": None,
    "leak_attempt_detected": False,
    "leak_in_package": False,
}


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def check_case(run_dir: Path, expected_files: dict[str, str]) -> list[str]:
    """Return problems; an empty list means all checks passed."""
    problems = []
    try:
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            return ["result.json must contain an object"]

        for key, expected in EXPECTED_RESULT.items():
            if key not in result or result[key] != expected:
                problems.append(f"RESULT_FIELD: {key}")

        candidate = run_dir / "candidate"
        actual_files = {p.relative_to(candidate).as_posix(): p for p in candidate.rglob("*") if p.is_file()}

        missing = set(expected_files) - set(actual_files)
        extra = set(actual_files) - set(expected_files)

        for name in sorted(missing):
            problems.append(f"MISSING_FILE: {name}")
        for name in sorted(extra):
            problems.append(f"EXTRA_FILE: {name}")

        for name, expected in expected_files.items():
            if name not in actual_files:
                continue
            actual = actual_files[name].read_text(encoding="utf-8")
            if normalize(actual) != normalize(expected):
                problems.append(f"CLI_MISMATCH: {name}")

    except (OSError, ValueError) as exc:
        problems.append(f"READ_ERROR: {type(exc).__name__}: {exc}")

    return problems


def self_test() -> int:
    """Use temporary files only; never modify saved run results."""
    expected = GOLDEN["clean-vlan30"]

    with tempfile.TemporaryDirectory() as temp:
        run_dir = Path(temp)
        candidate = run_dir / "candidate"
        candidate.mkdir()
        (run_dir / "result.json").write_text(json.dumps(EXPECTED_RESULT), encoding="utf-8")
        cfg = candidate / "SW2.cfg"
        correct = expected["SW2.cfg"]
        cfg.write_text(correct, encoding="utf-8")

        problems = check_case(run_dir, expected)
        if problems:
            print("FAIL: correct fixture was rejected:", problems)
            return 1
        print("PASS: correct fixture accepted")

        wrong = correct.replace("vlan 30\n", "vlan 31\n", 1)
        cfg.write_text(wrong, encoding="utf-8")

        problems = check_case(run_dir, expected)
        if problems != ["CLI_MISMATCH: SW2.cfg"]:
            print("FAIL: wrong VLAN not detected as expected:", problems)
            return 1
        print("PASS: wrong VLAN detected")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.runs is None:
        ap.error("provide --runs or --self-test")

    passed = 0
    for case_name, expected_files in GOLDEN.items():
        # These three cases currently use run_id equal to their case name.
        problems = check_case(args.runs / case_name, expected_files)
        ok = not problems
        passed += int(ok)
        print(f"{case_name}: {'PASS' if ok else 'FAIL'}")
        for problem in problems:
            print(f"  {problem}")

    print(f"\n{passed}/{len(GOLDEN)} cases passed")
    return 0 if passed == len(GOLDEN) else 1


if __name__ == "__main__":
    raise SystemExit(main())
