#!/usr/bin/env python3
"""pp.py — the agent-side CLI for the git.pp submission.

One stdlib-only Python file (see README.md for why Python, not bash).
A judge — or an agent — uses it to drive the live Worker + Artifacts:

  pp.py project create <name>                create a project repo (gated)
  pp.py task fork <project> <task>           fork <project>--<task> (gated)
  pp.py token <repo>                         mint a repo token (gated);
                                             prints git-config export lines
  pp.py complete <repo> --id ID --parents a,b --kind task|witness|override
                         --set k=v [--set k=v ...] [--reason "..."]
                         [--work DIR]
                                             clone, write record.json (+
                                             work files), commit, push
  pp.py state <project>                      derived state + conflicts
  pp.py verify <project> --at ID [--tamper]  checkpoint check against the
                                             Python referee (../poc)
  pp.py collect <project>                    print the collected records
                                             as JSON (used by scene.sh
                                             and the determinism shuffle)

Config (environment):
  PP_WORKER           Worker base URL
                      (default https://gitpp.casey-digennaro.workers.dev)
  PP_ADMIN_KEY        admin key for gated routes; overrides the key file
  PP_ADMIN_KEY_FILE   key file path (default ../.admin-key.local relative
                      to this script — the build tree's mode-600 file).
                      The key is read at runtime, never printed, never
                      written anywhere by this tool.
  PP_ARTIFACTS_ACCOUNT / PP_ARTIFACTS_NS
                      build git remote URLs
                      (default 049ff5e84ecf636b53b162cbb580aae6 / default)
  PP_POC_DIR          location of poc/ (fold.py + pp.py referee);
                      default ../../poc relative to this script

Git runs locally, wherever this command runs (a judge's machine, a
fleet box, CI). Tokens for git are minted per operation through the
gated Worker route and passed to git via GIT_CONFIG_* environment
(http.extraHeader) — they are never written into .git/config, files,
or logs. The one exception is the explicit `token` command, whose
whole purpose is to print a token the operator asked for.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = os.environ.get("PP_WORKER", "https://gitpp.casey-digennaro.workers.dev").rstrip("/")
ACCOUNT = os.environ.get("PP_ARTIFACTS_ACCOUNT", "049ff5e84ecf636b53b162cbb580aae6")
NS = os.environ.get("PP_ARTIFACTS_NS", "default")
POC_DIR = Path(os.environ.get("PP_POC_DIR", HERE.parent / "referee"))

GIT_IDENT = ["-c", "user.name=pp-runner", "-c", "user.email=pp@superinstance.ai"]


def die(msg, code=2):
    print(f"pp: error: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# admin key + HTTP
# --------------------------------------------------------------------------

def admin_key():
    k = os.environ.get("PP_ADMIN_KEY")
    if k:
        return k.strip()
    f = Path(os.environ.get("PP_ADMIN_KEY_FILE", HERE.parent / ".admin-key.local"))
    try:
        return f.read_text().strip()
    except OSError:
        die(f"admin key not found: set PP_ADMIN_KEY or place the key in {f}")
    return ""  # unreachable


def http(method, path, body=None, gated=False, raw_query=None):
    url = WORKER + path
    if raw_query:
        url += "?" + raw_query
    data = None
    headers = {"accept": "application/json",
               # urllib's default UA is refused by Cloudflare (error 1010)
               "user-agent": "pp.py/1.0 (git.pp runner; python-urllib)"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    if gated:
        headers["X-Admin-Key"] = admin_key()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        die(f"{method} {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"{method} {path} -> {e.reason}")


def remote_url(repo):
    return f"https://{ACCOUNT}.artifacts.cloudflare.net/git/{NS}/{repo}.git"


def mint_token(repo, scope="write"):
    out = http("POST", f"/api/repos/{urllib.parse.quote(repo, safe='')}/token",
               {"scope": scope}, gated=True)
    tok = out.get("plaintext")
    if not tok:
        die(f"token mint for {repo} returned no plaintext: {out}")
    return tok


# --------------------------------------------------------------------------
# git plumbing (token via environment only)
# --------------------------------------------------------------------------

def git_env(token):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
    return env


def git(args, env, cwd=None, check=True):
    r = subprocess.run(["git", *args], env=env, cwd=cwd,
                       capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")
    return r


def fresh_checkout(repo, token, parent):
    """Clone `repo` into parent/checkout and return the path. Handles a
    brand-new empty repo (unborn HEAD -> point it at main)."""
    dest = Path(parent) / "checkout"
    git(["clone", "-q", remote_url(repo), str(dest)], git_env(token))
    env = git_env(token)
    head = git(["rev-parse", "--verify", "HEAD"], env, cwd=dest, check=False)
    if head.returncode != 0:
        git(["symbolic-ref", "HEAD", "refs/heads/main"], env, cwd=dest)
    else:
        cur = git(["branch", "--show-current"], env, cwd=dest).stdout.strip()
        if cur != "main":
            git(["checkout", "-q", "main"], env, cwd=dest)
    return dest


# --------------------------------------------------------------------------
# commands: project / task / token
# --------------------------------------------------------------------------

def cmd_project_create(args):
    body = {"name": args.name}
    if args.description:
        body["description"] = args.description
    out = http("POST", "/api/repos", body, gated=True)
    print(json.dumps(out, indent=2, sort_keys=True))


def cmd_task_fork(args):
    name = f"{args.project}--{args.task}"
    path = f"/api/repos/{urllib.parse.quote(args.project, safe='')}/fork"
    out = http("POST", path, {"name": name}, gated=True)
    print(json.dumps(out, indent=2, sort_keys=True))


def cmd_token(args):
    body = {"scope": args.scope}
    if args.ttl:
        body["ttl"] = args.ttl
    out = http("POST", f"/api/repos/{urllib.parse.quote(args.repo, safe='')}/token",
               body, gated=True)
    tok = out.get("plaintext")
    if not tok:
        die(f"token mint returned no plaintext: {out}")
    # Explicit operator request: the token goes to stdout, and only here.
    print("# repo token for git against Artifacts (expires "
          f"{out.get('expiresAt')}); use with any git command:")
    print("export GIT_CONFIG_COUNT=1")
    print("export GIT_CONFIG_KEY_0=http.extraHeader")
    print(f'export GIT_CONFIG_VALUE_0="Authorization: Bearer {tok}"')
    print("export GIT_TERMINAL_PROMPT=0")


# --------------------------------------------------------------------------
# command: complete
# --------------------------------------------------------------------------

def parse_value(text):
    try:
        return json.loads(text)
    except Exception:
        return text


def cmd_complete(args):
    effects = {}
    for kv in args.sets or []:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            die(f"--set wants k=v, got {kv!r}")
        effects[k] = parse_value(v)
    parents = [p.strip() for p in (args.parents or "").split(",") if p.strip()]
    record = {"id": args.id, "parents": parents, "kind": args.kind,
              "effects": effects}
    if args.reason:
        record["reason"] = args.reason

    token = mint_token(args.repo, "write")
    tmp = tempfile.mkdtemp(prefix="pp-complete-")
    try:
        dest = fresh_checkout(args.repo, token, tmp)
        env = git_env(token)
        if args.work:
            src = Path(args.work)
            if not src.is_dir():
                die(f"--work {src} is not a directory")
            for item in src.iterdir():
                if item.name == ".git":
                    continue
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        (dest / "record.json").write_text(json.dumps(record, indent=2) + "\n")
        git(["add", "-A"], env, cwd=dest)
        git([*GIT_IDENT, "commit", "-q", "-m", f"record {args.id}"], env, cwd=dest)
        git(["push", "-q", "origin", "main"], env, cwd=dest)
        files = git(["show", "--pretty=format:", "--name-only", "HEAD"],
                    env, cwd=dest).stdout.split()
    finally:
        if not os.environ.get("PP_KEEP_WORK"):
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"completed {args.id} in {args.repo}: kind={args.kind} "
          f"parents={parents} effects={json.dumps(effects, sort_keys=True)} "
          f"files={files} pushed to main")


# --------------------------------------------------------------------------
# command: state
# --------------------------------------------------------------------------

def cmd_state(args):
    q = urllib.parse.urlencode({"project": args.project})
    out = http("GET", "/api/state", raw_query=q)
    print(f"project {args.project} — derived state")
    if out.get("state"):
        for k in sorted(out["state"]):
            print(f"  {k} = {json.dumps(out['state'][k], sort_keys=True)}")
    else:
        print("  (empty)")
    print("conflicts:")
    if out.get("conflicts"):
        for c in out["conflicts"]:
            losers = ", ".join(c.get("losers") or []) or "—"
            print(f"  {c['key']}: winner {c['winner']}; losers (preserved): {losers}")
    else:
        print("  (none)")
    print(f"order: {', '.join(out.get('order') or []) or '(none)'}")
    print(f"orphans: {', '.join(out.get('orphans') or []) or '(none)'}")


# --------------------------------------------------------------------------
# collection + verify (via the Python referee in ../poc)
# --------------------------------------------------------------------------

def _load_poc():
    sys.path.insert(0, str(POC_DIR))
    import fold  # noqa: E402  (referee/fold.py — the Python reference)
    spec = importlib.util.spec_from_file_location("poc_pp", POC_DIR / "pp.py")
    poc_pp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poc_pp)
    return fold, poc_pp


def collect_records(project):
    """Clone the project repo + every <project>--* repo into a temp dir
    and collect records with poc/pp.py's logic (project: one record per
    commit on main; task repos: HEAD record.json). Returns
    (records, tmpdir) — caller removes tmpdir."""
    listing = http("GET", "/api/repos")
    names = sorted(
        r["name"] for r in listing.get("repos", [])
        if r.get("name") == project or str(r.get("name", "")).startswith(project + "--")
    )
    if project not in names:
        die(f"project repo {project} not found in namespace")
    fold, poc_pp = _load_poc()
    tmp = tempfile.mkdtemp(prefix="pp-collect-")
    checkouts = []
    for name in names:
        try:
            token = mint_token(name, "read")
        except SystemExit:
            token = mint_token(name, "write")
        parent = Path(tmp) / name
        parent.mkdir(parents=True)
        dest = fresh_checkout(name, token, parent)
        checkouts.append(("project" if name == project else "task", str(dest)))
    records = poc_pp.collect(checkouts)
    return records, tmp


def cmd_collect(args):
    records, tmp = collect_records(args.project)
    try:
        print(json.dumps(records, indent=2, sort_keys=True))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_verify(args):
    fold, poc_pp = _load_poc()
    records, tmp = collect_records(args.project)
    try:
        closure = poc_pp.closure_records(records, args.at)
        if closure is None:
            print(f"FAIL — record {args.at!r} not found in project {args.project}")
            sys.exit(1)
        honest_state = fold.derive(closure)["state"]
        print(f"ancestor-closure state at {args.at}: "
              f"{json.dumps(honest_state, sort_keys=True)}")
        honest_ok = fold.verify_checkpoint(
            records, {"at": args.at, "state": honest_state})
        print(f"honest checkpoint: {'PASS' if honest_ok else 'FAIL'}")
        ok = bool(honest_ok)
        if args.tamper:
            tampered = dict(honest_state)
            if tampered:
                k = sorted(tampered)[0]
                tampered[k] = "__tampered__" if tampered[k] != "__tampered__" \
                    else "__tampered-2__"
                what = f"flipped {k}"
            else:
                tampered["__tampered__"] = True
                what = "added a key to the empty state"
            forged_ok = fold.verify_checkpoint(
                records, {"at": args.at, "state": tampered})
            rejected = forged_ok is False
            print(f"tamper check: {'PASS' if rejected else 'FAIL'} "
                  f"({what}; forged state "
                  f"{'rejected' if rejected else 'ACCEPTED — BUG'})")
            ok = ok and rejected
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pp", description="git.pp agent-side CLI (Worker + Artifacts)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("project", help="project repo operations")
    psub = p.add_subparsers(dest="sub", required=True)
    pc = psub.add_parser("create")
    pc.add_argument("name")
    pc.add_argument("--description")
    pc.set_defaults(fn=cmd_project_create)

    p = sub.add_parser("task", help="task repo operations")
    psub = p.add_subparsers(dest="sub", required=True)
    pf = psub.add_parser("fork")
    pf.add_argument("project")
    pf.add_argument("task")
    pf.set_defaults(fn=cmd_task_fork)

    p = sub.add_parser("token")
    p.add_argument("repo")
    p.add_argument("--scope", default="write", choices=["read", "write"])
    p.add_argument("--ttl", type=int)
    p.set_defaults(fn=cmd_token)

    p = sub.add_parser("complete")
    p.add_argument("repo")
    p.add_argument("--id", required=True)
    p.add_argument("--parents", default="",
                   help="comma-separated parent record ids (empty = root)")
    p.add_argument("--kind", required=True,
                   choices=["task", "witness", "override"])
    p.add_argument("--set", dest="sets", action="append", default=[],
                   help="k=v; v is parsed as JSON when possible")
    p.add_argument("--reason")
    p.add_argument("--work", help="directory whose files join the commit")
    p.set_defaults(fn=cmd_complete)

    p = sub.add_parser("state")
    p.add_argument("project")
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser("verify")
    p.add_argument("project")
    p.add_argument("--at", required=True)
    p.add_argument("--tamper", action="store_true",
                   help="also flip one closure value and require rejection")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("collect")
    p.add_argument("project")
    p.set_defaults(fn=cmd_collect)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
