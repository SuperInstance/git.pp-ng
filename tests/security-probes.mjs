/**
 * security-probes.mjs — black-box probes of the Worker's security surface,
 * run against the real module with mock bindings (no network, no deploy).
 *
 *   bun tests/security-probes.mjs [path-to-worker.ts]
 *
 * Exit code 0 iff every probe's expectation for the CURRENT code holds.
 * The expectations encode the hardened behaviour; run against a pre-fix
 * tree, the probes print the vulnerable "before" transcript and exit 1.
 */
const workerPath = process.argv[2] ?? new URL("../src/worker.ts", import.meta.url).pathname;
const worker = (await import(workerPath)).default;

const KEY = "probe-admin-key";
let failures = 0;

function makeEnv() {
  const kv = new Map();
  const calls = { delete: [], createToken: [], get: [], create: [] };
  const state = { listThrows: false };
  const repoStub = (name) => ({
    info: async () => ({ name }),
    createToken: async (scope, ttl) => {
      calls.createToken.push({ name, scope, ttl });
      return { plaintext: "MINTED-SECRET", expiresAt: "2030-01-01T00:00:00Z" };
    },
    fork: async (n) => ({
      name: n, remote: "https://x/" + n, defaultBranch: "main",
      token: "FORK-TOP-TOKEN", nested: { token: "FORK-NESTED-TOKEN", keep: 1 },
    }),
    log: async () => ({ commits: [] }),
    readFile: async () => null,
  });
  const env = {
    ADMIN_KEY: KEY,
    EVENTS: {
      get: async (k) => kv.get(k) ?? null,
      put: async (k, v) => void kv.set(k, v),
    },
    ARTIFACTS: {
      create: async (name) => {
        calls.create.push(name);
        return {
          name, remote: "https://x/" + name, defaultBranch: "main",
          token: "CREATE-TOP-TOKEN", nested: { token: "CREATE-NESTED-TOKEN", keep: 1 },
        };
      },
      get: async (name) => { calls.get.push(name); return repoStub(name); },
      list: async () => {
        if (state.listThrows) throw new Error("binding exploded: /internal/path/xyz");
        return { repos: [{ name: "poc-quilt-1", remote: "https://x/poc-quilt-1", defaultBranch: "main", token: "LIST-TOKEN" }] };
      },
      delete: async (name) => { calls.delete.push(name); },
    },
  };
  return { env, kv, calls, state };
}

