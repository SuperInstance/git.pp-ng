/**
 * gitpp — the front door.
 *
 * A Cloudflare Worker over Artifacts. Repos hold the record (one
 * record.json per commit in the project repo; one at HEAD in each task
 * repo forked from it). This Worker creates/forks repos through the
 * Artifacts binding, reads records back through the same binding
 * (log + readFile — no git clone), derives current state with the
 * refereed fold (./fold.ts), and renders the projection.
 *
 * The projection is a pure function of the record, derived per request.
 * Nothing here is a second source of truth.
 *
 * Mutating routes require the X-Admin-Key header (ADMIN_KEY secret
 * binding). Read routes are public: the projection is the demo's face.
 */
import {
  derive,
  witnessFreshness,
  type DeriveOutput,
  type FoldRecord,
} from "./fold.ts";

// ---------------------------------------------------------------------------
// Artifacts binding surface (as verified live by the worker spike,
// research/worker-spike.md). Read shapes that the docs leave loose
// (list pages, log entries) are extracted defensively.
// ---------------------------------------------------------------------------

interface RepoMeta {
  name: string;
  remote?: string;
  defaultBranch?: string;
  token?: string;
  [key: string]: unknown;
}

interface ArtifactsRepo {
  info(): Promise<unknown>;
  createToken(
    scope?: string,
    ttl?: number,
  ): Promise<{ plaintext: string; expiresAt?: string } | string>;
  fork(name: string, opts?: { [key: string]: unknown }): Promise<RepoMeta>;
  log(opts?: { ref?: string; limit?: number }): Promise<unknown>;
  readFile(opts: { ref: string; path: string }): Promise<Blob | null>;
}

interface ArtifactsBinding {
  create(name: string, opts?: { [key: string]: unknown }): Promise<RepoMeta>;
  get(name: string): Promise<ArtifactsRepo>;
  list(opts?: { limit?: number; cursor?: string }): Promise<unknown>;
  delete(name: string): Promise<unknown>;
}

interface KVNamespace {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
}

interface Env {
  ARTIFACTS: ArtifactsBinding;
  ADMIN_KEY?: string;
  EVENTS?: KVNamespace;
}

const DEFAULT_PROJECT = "poc-quilt-1";
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function display(v: unknown): string {
  if (typeof v === "string") return v;
  return JSON.stringify(v) ?? String(v);
}

/** Constant-time string equality (F1): always scans the longer length and
 * folds the length difference in, so the comparison does not short-circuit
 * on the first differing byte. Best-effort in JS (the JIT makes no hard
 * guarantees), but strictly better than `===` on a secret. */
function keyEq(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const ca = i < a.length ? a.charCodeAt(i) : 0;
    const cb = i < b.length ? b.charCodeAt(i) : 0;
    diff |= ca ^ cb;
  }
  return diff === 0;
}

function authed(request: Request, env: Env): boolean {
  if (!env.ADMIN_KEY) return false; // fail closed if the secret is unset
  const provided = request.headers.get("x-admin-key");
  return provided !== null && keyEq(provided, env.ADMIN_KEY);
}

/** The `?project=` parameter of the read routes: absent means the default
 * project; present means it must be a well-formed name (same shape the
 * create route enforces), or the request is a clean 400 — never a derive
 * over a garbage name. */
function projectParam(
  url: URL,
): { project: string } | { response: Response } {
  const p = url.searchParams.get("project");
  if (p === null) return { project: DEFAULT_PROJECT };
  if (!NAME_RE.test(p)) {
    return { response: json({ error: "invalid project name" }, 400) };
  }
  return { project: p };
}

/** A repo name taken from a URL path segment: percent-decoded defensively
 * (malformed encodings are a 400, never a 500) and held to the same shape
 * as the create route, so traversal-shaped strings never reach the
 * binding. Returns null when the segment is not a well-formed name. */
function pathName(segment: string): string | null {
  let name: string;
  try {
    name = decodeURIComponent(segment);
  } catch {
    return null;
  }
  return NAME_RE.test(name) ? name : null;
}

