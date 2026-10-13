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
                         [--work DIR] [--genesis]
                                             clone, write record.json (+
                                             work files), commit, push

Refusals (checked before anything is pushed — the fold is unchanged):
  * secret-shaped content in the record body (reason + effect values) is
    refused outright; the sanctioned shape is a vault pointer, e.g.
    --set db.password=vault:staging-db, which passes. There is no override
    flag — the refusal is the feature.
  * a task record with empty/missing effects is refused: a task asserts
    completed work, so it must carry effects; a change you merely want is
    spoken to your human, not filed. No override flag. (Record an observation
    with --kind witness.)
  * an empty-parents (genesis) record is refused unless --genesis is passed
    AND the project has no records yet; the error names the current frontier.
  * a witness carrying non-observation effect keys is warned (stderr), never
    refused — a witness reporting measured state (env.*) is legal.
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
import math
import os
import re
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


# --------------------------------------------------------------------------
# refusals — pre-push checks that live inside the tool the agent already runs.
# The fold is unchanged; these guard what reaches the record in the first place.
# Pure functions (scan_secret / witness_warnings / genesis_decision) so a
# runner test can exercise every branch without touching the network.
# --------------------------------------------------------------------------

# Known secret shapes. Each maps to a human-named pattern class; the matched
# VALUE is never echoed — only the class is reported.
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "openai-style secret key (sk-...)"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token (ghp_...)"),
    (re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"), "github token (gh*_...)"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "aws access key id (AKIA...)"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "PEM private key header"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "bearer token"),
]

# A key name that says "this holds a credential". "key" must be bounded (so
# "monkey"/"keyboard" do not match); the credential words match as substrings.
_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|(^|[._-])keys?([._-]|$))"
)

# "password: hunter2" / "api_key=AKIA..." embedded inside a free-text string.
_INLINE_ASSIGN_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"\s*[:=]\s*[\"']?([^\s\"']{6,})"
)

# Values that are obviously not a live secret — the key=value rule skips these.
_PLACEHOLDERS = {
    "", "x", "none", "null", "nil", "todo", "tbd", "redacted", "changeme",
    "change-me", "placeholder", "example", "sample", "dummy", "test", "fake",
    "your-key-here", "...",
}


def _looks_like_vault_pointer(v):
    """A sanctioned indirection — `vault:staging-db`, `vault://...`, `secret-ref:...`
    — is the ESCAPE HATCH. It names a secret without carrying one, so it passes."""
    return isinstance(v, str) and bool(
        re.match(r"(?i)^(vault:|secret-ref:|secretref:|op://|aws-secrets:)", v.strip())
    )


def _is_placeholder(v):
    s = v.strip()
    if s.lower() in _PLACEHOLDERS:
        return True
    if re.fullmatch(r"[*x.<>\[\]{}?_-]+", s):  # ***, xxxx, <...>, ----
        return True
    if re.fullmatch(r"(?i)<[^>]*>", s):  # <your-token>
        return True
    return False


def _shannon_entropy(s):
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _high_entropy_token(s):
    """A long, dense, mixed token is almost certainly a credential. Tuned so
    ordinary values (dotted keys, region names, words, numbers, the scene's
    records) never trip it: needs length >= 24, a secret-ish charset, a mix of
    letters and digits, and >= 4.0 bits/char of entropy."""
    for tok in re.split(r"[\s\"'`,;]+", s):
        if len(tok) < 24:
            continue
        if _looks_like_vault_pointer(tok) or _is_placeholder(tok):
            continue
        if not re.fullmatch(r"[A-Za-z0-9+/=_\-\.]{24,}", tok):
            continue
        has_alpha = any(c.isalpha() for c in tok)
        has_digit = any(c.isdigit() for c in tok)
        is_long_hex = bool(re.fullmatch(r"[0-9a-fA-F]{32,}", tok))
        if not ((has_alpha and has_digit) or is_long_hex):
            continue
        if _shannon_entropy(tok) >= 4.0:
            return True
    return False


def _scan_string(s):
    for rx, name in _SECRET_PATTERNS:
        if rx.search(s):
            return name
    m = _INLINE_ASSIGN_RE.search(s)
    if m and not _is_placeholder(m.group(2)) and not _looks_like_vault_pointer(m.group(2)):
        return "secret-shaped assignment in text"
    if _high_entropy_token(s):
        return "high-entropy token"
    return None


def _scan_value(v, in_key):
    # key=value rule: a credential-named key with a real string value.
    if in_key is not None and _SECRET_KEY_RE.search(in_key):
        if (isinstance(v, str) and len(v.strip()) >= 6
                and not _is_placeholder(v) and not _looks_like_vault_pointer(v)):
            return "secret-shaped key=value assignment"
    if isinstance(v, str):
        if not _looks_like_vault_pointer(v):
            hit = _scan_string(v)
            if hit:
                return hit
    elif isinstance(v, dict):
        for k, sub in v.items():
            hit = _scan_value(sub, in_key=str(k))
            if hit:
                return hit
    elif isinstance(v, list):
        for sub in v:
            hit = _scan_value(sub, in_key=None)
            if hit:
                return hit
    return None


