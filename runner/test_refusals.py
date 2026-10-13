#!/usr/bin/env python3
"""Refusal tests for pp.py (runner-only; the fold is not involved).

Exercises every branch of the pre-push refusals as pure functions — no network,
no git — plus a regression that the refusals never fire on the records the live
scenes actually push. Prints PASS/FAIL per case; exits non-zero on any failure.

    python3 runner/test_refusals.py
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pp", HERE / "pp.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

passed = failed = 0


def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS — {msg}")
    else:
        failed += 1
        print(f"FAIL — {msg}" + (f" ({detail})" if detail else ""))


def rec(effects=None, reason=None, kind="task", parents=("p",), rid="r"):
    r = {"id": rid, "parents": list(parents), "kind": kind, "effects": effects or {}}
    if reason is not None:
        r["reason"] = reason
    return r


# --- 1. SECRET REFUSAL ----------------------------------------------------
# secret in reason
check(pp.scan_secret(rec(reason="rotate the key sk-abcdEFGH0123456789klmnopqrstuvwx")) is not None,
      "secret in reason refused (sk-)")
# secret nested in effects
check(pp.scan_secret(rec(effects={"db": {"password": "hunter2-prod-pw"}})) is not None,
      "secret nested in effects refused (key=value)")
# each known pattern class
for val, label in [
    ("sk-abcdEFGH0123456789klmnopqrstuvwx", "sk-"),
    ("ghp_ABCDEFGHIJKLMNOpqrstuvwx0123456789", "ghp_"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIA"),
    ("-----BEGIN RSA PRIVATE KEY-----", "PEM"),
    ("Bearer eyJhbGciOiJIUzI1Ni.abcdefghijklmnop", "bearer"),
]:
    check(pp.scan_secret(rec(effects={"k": val})) is not None, f"known pattern refused ({label})")
# password assignment by key name
check(pp.scan_secret(rec(effects={"api_key": "AKIAIOSFODNN7EXAMPLE"})) is not None,
      "credential-named key with real value refused")
# inline assignment in free text
check(pp.scan_secret(rec(reason="set password=hunter2 before deploy")) is not None,
      "inline password=... in reason refused")
# high-entropy token
check(pp.scan_secret(rec(effects={"k": "Zx9Qw3Vb7Nm2Ke5Rt8Yu1Ap4Sd6Fg0Hj"})) is not None,
      "high-entropy token refused")

# --- vault pointer PASSES (the sanctioned escape hatch) -------------------
check(pp.scan_secret(rec(effects={"db.password": "vault:staging-db"})) is None,
      "vault pointer passes under a credential-named key")
check(pp.scan_secret(rec(reason="credentials live at vault://prod/db")) is None,
      "vault:// pointer in reason passes")
# placeholders pass
check(pp.scan_secret(rec(effects={"password": "changeme"})) is None,
      "placeholder value under credential key passes")
check(pp.scan_secret(rec(effects={"token": "<your-token>"})) is None,
      "angle-bracket placeholder passes")

# --- 2. GENESIS GUARD -----------------------------------------------------
check(pp.genesis_decision([], False, 0, []) is not None,
      "genesis without --genesis refused (empty project)")
check(pp.genesis_decision([], False, 3, ["task-c", "task-d"]) is not None,
      "genesis without --genesis refused (non-empty, teaches frontier)")
check("task-c" in pp.genesis_decision([], False, 3, ["task-c", "task-d"]),
      "genesis refusal names the frontier records")
check(pp.genesis_decision([], True, 5, ["x"]) is not None,
      "genesis WITH --genesis on a non-empty project refused")
check(pp.genesis_decision([], True, 0, []) is None,
      "genesis WITH --genesis on an empty project passes")
check(pp.genesis_decision(["genesis"], False, 3, ["x"]) is None,
      "non-empty parents never trips the genesis guard")
check(pp._frontier_ids([
    {"id": "genesis", "parents": [], "orphan": False},
    {"id": "a", "parents": ["genesis"], "orphan": False},
    {"id": "b", "parents": ["genesis"], "orphan": False},
]) == ["a", "b"], "frontier = applied leaves (not the root)")

# --- 3. KIND DISCIPLINE ---------------------------------------------------
check(pp.witness_warnings(rec(kind="witness", effects={"config.timeout": 5})),
      "witness with non-observation key warns")
check(pp.witness_warnings(rec(kind="witness", effects={"env.disk_pct": 93})) == [],
      "witness with observation key (env.*) does not warn")
check(pp.witness_warnings(rec(kind="task", effects={"config.timeout": 5})) == [],
      "task is never warned by kind discipline")

# --- 4. REGRESSION: the live scenes' records never trip a refusal ---------
# Exactly the records runner/scene.sh and poc-quilt-1 push.
scene_records = [
    rec(rid="genesis", kind="task", parents=[], effects={"project": "poc-quilt-1", "status": "open"}),
    rec(rid="task-a", effects={"config.timeout": 30, "feature.cache": "on"}),
    rec(rid="task-b", effects={"config.timeout": 60, "feature.search": "on"}),
    rec(rid="task-d", effects={"config.timeout": 99}),
    rec(rid="task-c", effects={"feature.inherited": True}),
    rec(rid="override-1", kind="override", effects={"config.timeout": 45},
        reason="captain: 45 is the safe value"),
    rec(rid="witness-1", kind="witness", effects={"env.oracle_disk_pct": 93}),
    rec(rid="witness-2", kind="witness", effects={"env.oracle_load1_x100": 17}),
    rec(rid="demo", kind="task", parents=[], effects={"project": "demo-a1b2c3", "status": "open"}),
]
check(all(pp.scan_secret(r) is None for r in scene_records),
      "no scene record trips the secret scan")
check(all(pp.witness_warnings(r) == [] for r in scene_records),
      "no scene witness trips the kind-discipline warning")
# poc-scale-1 shapes (region/mode strings, feature bools) stay clean too.
check(pp.scan_secret(rec(effects={"config.region": "us-east-2", "config.mode": "eco"})) is None,
      "scale-project string values do not trip the secret scan")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