function pageItems(page: unknown): unknown[] {
  if (Array.isArray(page)) return page;
  if (page && typeof page === "object") {
    const p = page as { [k: string]: unknown };
    for (const k of ["repos", "items", "result", "data"]) {
      if (Array.isArray(p[k])) return p[k] as unknown[];
    }
  }
  return [];
}

function pageCursor(page: unknown): string | null {
  if (page && typeof page === "object" && !Array.isArray(page)) {
    const p = page as { [k: string]: unknown };
    for (const k of ["cursor", "nextCursor", "next_cursor"]) {
      if (typeof p[k] === "string" && p[k]) return p[k] as string;
    }
  }
  return null;
}

async function listAllRepos(env: Env): Promise<RepoMeta[]> {
  const out: RepoMeta[] = [];
  let cursor: string | undefined;
  for (let i = 0; i < 50; i++) {
    const page = await env.ARTIFACTS.list({ limit: 100, cursor });
    for (const item of pageItems(page)) {
      if (item && typeof item === "object") out.push(item as RepoMeta);
    }
    const next = pageCursor(page);
    if (!next || next === cursor) break;
    cursor = next;
  }
  return out;
}

function commitIds(logResult: unknown): string[] {
  let entries: unknown[] = [];
  if (Array.isArray(logResult)) entries = logResult;
  else if (logResult && typeof logResult === "object") {
    const p = logResult as { [k: string]: unknown };
    for (const k of ["commits", "log", "entries", "history", "result"]) {
      if (Array.isArray(p[k])) {
        entries = p[k] as unknown[];
        break;
      }
    }
  }
  const ids: string[] = [];
  for (const e of entries) {
    if (typeof e === "string") ids.push(e);
    else if (e && typeof e === "object") {
      const o = e as { [k: string]: unknown };
      for (const k of ["id", "hash", "sha", "oid", "commit"]) {
        if (typeof o[k] === "string") {
          ids.push(o[k] as string);
          break;
        }
      }
    }
  }
  return ids;
}

// ---------------------------------------------------------------------------
// record collection: the record for project P lives in repo P (one
// record.json per commit — walk the whole log) and in every repo named
// P--* (the task's record.json at HEAD). Dedupe by record id.
// ---------------------------------------------------------------------------

// Blob reads dominate collection latency: one awaited readFile/log per repo,
// run sequentially, is ~N network round-trips (poc-scale-1: 61 repos, ~14-28s
// warm, while the fold itself is ~12ms). We fetch the blobs with bounded
// concurrency but still INGEST them in the original deterministic order, so
// dedupe ("first-seen wins"), origin attribution, the records array, and every
// derived output stay byte-identical to the sequential version.
const COLLECT_CONCURRENCY = 24;

/** Resolve `fn` over `items` with at most `limit` in flight, writing each
 * result at its item's index (so the output is order-preserving regardless of
 * completion order). `fn` must not reject — callers return null on failure. */
async function mapBounded<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  const workers: Promise<void>[] = [];
  for (let w = 0; w < Math.min(limit, items.length); w++) {
    workers.push(
      (async () => {
        for (;;) {
          const i = next++;
          if (i >= items.length) return;
          results[i] = await fn(items[i]);
        }
      })(),
    );
  }
  await Promise.all(workers);
  return results;
}

interface CollectionWarning {
  id: string;
  keptFrom: string | null;
  droppedFrom: string;
}

