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

# --- 3b. PROPOSAL-RECORD (EMPTY-EFFECTS TASK) REFUSAL ---------------------
check(pp.empty_effects_refusal(rec(kind="task", effects={})) is not None,
      "empty-effects task refused")
check(pp.empty_effects_refusal({"id": "r", "parents": ["p"], "kind": "task"}) is not None,
      "missing-effects task refused")
check(pp.empty_effects_refusal(rec(kind="task", effects={"config.timeout": 5})) is None,
      "task with effects passes")
check(pp.empty_effects_refusal(rec(kind="witness", effects={})) is None,
      "witness with empty effects NOT refused by this rule (the warning's job)")
check(pp.empty_effects_refusal(rec(kind="override", effects={"config.timeout": 45})) is None,
      "override with effects passes")
check(pp.empty_effects_refusal(rec(kind="override", effects={})) is None,
      "override is out of scope for this rule (task-only)")

# --- 3c. OVERRIDE-GROUNDING REFUSAL ---------------------------------------
# Universe mirrors the live poc-quilt-1 record, with its real parentage —
# the same shape scene.sh builds (genesis -> task-a/task-b -> override-1,
# witnesses on the side). Grounding is judged against this universe.
U = [
    rec(rid="genesis", parents=[], effects={"project": "poc-quilt-1", "status": "open"}),
    rec(rid="task-a", parents=["genesis"], effects={"config.timeout": 30, "feature.cache": "on"}),
    rec(rid="task-b", parents=["genesis"], effects={"config.timeout": 60, "feature.search": "on"}),
    rec(rid="override-1", kind="override", parents=["task-a", "task-b"],
        effects={"config.timeout": 45}, reason="captain: 45 is the safe value"),
    rec(rid="task-c", parents=["override-1"], effects={"feature.inherited": True}),
    rec(rid="task-d", parents=["task-b"], effects={"config.timeout": 99}),
    rec(rid="witness-1", kind="witness", parents=["genesis"], effects={"env.oracle_disk_pct": 93}),
    rec(rid="witness-2", kind="witness", parents=["override-1"], effects={"env.oracle_load1_x100": 17}),
]
# grounded override passes: descends from the frontier, rules on a key the
# ancestry set (config.timeout via task-a/task-b/override-1/task-d).
check(pp.override_grounding_refusal(
    rec(rid="o2", kind="override", parents=["task-c", "task-d", "witness-1", "witness-2"],
        effects={"config.timeout": 50}), U) is None,
    "grounded override passes (frontier parents, ancestry set the key)")
# the scene's own override shape passes against its own universe.
check(pp.override_grounding_refusal(
    rec(rid="o1", kind="override", parents=["task-a", "task-b"],
        effects={"config.timeout": 45}), U) is None,
    "scene override-1 shape passes grounding")
# grounding is transitive: feature.cache was set by task-a, three links up.
check(pp.override_grounding_refusal(
    rec(rid="o3", kind="override", parents=["task-c"],
        effects={"feature.cache": "off"}), U) is None,
    "override grounded through a transitive ancestor passes")
# M3 SHAPE REFUSED: the bare-observation draft — descending only from the
# witness that made the observation, it invents a ruling on config.timeout,
# a key that lineage never set. The observed key itself is grounded.
_m3 = pp.override_grounding_refusal(
    rec(rid="override-2", kind="override", parents=["witness-1"],
        effects={"config.timeout": 50, "env.oracle_disk_pct": 94},
        reason="timeout chosen to balance responsiveness and resource usage"), U)
check(_m3 is not None, "M3-shaped override refused (invented ruling from an observation)")
check(_m3 is not None and "config.timeout" in _m3,
      "M3 refusal names the ungrounded key (config.timeout)")
check(_m3 is not None and "env.oracle_disk_pct" not in _m3,
      "M3 refusal does NOT name the witness-grounded key (env.oracle_disk_pct)")
# override over a witness-observed key passes: the witness IS the grounding.
check(pp.override_grounding_refusal(
    rec(rid="o4", kind="override", parents=["witness-1"],
        effects={"env.oracle_disk_pct": 90}), U) is None,
    "override over a witness-observed key passes")
# an unknown parent contributes no grounding.
check(pp.override_grounding_refusal(
    rec(rid="o5", kind="override", parents=["no-such-record"],
        effects={"config.timeout": 1}), U) is not None,
    "override descending from an unknown parent is ungrounded")
# scope: tasks originate keys and witnesses report — neither is ever refused
# by this rule, even with keys nobody has ever set.
check(pp.override_grounding_refusal(
    rec(rid="t9", kind="task", parents=["witness-1"],
        effects={"brand.new.key": 1}), U) is None,
    "task with a brand-new key NOT refused (tasks originate keys)")
check(pp.override_grounding_refusal(
    rec(rid="w9", kind="witness", parents=["genesis"],
        effects={"env.brand_new": 1}), U) is None,
    "witness NOT refused by the grounding rule")
check(pp.override_grounding_refusal(
    rec(rid="o6", kind="override", parents=["task-a"], effects={}), U) is None,
    "empty-effects override is out of scope for this rule")

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
check(all(pp.empty_effects_refusal(r) is None for r in scene_records),
      "no scene record trips the empty-effects refusal")
# poc-scale-1 shapes (region/mode strings, feature bools) stay clean too.
check(pp.scan_secret(rec(effects={"config.region": "us-east-2", "config.mode": "eco"})) is None,
      "scale-project string values do not trip the secret scan")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