def scan_secret(record):
    """Return the matched pattern CLASS (never the value) if the record body —
    reason plus effect values, recursively — looks like it carries a secret,
    else None. Record id and parents are not scanned (they are not the body)."""
    reason = record.get("reason")
    if isinstance(reason, str) and not _looks_like_vault_pointer(reason):
        hit = _scan_string(reason)
        if hit:
            return hit
    return _scan_value(record.get("effects") or {}, in_key=None)


# Witnesses report measured/ambient state. These prefixes mark an observation;
# a witness setting anything else is reporting intent, which is the boundary the
# warning teaches (it is a warning, never a refusal — measured state is legal).
_OBSERVATION_PREFIXES = ("env.", "obs.", "observed.", "metric.", "metrics.",
                         "seen.", "reading.", "sample.")


def witness_warnings(record):
    """Loud-but-non-fatal warnings for a witness carrying non-observation keys."""
    if record.get("kind") != "witness":
        return []
    bad = [k for k in (record.get("effects") or {})
           if not str(k).startswith(_OBSERVATION_PREFIXES)]
    if not bad:
        return []
    return [
        "witness record sets non-observation key(s): " + ", ".join(sorted(bad)),
        "  witnesses report measured/ambient state (e.g. env.*); a key that "
        "looks like intent (config.*, feature.*) usually belongs on a task.",
        "  pushing anyway — a witness that reports real measured state is legal.",
    ]


def genesis_decision(parents, genesis_flag, count, frontier):
    """None if the record may proceed; otherwise a teaching refusal message.
    A root record (empty parents) is allowed only with --genesis AND an empty
    project."""
    if parents:
        return None
    if not genesis_flag:
        msg = ("refusing an empty-parents (genesis) record without --genesis: "
               "pass --genesis to create a project's first record.")
        if count:
            msg += (f" but this project already has {count} record(s) — you "
                    "probably meant to parent to the current frontier: "
                    f"{', '.join(frontier) or '(none)'}.")
        return msg
    if count:
        return (f"refusing --genesis: this project already has {count} "
                "record(s); genesis is only for an empty project. Parent to "
                f"the current frontier instead: {', '.join(frontier) or '(none)'}.")
    return None


def empty_effects_refusal(record):
    """None if the record may proceed; else a one-line teaching refusal.
    A task record ASSERTS completed work, so it must carry effects. A task with
    empty or missing effects is the "proposal record" smell: a change merely
    WANTED, filed instead of spoken. Only kind == "task" is refused here —
    witness discipline is the existing warning's job, and override is
    unaffected."""
    if record.get("kind") != "task":
        return None
    eff = record.get("effects")
    if isinstance(eff, dict) and eff:
        return None
    return ("empty-effects task record — a task asserts completed work and must "
            "carry effects; a change you merely want is spoken to your human, "
            "not filed. Record an observation with --kind witness.")


def _project_of(repo):
    """The project a repo belongs to: the part before the first '--'."""
    return repo.split("--", 1)[0]


def _frontier_ids(records):
    """Applied leaves — records no other record parents to — in the application
    order /api/records already returns them in. These are what a new record
    should usually parent to."""
    parented = set()
    for r in records:
        for p in (r.get("parents") or []):
            parented.add(p)
    return [r["id"] for r in records
            if r.get("id") and r["id"] not in parented and not r.get("orphan")]


def project_records(project):
    """The project's collected records (application order) via /api/records."""
    q = urllib.parse.urlencode({"project": project})
    out = http("GET", "/api/records", raw_query=q)
    return out.get("records") or []


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

    # --- refusals (before any token, checkout, or push) -------------------
    # 1. Secret refusal: never push a record whose body looks like it carries a
    #    secret. No escape flag — the sanctioned shape is a vault pointer, which
    #    passes the scan. The value is never echoed.
    hit = scan_secret(record)
    if hit:
        die(f"refusing to push {args.id}: {hit} in the record body (value not "
            "shown). Store the secret in your secret manager and put a pointer "
            "in the record instead, e.g. --set db.password=vault:staging-db.")
    # 2. Proposal-record refusal: a task asserts completed work, so a task with
    #    empty/missing effects is the "proposal record" smell — refused. No
    #    override flag (same doctrine as the secret refusal).
    msg = empty_effects_refusal(record)
    if msg:
        die(f"refusing to push {args.id}: {msg}")
    # 3. Kind discipline: a witness carrying non-observation keys is warned
    #    loudly on stderr, never refused.
    for line in witness_warnings(record):
        print(f"pp: warning: {line}", file=sys.stderr)
    # 4. Genesis guard: a root record (empty parents) needs --genesis AND an
    #    empty project. Only queries the Worker when parents are empty.
    if not parents:
        project = _project_of(args.repo)
        existing = project_records(project)
        msg = genesis_decision(parents, args.genesis, len(existing),
                               _frontier_ids(existing))
        if msg:
            die(msg)

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
    p.add_argument("--genesis", action="store_true",
                   help="allow an empty-parents root record (only on a project "
                        "with no records yet; see Refusals in runner/README.md)")
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