async function collectProject(
  env: Env,
  project: string,
): Promise<{
  records: FoldRecord[];
  repos: string[];
  origin: Map<string, string>;
  warnings: CollectionWarning[];
}> {
  const all = await listAllRepos(env);
  const names = all
    .map((r) => r.name)
    .filter(
      (n): n is string =>
        typeof n === "string" && (n === project || n.startsWith(project + "--")),
    )
    .sort();

  // Ordered read plan: project-repo commits first (in deterministic log order),
  // then task repos in sorted name order. Each entry reads one record.json to
  // text, catching everything so a failed read becomes null (skipped), exactly
  // as the sequential version skipped a throw or a null blob.
  const plan: { repo: string; read: () => Promise<string | null> }[] = [];

  // Project repo: enumerate commits first (one get + one log — sequential, as
  // the reads below depend on the commit list).
  try {
    const repo = await env.ARTIFACTS.get(project);
    let commits: string[] = [];
    try {
      commits = commitIds(await repo.log({ ref: "main", limit: 1000 }));
    } catch {
      commits = [];
    }
    if (commits.length === 0) commits = ["main"];
    for (const c of commits) {
      plan.push({
        repo: project,
        read: async () => {
          try {
            const b = await repo.readFile({ ref: c, path: "record.json" });
            return b ? await b.text() : null;
          } catch {
            return null;
          }
        },
      });
    }
  } catch {
    /* project repo missing/unready — derive over what exists */
  }

  // Task repos: the task's record at HEAD.
  for (const name of names) {
    if (name === project) continue;
    plan.push({
      repo: name,
      read: async () => {
        try {
          const repo = await env.ARTIFACTS.get(name);
          const b = await repo.readFile({ ref: "main", path: "record.json" });
          return b ? await b.text() : null;
        } catch {
          return null;
        }
      },
    });
  }

  // Fetch concurrently (results stay index-aligned), then ingest IN ORDER.
  const texts = await mapBounded(plan, COLLECT_CONCURRENCY, (p) => p.read());

  const byId = new Map<string, FoldRecord>();
  // record id -> the repo the record was first collected from. First-seen
  // wins, exactly as the dedupe below does, so the project repo (ingested
  // first) is credited for records that also appear at a task HEAD.
  const origin = new Map<string, string>();
  // Normalized JSON of the kept record per id, to tell a harmless duplicate
  // (the same record seen in the project repo AND at a task HEAD) from a
  // genuine divergent duplicate (same id, different content across repos),
  // which is the one case where this first-seen dedupe silently drops data.
  const keptNorm = new Map<string, string>();
  const warnings: CollectionWarning[] = [];
  for (let i = 0; i < plan.length; i++) {
    const text = texts[i];
    if (text === null) continue;
    let rec: FoldRecord;
    try {
      rec = JSON.parse(text) as FoldRecord;
    } catch {
      /* not JSON a JS reader accepts — skip (see ENGINEERING.md collection audit) */
      continue;
    }
    if (!rec || typeof rec.id !== "string") continue;
    const norm = JSON.stringify(rec);
    if (!byId.has(rec.id)) {
      byId.set(rec.id, rec);
      origin.set(rec.id, plan[i].repo);
      keptNorm.set(rec.id, norm);
    } else if (keptNorm.get(rec.id) !== norm) {
      // Divergent duplicate: a record with this id, but different content, was
      // dropped by first-seen dedupe. Surfaced rather than lost silently.
      warnings.push({
        id: rec.id,
        keptFrom: origin.get(rec.id) ?? null,
        droppedFrom: plan[i].repo,
      });
    }
  }

  return { records: [...byId.values()], repos: names, origin, warnings };
}

async function projectDerivation(
  env: Env,
  project: string,
): Promise<{
  out: DeriveOutput;
  records: FoldRecord[];
  recordCount: number;
  repos: string[];
  origin: Map<string, string>;
  warnings: CollectionWarning[];
}> {
  const { records, repos, origin, warnings } = await collectProject(env, project);
  return {
    out: derive(records),
    records,
    recordCount: records.length,
    repos,
    origin,
    warnings,
  };
}

// ---------------------------------------------------------------------------
// record view: the collected records, annotated with the repo each came
// from and ordered by the fold's application order (out.order), with
// orphans — which have no application order — listed after. Fields are the
// raw record fields (parents/kind/effects/reason); derivation is never
// consulted for anything but ordering and the orphan flag.
// ---------------------------------------------------------------------------

