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
  deriveDelta,
  witnessFreshness,
  type Checkpoint,
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

function json(
  data: unknown,
  status = 200,
  extraHeaders?: { [k: string]: string },
): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...(extraHeaders ?? {}) },
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

function authed(request: Request, env: Env): boolean {
  if (!env.ADMIN_KEY) return false; // fail closed if the secret is unset
  const provided = request.headers.get("x-admin-key");
  return provided !== null && provided === env.ADMIN_KEY;
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

// ---------------------------------------------------------------------------
// checkpoint + tail (delta reads). A project may store a checkpoint.json at the
// project repo's HEAD: a fold Checkpoint (at / state / closureRoot / closure)
// plus a cover manifest. On the read path, records already in the checkpoint's
// closure are served FROM the checkpoint (not re-read); only the TAIL (records
// outside the closure) is read from Artifacts. The namespace `list` already
// returns each repo's updatedAt, so a covered TASK repo is skipped with no
// per-repo call when its updatedAt is unchanged; a covered PROJECT commit is
// skipped by its immutable commit id. derive() then runs over closure ∪ tail —
// the full record set — so every response is byte-identical to the full fold,
// which stays the reference (and the silent fallback for any doubt).
// ---------------------------------------------------------------------------

interface StoredCheckpoint extends Checkpoint {
  // project-repo commit id -> the record id that commit contributed to the
  // closure. Commit ids are immutable, so a match is a sound skip.
  coverProjectCommits?: { [commitId: string]: string };
  // task repo name -> { updatedAt at checkpoint time, the record id in the
  // closure }. A task repo is skipped only while its CURRENT updatedAt matches.
  coverTaskRepos?: { [repo: string]: { updatedAt?: string | null; id: string } };
}

function repoUpdatedAt(meta: RepoMeta): string | null {
  const v = (meta as { updatedAt?: unknown }).updatedAt;
  return typeof v === "string" ? v : null;
}

async function readCheckpoint(
  repo: ArtifactsRepo,
): Promise<StoredCheckpoint | null> {
  let parsed: unknown;
  try {
    const b = await repo.readFile({ ref: "main", path: "checkpoint.json" });
    if (!b) return null;
    parsed = JSON.parse(await b.text());
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const c = parsed as { [k: string]: unknown };
  if (
    typeof c.at !== "string" ||
    typeof c.closureRoot !== "string" ||
    typeof c.state !== "object" ||
    c.state === null ||
    Array.isArray(c.state) ||
    !Array.isArray(c.closure)
  ) {
    return null;
  }
  return parsed as StoredCheckpoint;
}

interface Collection {
  records: FoldRecord[];
  repos: string[];
  origin: Map<string, string>;
  warnings: CollectionWarning[];
  // Records collected from outside the checkpoint's closure (the freshly-read
  // tail). Only meaningful when a checkpoint drove the collection.
  tail: FoldRecord[];
  // Whether every covered repo/commit the checkpoint named was present and
  // unchanged, so the cache was safe to use. False forces the full fallback.
  coverIntact: boolean;
}

async function collectProject(
  env: Env,
  project: string,
  checkpoint?: StoredCheckpoint | null,
): Promise<Collection> {
  const all = await listAllRepos(env);
  const updatedAt = new Map<string, string | null>();
  for (const r of all) {
    if (typeof r.name === "string") updatedAt.set(r.name, repoUpdatedAt(r));
  }
  const names = all
    .map((r) => r.name)
    .filter(
      (n): n is string =>
        typeof n === "string" && (n === project || n.startsWith(project + "--")),
    )
    .sort();

  // Index the checkpoint's closure records by id for cache lookups.
  const closureById = new Map<string, FoldRecord>();
  if (checkpoint) {
    for (const r of checkpoint.closure) {
      const id = (r as { id?: unknown }).id;
      if (typeof id === "string") closureById.set(id, r);
    }
  }
  // Every covered repo/commit must still be present and (for task repos)
  // unchanged, or the cache no longer reflects reality — force full fallback.
  let coverIntact = true;

  // Ordered read plan: project-repo commits first (deterministic log order),
  // then task repos in sorted name order — exactly the full collection's order,
  // so dedupe ("first-seen wins"), origin attribution and warnings are
  // byte-identical whether an entry is read or served from the checkpoint.
  // `cache` holds record.json text served from the checkpoint (no network);
  // `tail` marks an entry read fresh (outside the closure).
  const plan: {
    repo: string;
    cache: string | null;
    read: () => Promise<string | null>;
    tail: boolean;
  }[] = [];

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
      const coveredId = checkpoint?.coverProjectCommits?.[c];
      const cachedRec = coveredId ? closureById.get(coveredId) : undefined;
      const cache = cachedRec ? JSON.stringify(cachedRec) : null;
      plan.push({
        repo: project,
        cache,
        tail: cache === null,
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

  for (const name of names) {
    if (name === project) continue;
    const cover = checkpoint?.coverTaskRepos?.[name];
    const cachedRec = cover ? closureById.get(cover.id) : undefined;
    // Serve from the checkpoint only when the record is in the closure AND the
    // repo's updatedAt is unchanged since the checkpoint was taken.
    const unchanged =
      cover !== undefined &&
      cachedRec !== undefined &&
      cover.updatedAt != null &&
      updatedAt.get(name) != null &&
      updatedAt.get(name) === cover.updatedAt;
    const cache = unchanged ? JSON.stringify(cachedRec) : null;
    plan.push({
      repo: name,
      cache,
      tail: cache === null,
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

  // Read only the uncovered (tail) entries; covered entries use their cache.
  const texts = await mapBounded(plan, COLLECT_CONCURRENCY, (p) =>
    p.cache !== null ? Promise.resolve(p.cache) : p.read(),
  );

  const byId = new Map<string, FoldRecord>();
  const origin = new Map<string, string>();
  const keptNorm = new Map<string, string>();
  const warnings: CollectionWarning[] = [];
  const tail: FoldRecord[] = [];
  // Closure record ids actually served from the checkpoint cache (i.e. whose
  // source repo/commit is still present). If this does not cover the whole
  // closure, some covered record's source vanished since the checkpoint — the
  // reconstruction would then carry a record reality no longer has, so we fall
  // back. (A covered task repo whose content CHANGED is caught separately:
  // it is re-read as tail and deriveDelta rejects the tail/closure id clash.)
  const servedFromCache = new Set<string>();
  for (let i = 0; i < plan.length; i++) {
    const text = texts[i];
    if (text === null) continue;
    let rec: FoldRecord;
    try {
      rec = JSON.parse(text) as FoldRecord;
    } catch {
      continue;
    }
    if (!rec || typeof rec.id !== "string") continue;
    if (plan[i].tail) tail.push(rec);
    else servedFromCache.add(rec.id);
    const norm = JSON.stringify(rec);
    if (!byId.has(rec.id)) {
      byId.set(rec.id, rec);
      origin.set(rec.id, plan[i].repo);
      keptNorm.set(rec.id, norm);
    } else if (keptNorm.get(rec.id) !== norm) {
      warnings.push({
        id: rec.id,
        keptFrom: origin.get(rec.id) ?? null,
        droppedFrom: plan[i].repo,
      });
    }
  }

  if (checkpoint) {
    for (const id of closureById.keys()) {
      if (!servedFromCache.has(id)) {
        coverIntact = false; // a covered record's source vanished/changed
        break;
      }
    }
  }

  return { records: [...byId.values()], repos: names, origin, warnings, tail, coverIntact };
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
  derivedVia: "delta" | "full";
}> {
  // Read the project's checkpoint (if any) once, from the project repo HEAD.
  let checkpoint: StoredCheckpoint | null = null;
  try {
    const repo = await env.ARTIFACTS.get(project);
    checkpoint = await readCheckpoint(repo);
  } catch {
    checkpoint = null;
  }

  if (checkpoint) {
    const col = await collectProject(env, project, checkpoint);
    // Delta derivation: derive(closure ∪ tail). deriveDelta re-verifies the
    // checkpoint (closure-root recompute, VERIFIED state, closure completeness,
    // tail/closure disjointness) and returns null on ANY doubt — then we fall
    // back to the full fold below, which re-reads everything.
    if (col.coverIntact) {
      const out = deriveDelta(checkpoint, col.tail);
      if (out !== null) {
        return {
          out,
          records: col.records,
          recordCount: col.records.length,
          repos: col.repos,
          origin: col.origin,
          warnings: col.warnings,
          derivedVia: "delta",
        };
      }
    }
  }

  // Full fold: the reference path. No checkpoint, or any doubt about one.
  const col = await collectProject(env, project, null);
  return {
    out: derive(col.records),
    records: col.records,
    recordCount: col.records.length,
    repos: col.repos,
    origin: col.origin,
    warnings: col.warnings,
    derivedVia: "full",
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
  const project = url.searchParams.get("project") ?? DEFAULT_PROJECT;
  const { out, derivedVia } = await projectDerivation(env, project);
  // The JSON body is exactly derive()'s output (byte-identical to the full
  // fold); how it was derived is surfaced in a header only, for observability.
  return json(out, 200, { "x-gitpp-derivation": derivedVia });
}

async function handleRoot(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const project = url.searchParams.get("project") ?? DEFAULT_PROJECT;
  const { out, records, recordCount, repos, origin, derivedVia } =
    await projectDerivation(env, project);
  const recordsView = buildRecordsView(records, out, origin, witnessFreshness(records));
  return new Response(
    renderProjection(project, out, recordCount, repos, recordsView),
    {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "x-gitpp-derivation": derivedVia,
      },
    },
  );
}

async function handleRecords(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const project = url.searchParams.get("project") ?? DEFAULT_PROJECT;
  const { out, records, repos, origin, warnings, derivedVia } =
    await projectDerivation(env, project);
  const view = buildRecordsView(records, out, origin, witnessFreshness(records));
  return json(
    {
      project,
      count: view.length,
      repos,
      records: view,
      collectionWarnings: warnings,
    },
    200,
    { "x-gitpp-derivation": derivedVia },
  );
}

async function handleListRepos(env: Env): Promise<Response> {
  const repos = await listAllRepos(env);
  return json({ repos, count: repos.length });
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

function publicMeta(meta: RepoMeta): { [k: string]: unknown } {
  const { token: _token, ...rest } = meta ?? {};
  return rest;
}

async function handleCreateRepo(request: Request, env: Env): Promise<Response> {
  const body = await readJsonBody(request);
  const name = body?.name;
  if (typeof name !== "string" || !NAME_RE.test(name)) {
    return json({ error: "body {name} required, [A-Za-z0-9._-], max 100" }, 400);
  }
  const meta = await env.ARTIFACTS.create(name, {
    setDefaultBranch: "main",
    description: typeof body?.description === "string" ? body.description : undefined,
  });
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

/** Manual event ingest (observability only — the production event path
 * is the queue consumer below; event subscriptions deliver to a Queue,
 * not to a fetch route). Records the raw envelope in KV. Derivation
 * never depends on events: /api/state re-derives from the record on
 * every request. */
async function handleEvent(request: Request, env: Env): Promise<Response> {
  if (!env.EVENTS) return json({ error: "EVENTS KV not bound" }, 501);
  let envelope: unknown = null;
  try {
    envelope = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const record = {
    receivedAt: new Date().toISOString(),
    headers: {
      eventType: request.headers.get("cf-event-type"),
      eventSource: request.headers.get("cf-event-source"),
    },
    envelope,
  };
  await env.EVENTS.put("last-push", JSON.stringify(record));
  return json({ recorded: true });
}

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

    try {
      if (method === "GET" && path === "/") return handleRoot(request, env);
      if (method === "GET" && path === "/api/state") {
        return handleState(request, env);
      }
      if (method === "GET" && path === "/api/records") {
        return handleRecords(request, env);
      }
      if (method === "GET" && path === "/api/repos") {
        return handleListRepos(env);
      }

      const gated = (fn: () => Promise<Response>): Promise<Response> =>
        authed(request, env)
          ? fn()
          : Promise.resolve(json({ error: "unauthorized" }, 401));

      if (method === "POST" && path === "/api/repos") {
        return gated(() => handleCreateRepo(request, env));
      }
      const forkMatch = path.match(/^\/api\/repos\/([^/]+)\/fork$/);
      if (method === "POST" && forkMatch) {
        return gated(() =>
          handleForkRepo(request, env, decodeURIComponent(forkMatch[1])),
        );
      }
      const deleteMatch = path.match(/^\/api\/repos\/([^/]+)$/);
      if (method === "DELETE" && deleteMatch) {
        return gated(() =>
          handleDeleteRepo(env, decodeURIComponent(deleteMatch[1])),
        );
      }
      const tokenMatch = path.match(/^\/api\/repos\/([^/]+)\/token$/);
      if (method === "POST" && tokenMatch) {
        return gated(() =>
          handleToken(request, env, decodeURIComponent(tokenMatch[1])),
        );
      }
      if (method === "GET" && path === "/api/debug/log") {
        return gated(() => handleDebugLog(request, env));
      }
      if (method === "GET" && path === "/api/last-event") {
        return gated(() => handleLastEvent(env));
      }
      if (method === "POST" && path === "/api/events") {
        return handleEvent(request, env);
      }

      return json({ error: "not found" }, 404);
    } catch (err) {
      return json(
        { error: "internal", message: err instanceof Error ? err.message : String(err) },
        500,
      );
    }
  },
};