async function probe(name, setup, run, expect) {
  const ctx = makeEnv();
  if (setup) setup(ctx);
  const req = run(ctx);
  let res, body = "";
  try {
    res = await worker.fetch(req, ctx.env);
    body = await res.text();
  } catch (e) {
    res = { status: -1 };
    body = "THREW: " + (e && e.message);
  }
  const problems = expect(res, body, ctx);
  const ok = problems.length === 0;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  -> ${res.status} ${body.replace(/\s+/g, " ").slice(0, 140)}`);
  for (const p of problems) console.log(`      !! ${p}`);
}

const R = (method, path, opts = {}) =>
  new Request("https://probe.test" + path, {
    method,
    headers: { ...(opts.key ? { "x-admin-key": opts.key } : {}), ...(opts.json ? { "content-type": "application/json" } : {}) },
    body: opts.body,
  });
const has = (s) => (res, body) => (body.includes(s) ? [`body should not contain ${JSON.stringify(s)}`] : []);
const status = (want) => (res) => (res.status === want ? [] : [`want status ${want}`]);

// 1. F2: the public event-ingest write must not exist; KV stays untouched.
await probe("F2 POST /api/events is gone", null,
  () => R("POST", "/api/events", { json: true, body: JSON.stringify({ fake: "envelope" }) }),
  (res, body, { kv }) => [
    ...status(404)(res),
    ...(kv.has("last-push") ? ["KV last-push was written by an unauthenticated request"] : []),
  ]);

// 2. The legitimate observability read still works with the key.
await probe("GET /api/last-event (gated) still answers", null,
  () => R("GET", "/api/last-event", { key: KEY }),
  (res) => status(200)(res));

// 3. Malformed percent-encoding in a path param: clean 400, never a 500.
await probe("DELETE /api/repos/%E0%A4%A (malformed encoding)", null,
  () => R("DELETE", "/api/repos/%E0%A4%A", { key: KEY }),
  (res, body) => [...status(400)(res), ...has("URIError")(res, body), ...has("malformed")(res, body)]);

// 4. Traversal-shaped repo name never reaches the binding.
await probe("DELETE /api/repos/..%2Fevil (traversal name)", null,
  () => R("DELETE", "/api/repos/..%2Fevil", { key: KEY }),
  (res, body, { calls }) => [
    ...status(400)(res),
    ...(calls.delete.length ? [`binding delete() called with ${JSON.stringify(calls.delete)}`] : []),
  ]);

// 5. F3: create response carries no token at any depth.
await probe("POST /api/repos strips nested tokens (F3)", null,
  () => R("POST", "/api/repos", { key: KEY, json: true, body: JSON.stringify({ name: "probe-1" }) }),
  (res, body) => [...status(201)(res), ...has("CREATE-TOP-TOKEN")(res, body), ...has("CREATE-NESTED-TOKEN")(res, body), ...has('"token"')(res, body)]);

// 6. Fork response likewise.
await probe("POST /api/repos/x/fork strips nested tokens (F3)", null,
  () => R("POST", "/api/repos/poc-quilt-1/fork", { key: KEY, json: true, body: JSON.stringify({ name: "probe-2" }) }),
  (res, body) => [...status(201)(res), ...has("FORK-TOP-TOKEN")(res, body), ...has("FORK-NESTED-TOKEN")(res, body)]);

// 7. Public repo list is scrubbed the same way; other fields survive.
await probe("GET /api/repos (public) strips token fields", null,
  () => R("GET", "/api/repos"),
  (res, body) => [...status(200)(res), ...has("LIST-TOKEN")(res, body), ...(body.includes("poc-quilt-1") ? [] : ["repo name missing from list"])]);

// 8. Token route: sane ttl passes; abusive ttl is refused before the binding.
await probe("POST token with ttl=3600 still mints", null,
  () => R("POST", "/api/repos/poc-quilt-1/token", { key: KEY, json: true, body: JSON.stringify({ scope: "write", ttl: 3600 }) }),
  (res, body, { calls }) => [...status(200)(res), ...(calls.createToken.length === 1 ? [] : ["binding createToken not called"])]);
await probe("POST token with ttl=-5 refused", null,
  () => R("POST", "/api/repos/poc-quilt-1/token", { key: KEY, json: true, body: JSON.stringify({ ttl: -5 }) }),
  (res, body, { calls }) => [...status(400)(res), ...(calls.createToken.length ? ["binding createToken called with abusive ttl"] : [])]);
await probe("POST token with ttl=1e12 refused", null,
  () => R("POST", "/api/repos/poc-quilt-1/token", { key: KEY, json: true, body: JSON.stringify({ ttl: 1e12 }) }),
  (res, body, { calls }) => [...status(400)(res), ...(calls.createToken.length ? ["binding createToken called with abusive ttl"] : [])]);

// 9. Read routes validate the project name; the real one still derives.
await probe("GET /api/state?project=bad name -> 400", null,
  () => R("GET", "/api/state?project=bad%20name"),
  (res) => status(400)(res));
await probe("GET /api/state?project=poc-quilt-1 -> 200", null,
  () => R("GET", "/api/state?project=poc-quilt-1"),
  (res) => status(200)(res));
await probe("GET /api/records?project=../x -> 400", null,
  () => R("GET", "/api/records?project=..%2Fx"),
  (res) => status(400)(res));

// 10. A binding failure surfaces as a bare {error:"internal"} — no internals.
await probe("binding throw -> 500 without message leak", ({ state }) => { state.listThrows = true; },
  () => R("GET", "/api/state?project=poc-quilt-1"),
  (res, body) => [...status(500)(res), ...has("/internal/path/xyz")(res, body), ...has("binding exploded")(res, body), ...has('"message"')(res, body)]);

// 11. Auth discipline: query-string keys never authenticate; wrong/no key 401.
await probe("POST /api/repos with key only in query string -> 401", null,
  () => R("POST", "/api/repos?x-admin-key=" + KEY, { json: true, body: JSON.stringify({ name: "probe-3" }) }),
  (res, body, { calls }) => [...status(401)(res), ...(calls.create.length ? ["repo created via query-string key"] : [])]);
await probe("POST /api/repos/<n>/token wrong key -> 401", null,
  () => R("POST", "/api/repos/poc-quilt-1/token", { key: "nope", json: true, body: "{}" }),
  (res) => status(401)(res));
await probe("GET /api/debug/log no key -> 401", null,
  () => R("GET", "/api/debug/log?repo=poc-quilt-1"),
  (res) => status(401)(res));

// 12. Debug route validates the repo name before touching the binding.
await probe("GET /api/debug/log?repo=bad name -> 400", null,
  () => R("GET", "/api/debug/log?repo=bad%20name", { key: KEY }),
  (res, body, { calls }) => [...status(400)(res), ...(calls.get.length ? ["binding get() called with unvalidated name"] : [])]);

// 13. Duplicate repo create: the binding's "already exists" rejection is a
// client conflict — a clean 409 with a stable body, never a 500. A binding
// failure that is NOT a duplicate still surfaces as the bare 500 (no leak).
await probe("POST /api/repos duplicate name -> 409", ({ env }) => {
    env.ARTIFACTS.create = async () => { throw new Error("a repository with this name already exists"); };
  },
  () => R("POST", "/api/repos", { key: KEY, json: true, body: JSON.stringify({ name: "poc-quilt-1" }) }),
  (res, body) => [
    ...status(409)(res),
    ...(body.includes('"repo already exists"') ? [] : ["409 body should carry the stable error string"]),
    ...(body.includes('"poc-quilt-1"') ? [] : ["409 body should name the repo"]),
  ]);
await probe("POST /api/repos binding failure (not a duplicate) -> clean 500", ({ env }) => {
    env.ARTIFACTS.create = async () => { throw new Error("binding exploded: /internal/path/xyz"); };
  },
  () => R("POST", "/api/repos", { key: KEY, json: true, body: JSON.stringify({ name: "probe-new" }) }),
  (res, body) => [...status(500)(res), ...has("/internal/path/xyz")(res, body), ...has("binding exploded")(res, body), ...has('"message"')(res, body)]);

console.log(failures === 0 ? "\nALL PROBES PASS" : `\n${failures} probe(s) failing`);
process.exit(failures === 0 ? 0 : 1);