interface RecordView {
  id: string;
  repo: string | null;
  kind: unknown;
  parents: unknown;
  effects: unknown;
  reason: unknown;
  orphan: boolean;
  // Per-witness staleness from witnessFreshness(): record-position distance
  // between the witness's causal past and the applied frontier. A number on
  // applied witness records, null on everything else (non-witnesses and
  // orphaned/cycle-excluded witnesses, which the fold does not apply).
  recordsBehind: number | null;
}

function buildRecordsView(
  records: FoldRecord[],
  out: DeriveOutput,
  origin: Map<string, string>,
  freshness: { [id: string]: number },
): RecordView[] {
  const byId = new Map<string, FoldRecord>();
  for (const r of records) {
    if (r && typeof r.id === "string" && !byId.has(r.id)) byId.set(r.id, r);
  }
  const orphanSet = new Set<string>(out.orphans);

  // Application order first, then orphans, then anything neither covers
  // (defensive — derive partitions every id into order ∪ orphans).
  const seen = new Set<string>();
  const seq: string[] = [];
  const pushId = (id: string): void => {
    if (!seen.has(id) && byId.has(id)) {
      seen.add(id);
      seq.push(id);
    }
  };
  for (const id of out.order) pushId(id);
  for (const id of out.orphans) pushId(id);
  for (const id of byId.keys()) pushId(id);

  return seq.map((id) => {
    const rec = byId.get(id) as FoldRecord;
    return {
      id,
      repo: origin.get(id) ?? null,
      kind: rec.kind,
      parents: rec.parents ?? [],
      effects: rec.effects ?? {},
      reason: rec.reason,
      orphan: orphanSet.has(id),
      recordsBehind: Object.prototype.hasOwnProperty.call(freshness, id)
        ? freshness[id]
        : null,
    };
  });
}

// ---------------------------------------------------------------------------
// projection page
// ---------------------------------------------------------------------------

/** Compact one-line summary of a record's effects, e.g. "config.timeout=45,
 * feature.cache=on". Empty effects render as an em dash. */
function effectsSummary(effects: unknown): string {
  if (!effects || typeof effects !== "object" || Array.isArray(effects)) {
    return "—";
  }
  const parts = Object.keys(effects as { [k: string]: unknown })
    .sort()
    .map((k) => `${k}=${display((effects as { [k: string]: unknown })[k])}`);
  return parts.length ? parts.join(", ") : "—";
}

