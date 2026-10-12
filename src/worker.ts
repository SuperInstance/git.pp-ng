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

async function collectProject(
  env: Env,
  project: string,
): Promise<{
  records: FoldRecord[];
  repos: string[];
  origin: Map<string, string>;
}> {
  const all = await listAllRepos(env);
  const names = all
    .map((r) => r.name)
    .filter(
      (n): n is string =>
        typeof n === "string" && (n === project || n.startsWith(project + "--")),
    )
    .sort();

  const byId = new Map<string, FoldRecord>();
  // record id -> the repo the record was first collected from. First-seen
  // wins, exactly as the dedupe below does, so the project repo (walked
  // first) is credited for records that also appear at a task HEAD.
  const origin = new Map<string, string>();
  const addBlob = async (
    blob: Blob | null,
    repoName: string,
  ): Promise<void> => {
    if (!blob) return;
    try {
      const rec = JSON.parse(await blob.text()) as FoldRecord;
      if (rec && typeof rec.id === "string" && !byId.has(rec.id)) {
        byId.set(rec.id, rec);
        origin.set(rec.id, repoName);
      }
    } catch {
      /* not a record — skip silently */
    }
  };

  // Project repo: every commit carries one record.
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
      try {
        await addBlob(await repo.readFile({ ref: c, path: "record.json" }), project);
      } catch {
        /* missing record.json at this ref — skip */
      }
    }
  } catch {
    /* project repo missing/unready — derive over what exists */
  }

  // Task repos: the task's record at HEAD.
  for (const name of names) {
    if (name === project) continue;
    try {
      const repo = await env.ARTIFACTS.get(name);
      await addBlob(await repo.readFile({ ref: "main", path: "record.json" }), name);
    } catch {
      /* skip */
    }
  }

  return { records: [...byId.values()], repos: names, origin };
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
}> {
  const { records, repos, origin } = await collectProject(env, project);
  return { out: derive(records), records, recordCount: records.length, repos, origin };
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
  const { out } = await projectDerivation(env, project);
  return json(out);
}

async function handleRoot(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const project = url.searchParams.get("project") ?? DEFAULT_PROJECT;
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
  const project = url.searchParams.get("project") ?? DEFAULT_PROJECT;
  const { out, records, repos, origin } = await projectDerivation(env, project);
  const view = buildRecordsView(records, out, origin, witnessFreshness(records));
  return json({ project, count: view.length, repos, records: view });
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
