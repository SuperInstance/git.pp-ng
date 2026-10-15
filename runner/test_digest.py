#!/usr/bin/env python3
"""Format-v2 digest tests for pp.py (runner-side; offline, no network).

The runner is the writer of format v2 (docs/FORMAT-V2.md): `complete` is
born with a `digest` field. These tests pin the writer's contract as pure
functions — the same style as test_refusals.py, kept a separate suite so
the refusal total it guards stays literal:

  * the digest is "sha256:" + the referee's checkpoint-v2 record leaf —
    the leaf implementation is imported from referee/fold.py, never
    reimplemented in the runner, so the two cannot drift;
  * a record already carrying the correct digest keeps it (same object);
  * an absent, malformed, or stale claim is replaced by the digest of the
    content actually being written (never written through as-is);
  * attaching a digest cannot change what the record folds to: the fold
    never reads the field, so derive / checkpoint roots are identical
    with and without it;
  * the digest is computed over the final content — the value the
    refusals in cmd_complete already approved — including float64-domain
    splitter values and a reason.

    python3 runner/test_digest.py
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pp", HERE / "pp.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_rspec = importlib.util.spec_from_file_location(
    "fold_referee", HERE.parent / "referee" / "fold.py")
ref = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(ref)

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
    r = {"id": rid, "parents": list(parents), "kind": kind,
         "effects": effects or {}}
    if reason is not None:
        r["reason"] = reason
    return r


# --- the digest is the referee's leaf, inherited not reimplemented --------
r = rec(effects={"config.timeout": 45})
check(pp.born_digest(r) == "sha256:" + ref._record_leaf(r),
      "born_digest is exactly 'sha256:' + referee _record_leaf")
r2 = rec(effects={"config.timeout": 45}, reason="captain: 45 is the safe value",
         kind="override")
check(pp.born_digest(r2) == "sha256:" + ref._record_leaf(r2),
      "leaf covers reason (whole-record leaf, as checkpoints pin it)")
# float64-domain splitters: the leaf dialect must be the referee's, so an
# exponent-range float digests identically by construction.
r3 = rec(effects={"tiny": 1e-5, "tinier": 2.5e-7, "negzero": -0.0,
                  "big": 2**53 + 1})
check(pp.born_digest(r3) == "sha256:" + ref._record_leaf(r3),
      "float splitter values digest via the referee leaf")
check(pp.born_digest(r3).startswith("sha256:") and len(pp.born_digest(r3)) == 71,
      "digest shape is sha256:<64 hex>")

# --- attach: absent / malformed / stale claims are replaced ---------------
out = pp.attach_digest(r)
check(out["digest"] == pp.born_digest(r), "attach adds the correct digest")
check("digest" not in r, "attach does not mutate the caller's record")
out_bad = pp.attach_digest(dict(r, digest="sha256:not-hex"))
check(out_bad["digest"] == pp.born_digest(r),
      "malformed digest is replaced, never written through")
stale = dict(out, effects={"config.timeout": 99})
out_stale = pp.attach_digest(stale)
check(out_stale["digest"] == pp.born_digest(stale)
      and out_stale["digest"] != out["digest"],
      "stale digest (content moved on) is recomputed for the content written")

# --- attach: a correct digest is kept, byte-for-byte ----------------------
check(pp.attach_digest(out) is out,
      "a record already carrying the correct digest keeps it (same object)")

# --- the fold never reads the field ---------------------------------------
# genesis -> a -> b, one contested key; derive and roots must be identical
# with correct digests, with garbage digests, and with no digests at all.
S = [rec(rid="g", parents=[], effects={"k": 1}),
     rec(rid="a", parents=["g"], effects={"k": 2}),
     rec(rid="b", parents=["a"], effects={"k": 3})]
born = [pp.attach_digest(x) for x in S]
garbage = [dict(x, digest="sha256:" + "0" * 64) for x in S]
check(ref.derive(born) == ref.derive(S) == ref.derive(garbage),
      "derive is identical for born / v1 / garbage-digest sets")
check(ref.checkpoint_root(born, "b") == ref.checkpoint_root(S, "b"),
      "checkpoint root is identical for born and v1 sets")
cp = ref.build_checkpoint(born, "b")
check(ref.verify_checkpoint(born, cp) == "VERIFIED",
      "checkpoint built over born records verifies VERIFIED")
check(all(x["digest"] == pp.born_digest(x) for x in born),
      "every attached digest verifies against the referee leaf")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
