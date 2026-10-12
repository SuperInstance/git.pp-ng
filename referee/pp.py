#!/usr/bin/env python3
"""pp.py — collector/renderer for the git.pp-on-Artifacts proof of concept.

The record model (see fold.py): every unit of work is a *record* — a JSON
object {"id", "parents", "kind": task|witness|override, "effects",
"reason"?} stored as `record.json` in a git commit. Repos live on
Cloudflare Artifacts:

  * ONE project repo accumulates records over time: every commit on
    `main` carries that commit's record as its `record.json`.
  * ONE repo per task (an Artifacts fork of the project): the task's
    record is the `record.json` of its HEAD commit.

This module collects the records out of local checkouts of those repos,
runs the deterministic fold (fold.derive) over them, and renders the
result as a markdown projection. It can also verify checkpoints
(fold.verify_checkpoint) for a record position.

Usage:
  python3 pp.py collect project=DIR task=DIR [task=DIR ...]
      Print the collected records as a JSON list.
  python3 pp.py render  project=DIR task=DIR [...]   (or --records FILE)
      Print the markdown projection of the derived state.
  python3 pp.py verify  --at ID [--state FILE] (repos... | --records FILE)
      Print JSON: the ancestor-closure state at ID, whether an honest
      checkpoint at ID verifies, and (if --state given) whether the
      state in FILE verifies at ID.

Stdlib only. Requires `git` on PATH and the checkouts to exist locally.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fold  # noqa: E402


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def _git(repo_dir, *args):
    r = subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo_dir}: {r.stderr.strip()}")
    return r.stdout


def _show_record(repo_dir, ref):
    """record.json at <ref>, or None if that commit doesn't carry one."""
    r = subprocess.run(
        ["git", "-C", repo_dir, "show", f"{ref}:record.json"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def project_records(repo_dir):
    """Every record committed on main, oldest first (one record.json per commit)."""
    hashes = _git(repo_dir, "log", "--reverse", "--format=%H", "main").split()
    out = []
    for h in hashes:
        rec = _show_record(repo_dir, h)
        if rec is not None:
            out.append(rec)
    return out


def task_records(repo_dir):
    """A task repo's record: the record.json of its HEAD commit."""
    rec = _show_record(repo_dir, "HEAD")
    return [rec] if rec is not None else []


def collect(checkouts):
    """checkouts: list of (role, dir) with role in {'project', 'task'}.
    Returns the records list, deduplicated by id (first occurrence wins;
    fold itself is permutation-invariant, so collection order is cosmetic)."""
    records, seen = [], set()
    for role, d in checkouts:
        recs = project_records(d) if role == "project" else task_records(d)
        for rec in recs:
            rid = str(rec.get("id"))
            if rid not in seen:
                seen.add(rid)
                records.append(rec)
    return records


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _fmt(v):
    return json.dumps(v, sort_keys=True)


def render_markdown(result, records):
    by_id = {str(r.get("id")): r for r in records}
    lines = ["## Derived state", "", "| key | value | set by |", "|---|---|---|"]
    winner_of = {}
    for c in result["conflicts"]:
        winner_of[c["key"]] = c["winner"]
    # For non-conflicted keys, find the (unique maximal) setter for display.
    for key in sorted(result["state"]):
        w = winner_of.get(key)
        if w is None:
            setters = [str(r["id"]) for r in records if key in (r.get("effects") or {})]
            w = setters[-1] if setters else "?"
        lines.append(f"| `{key}` | {_fmt(result['state'][key])} | {w} |")
    lines += ["", "## Conflicts", ""]
    if result["conflicts"]:
        lines += ["| key | winner | losers (preserved) |", "|---|---|---|"]
        for c in result["conflicts"]:
            lines.append(f"| `{c['key']}` | {c['winner']} | {', '.join(c['losers'])} |")
    else:
        lines.append("_none_")
    lines += ["", "## Application order", ""]
    for i, rid in enumerate(result["order"], 1):
        rec = by_id.get(rid, {})
        lines.append(f"{i}. `{rid}` ({rec.get('kind', '?')})")
    lines += ["", "## Orphans", ""]
    lines += [f"- `{o}`" for o in result["orphans"]] or ["_none_"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# checkpoints
# --------------------------------------------------------------------------

def closure_records(records, at):
    """The records that are ancestors of `at` (including `at` itself)."""
    idx = {str(r["id"]): r for r in records}
    if str(at) not in idx:
        return None
    seen, stack = set(), [str(at)]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(str(p) for p in (idx[n].get("parents") or []))
    return [idx[r] for r in sorted(seen)]


def verify_at(records, at, given_state=None):
    closure = closure_records(records, at)
    if closure is None:
        return {"at": at, "error": "record not found"}
    honest_state = fold.derive(closure)["state"]
    out = {
        "at": at,
        "closure_state": honest_state,
        "honest_checkpoint_verifies": fold.verify_checkpoint(
            records, {"at": at, "state": honest_state}
        ),
    }
    if given_state is not None:
        out["given_state_verifies"] = fold.verify_checkpoint(
            records, {"at": at, "state": given_state}
        )
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_checkouts(specs):
    out = []
    for s in specs:
        role, _, d = s.partition("=")
        if role not in ("project", "task") or not d:
            raise SystemExit(f"bad checkout spec {s!r} (want project=DIR or task=DIR)")
        out.append((role, d))
    return out


def _load_records(args):
    if args.records:
        return json.load(open(args.records))
    return collect(_parse_checkouts(args.repos))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("collect", "render"):
        p = sub.add_parser(name)
        p.add_argument("repos", nargs="*", help="project=DIR / task=DIR specs")
        p.add_argument("--records", help="load records from a JSON file instead")

    p = sub.add_parser("verify")
    p.add_argument("repos", nargs="*", help="project=DIR / task=DIR specs")
    p.add_argument("--records", help="load records from a JSON file instead")
    p.add_argument("--at", required=True)
    p.add_argument("--state", help="a JSON file with a claimed state to check")

    args = ap.parse_args(argv)
    records = _load_records(args)

    if args.cmd == "collect":
        print(json.dumps(records, indent=2, sort_keys=True))
    elif args.cmd == "render":
        print(render_markdown(fold.derive(records), records), end="")
    elif args.cmd == "verify":
        given = json.load(open(args.state)) if args.state else None
        print(json.dumps(verify_at(records, args.at, given), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