function renderProjection(
  project: string,
  out: DeriveOutput,
  recordCount: number,
  repos: string[],
  recordsView: RecordView[],
): string {
  const recordRows = recordsView
    .map((r) => {
      const parents =
        Array.isArray(r.parents) && r.parents.length
          ? r.parents.map((p) => esc(String(p))).join(", ")
          : "—";
      const kind = r.kind === undefined || r.kind === null ? "—" : String(r.kind);
      const behind =
        r.recordsBehind !== null
          ? ` <span class="meta">(${r.recordsBehind} behind)</span>`
          : "";
      const kindCell = r.orphan
        ? `${esc(kind)} <em>(orphan)</em>`
        : esc(kind) + behind;
      const from = r.repo ? `<span class="meta">${esc(r.repo)}</span>` : "";
      return (
        `<tr><td><code>${esc(r.id)}</code>${from ? "<br>" + from : ""}</td>` +
        `<td>${kindCell}</td><td>${parents}</td>` +
        `<td>${esc(effectsSummary(r.effects))}</td></tr>`
      );
    })
    .join("\n");
  const stateRows = Object.keys(out.state)
    .sort()
    .map(
      (k) =>
        `<tr><td><code>${esc(k)}</code></td><td>${esc(display(out.state[k]))}</td></tr>`,
    )
    .join("\n");
  const conflictRows =
    out.conflicts.length === 0
      ? `<tr><td colspan="3">no contested keys</td></tr>`
      : out.conflicts
          .map(
            (c) =>
              `<tr><td><code>${esc(c.key)}</code></td><td>${esc(c.winner)}</td>` +
              `<td>${c.losers.map(esc).join(", ") || "—"}</td></tr>`,
          )
          .join("\n");
  const orderItems = out.order.map((id) => `<li>${esc(id)}</li>`).join("\n");
  const orphanItems =
    out.orphans.length === 0
      ? "<li>none</li>"
      : out.orphans.map((id) => `<li>${esc(id)}</li>`).join("\n");

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>git.pp — ${esc(project)}</title>
<style>
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 2rem auto; max-width: 60rem; padding: 0 1rem; color: #111; }
  h1 { font-size: 1.4rem; margin-bottom: 0.2rem; }
  h2 { font-size: 1.05rem; margin-top: 2rem; border-bottom: 1px solid #ccc;
       padding-bottom: 0.25rem; }
  .meta { color: #555; font-size: 0.85rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ddd; padding: 0.35rem 0.6rem; text-align: left;
           vertical-align: top; }
  th { background: #f4f4f4; }
  code { font-size: 0.92em; }
</style>
</head>
<body>
<header>
  <h1>git.pp — project <code>${esc(project)}</code></h1>
  <p class="meta">${recordCount} records collected from ${repos.length} repo(s)
  (${repos.map(esc).join(", ")}) · derived ${new Date().toISOString()} ·
  this page is a projection: a pure function of the record, recomputed per request.</p>
</header>

<h2>The record</h2>
<p class="meta">The atom is the record, not the state. Each entry below is a
task completion, witness observation, or reasoned override, annotated with the
repo it was collected from and listed in the fold's application order. The state
section that follows is a pure fold of exactly these entries — read the record
first, then the state it folds to.</p>
<table>
  <tr><th>id (repo)</th><th>kind</th><th>parents</th><th>effects</th></tr>
${recordRows}
</table>

<h2>Derived state</h2>
<table>
  <tr><th>key</th><th>value</th></tr>
${stateRows}
</table>

<h2>Conflicts</h2>
<p class="meta">Losers are preserved: each losing record still lives in its
own repo and can be cloned, diffed, and promoted. Losing is free.</p>
<table>
  <tr><th>key</th><th>winner</th><th>preserved losers</th></tr>
${conflictRows}
</table>

<h2>Application order</h2>
<ol>
${orderItems}
</ol>

<h2>Orphans (excluded: ancestry not fully present)</h2>
<ul>
${orphanItems}
</ul>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// handlers
// ---------------------------------------------------------------------------

async function handleState(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const pp = projectParam(url);
  if ("response" in pp) return pp.response;
  const { out } = await projectDerivation(env, pp.project);
  return json(out);
}

async function handleRoot(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const pp = projectParam(url);
  if ("response" in pp) return pp.response;
  const project = pp.project;
  const { out, records, recordCount, repos, origin } = await projectDerivation(
    env,
    project,
  );
  const recordsView = buildRecordsView(records, out, origin, witnessFreshness(records));
  return new Response(
    renderProjection(project, out, recordCount, repos, recordsView),
    {
      headers: { "content-type": "text/html; charset=utf-8" },
    },
  );
}

async function handleRecords(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const pp = projectParam(url);
  if ("response" in pp) return pp.response;
  const project = pp.project;
  const { out, records, repos, origin, warnings } = await projectDerivation(
    env,
    project,
  );
  const view = buildRecordsView(records, out, origin, witnessFreshness(records));
  return json({
    project,
    count: view.length,
    repos,
    records: view,
    collectionWarnings: warnings,
  });
}

async function handleListRepos(env: Env): Promise<Response> {
  const repos = await listAllRepos(env);
  // Public route: the raw binding metadata goes through the same credential
  // scrub as create/fork responses, so no token-shaped field can ever ride
  // along if the binding's list shape grows one.
  return json({ repos: repos.map((r) => publicMeta(r)), count: repos.length });
}

async function readJsonBody(
  request: Request,
): Promise<{ [k: string]: unknown } | null> {
  try {
    const body = (await request.json()) as unknown;
    if (body && typeof body === "object" && !Array.isArray(body)) {
      return body as { [k: string]: unknown };
    }
    return null;
  } catch {
    return null;
  }
}

const SECRET_KEY_RE = /token|secret|password|credential|api[-_]?key/i;

/** Deep-scrub a binding metadata object before it leaves the Worker (F3):
 * keys that look like credentials are removed at EVERY depth, not just the
 * top level, so a future binding shape cannot smuggle a token into a
 * response. Field order and all non-secret values are preserved, so for
 * the binding shapes that exist today the output is byte-identical. */
function scrubMeta(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(scrubMeta);
  if (value && typeof value === "object") {
    const out: { [k: string]: unknown } = {};
    for (const [k, v] of Object.entries(value)) {
      if (SECRET_KEY_RE.test(k)) continue;
      out[k] = scrubMeta(v);
    }
    return out;
  }
  return value;
}

function publicMeta(meta: RepoMeta): { [k: string]: unknown } {
  return scrubMeta(meta ?? {}) as { [k: string]: unknown };
}

async function handleCreateRepo(request: Request, env: Env): Promise<Response> {
  const body = await readJsonBody(request);
  const name = body?.name;
  if (typeof name !== "string" || !NAME_RE.test(name)) {
    return json({ error: "body {name} required, [A-Za-z0-9._-], max 100" }, 400);
  }
  let meta: RepoMeta;
  try {
    meta = await env.ARTIFACTS.create(name, {
      setDefaultBranch: "main",
      description: typeof body?.description === "string" ? body.description : undefined,
    });
  } catch (err) {
    // A duplicate name is a client conflict, not a Worker fault: the
    // binding rejects it, and that rejection answers 409 with a stable
    // body instead of escaping as a 500. Any other binding failure
    // rethrows and surfaces as the bare 500.
    const msg = err instanceof Error ? err.message : String(err);
    if (/already exists/i.test(msg)) {
      return json({ error: "repo already exists", name }, 409);
    }
    throw err;
  }
  return json({ created: publicMeta(meta) }, 201);
}

async function handleForkRepo(
  request: Request,
  env: Env,
  source: string,
): Promise<Response> {
  const body = await readJsonBody(request);
  const name = body?.name;
  if (typeof name !== "string" || !NAME_RE.test(name)) {
    return json({ error: "body {name} required, [A-Za-z0-9._-], max 100" }, 400);
  }
  const repo = await env.ARTIFACTS.get(source);
  const meta = await repo.fork(name, {});
  return json({ forked: publicMeta(meta), source }, 201);
}

async function handleToken(
  request: Request,
  env: Env,
  repoName: string,
): Promise<Response> {
  const body = (await readJsonBody(request)) ?? {};
  if (
    body.scope !== undefined &&
    (typeof body.scope !== "string" ||
      body.scope.length === 0 ||
      body.scope.length > 32)
  ) {
    return json({ error: "scope must be a short string" }, 400);
  }
  if (
    body.ttl !== undefined &&
    (typeof body.ttl !== "number" ||
      !Number.isFinite(body.ttl) ||
      body.ttl <= 0 ||
      body.ttl > 30 * 24 * 3600)
  ) {
    return json(
      { error: "ttl must be a positive number of seconds (max 30 days)" },
      400,
    );
  }
  const scope = typeof body.scope === "string" ? body.scope : "write";
  const ttl = typeof body.ttl === "number" ? body.ttl : undefined;
  const repo = await env.ARTIFACTS.get(repoName);
  const tok = await repo.createToken(scope, ttl);
  if (typeof tok === "string") return json({ plaintext: tok, expiresAt: null });
  return json({ plaintext: tok.plaintext, expiresAt: tok.expiresAt ?? null });
}

async function handleDeleteRepo(env: Env, name: string): Promise<Response> {
  await env.ARTIFACTS.delete(name);
  return json({ deleted: name });
}

async function handleDebugLog(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const repoName = url.searchParams.get("repo");
  if (!repoName) return json({ error: "pass ?repo=<name>" }, 400);
  if (!NAME_RE.test(repoName)) {
    return json({ error: "invalid repo name" }, 400);
  }
  const repo = await env.ARTIFACTS.get(repoName);
  const log = await repo.log({
    ref: url.searchParams.get("ref") ?? "main",
    limit: 1000,
  });
  return json({ repo: repoName, log });
}

async function handleLastEvent(env: Env): Promise<Response> {
  if (!env.EVENTS) return json({ error: "EVENTS KV not bound" }, 501);
  const raw = await env.EVENTS.get("last-push");
  return json({ lastPush: raw ? JSON.parse(raw) : null });
}

/* There is deliberately NO fetch route for event ingest (finding F2,
 * closed in the production pass): the only writer of the KV last-push
 * mirror is the Queue consumer below, fed by Artifacts event
 * subscriptions. A public POST /api/events used to let anyone forge a
 * push envelope into that mirror; the route is removed, not gated, so
 * the write surface is exactly the subscription path. Derivation never
 * depended on events in the first place. */

// ---------------------------------------------------------------------------
// router + queue consumer
// ---------------------------------------------------------------------------

interface QueueMessageLike {
  body: unknown;
  ack(): void;
}
interface MessageBatchLike {
  messages: QueueMessageLike[];
  queue: string;
}

export default {
  /** Event-subscription consumer: Artifacts delivers repo events to a
   * Queue (gitpp-events); this handler records the latest envelope in
   * KV (key "last-push") for observability and acks. The projection
   * itself is derived per request from the record — events are an
   * optimisation/observability path, never a correctness dependency. */
  async queue(batch: MessageBatchLike, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      try {
        if (env.EVENTS) {
          await env.EVENTS.put(
            "last-push",
            JSON.stringify({
              receivedAt: new Date().toISOString(),
              via: "queue:" + batch.queue,
              envelope: msg.body,
            }),
          );
        }
      } finally {
        msg.ack();
      }
    }
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // Every dispatch is awaited inside the try: returning a handler's
    // promise un-awaited would let its rejection escape this catch and
    // surface as an uncontrolled runtime 500 instead of the clean body
    // below (and synchronous throws used to leak err.message with it).
    try {
      if (method === "GET" && path === "/") return await handleRoot(request, env);
      if (method === "GET" && path === "/api/state") {
        return await handleState(request, env);
      }
      if (method === "GET" && path === "/api/records") {
        return await handleRecords(request, env);
      }
      if (method === "GET" && path === "/api/repos") {
        return await handleListRepos(env);
      }

      const gated = (fn: () => Promise<Response>): Promise<Response> =>
        authed(request, env)
          ? fn()
          : Promise.resolve(json({ error: "unauthorized" }, 401));

      if (method === "POST" && path === "/api/repos") {
        return await gated(() => handleCreateRepo(request, env));
      }
      const forkMatch = path.match(/^\/api\/repos\/([^/]+)\/fork$/);
      if (method === "POST" && forkMatch) {
        return await gated(async () => {
          const source = pathName(forkMatch[1]);
          return source === null
            ? json({ error: "invalid repo name" }, 400)
            : handleForkRepo(request, env, source);
        });
      }
      const deleteMatch = path.match(/^\/api\/repos\/([^/]+)$/);
      if (method === "DELETE" && deleteMatch) {
        return await gated(async () => {
          const name = pathName(deleteMatch[1]);
          return name === null
            ? json({ error: "invalid repo name" }, 400)
            : handleDeleteRepo(env, name);
        });
      }
      const tokenMatch = path.match(/^\/api\/repos\/([^/]+)\/token$/);
      if (method === "POST" && tokenMatch) {
        return await gated(async () => {
          const name = pathName(tokenMatch[1]);
          return name === null
            ? json({ error: "invalid repo name" }, 400)
            : handleToken(request, env, name);
        });
      }
      if (method === "GET" && path === "/api/debug/log") {
        return await gated(() => handleDebugLog(request, env));
      }
      if (method === "GET" && path === "/api/last-event") {
        return await gated(() => handleLastEvent(env));
      }

      return json({ error: "not found" }, 404);
    } catch (err) {
      // Never surface internals: binding errors can carry ids and paths.
      // The detail goes to the Worker's log, not to the caller.
      console.error("gitpp: handler error", err);
      return json({ error: "internal" }, 500);
    }
  },
};
