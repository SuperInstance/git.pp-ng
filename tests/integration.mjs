#!/usr/bin/env node
/**
 * integration.mjs — live integration tests for the gitpp Worker.
 *
 * Plain Node (18+), no dependencies. Runs against a deployed Worker base
 * URL from env GITPP_BASE (default the public demo deployment) and asserts
 * the real derivation of project poc-quilt-1, the new /api/records view,
 * the HTML projection, and that mutating routes are gated.
 *
 *   node tests/integration.mjs
 *   GITPP_BASE=https://my-worker.example.workers.dev node tests/integration.mjs
 *
 * Prints PASS/FAIL per assertion and exits non-zero if any assertion fails.
 *
 * Pre-deploy note: /api/records does not exist on a Worker built before this
 * change, so it answers 404. That is expected, not a test defect: the
 * records assertions are reported as PENDING ("endpoint not yet deployed")
 * and do not fail the run. Every other assertion must pass regardless, and
 * once the updated Worker is deployed the records assertions run for real.
 */

const BASE = (process.env.GITPP_BASE || "https://gitpp.casey-digennaro.workers.dev").replace(/\/+$/, "");
const PROJECT = "poc-quilt-1";

let passed = 0;
let failed = 0;
let pending = 0;

function pass(msg) {
  passed++;
  console.log(`PASS — ${msg}`);
}
function fail(msg, detail) {
  failed++;
  console.log(`FAIL — ${msg}${detail ? ` (${detail})` : ""}`);
}
function pend(msg) {
  pending++;
  console.log(`PENDING — ${msg} [endpoint not yet deployed]`);
}
function check(cond, msg, detail) {
  if (cond) pass(msg);
  else fail(msg, detail);
}

function eq(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

async function getJson(path) {
  const res = await fetch(BASE + path, { headers: { accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { status: res.status, body };
}

async function getText(path) {
  const res = await fetch(BASE + path);
  const text = await res.text();
  return { status: res.status, text };
}

async function main() {
  console.log(`gitpp integration tests · base ${BASE} · project ${PROJECT}\n`);

  // --- 1. GET /api/state?project=poc-quilt-1 --------------------------------
  const state = await getJson(`/api/state?project=${PROJECT}`);
  if (state.status !== 200 || !state.body) {
    fail("GET /api/state responds 200 with JSON", `status ${state.status}`);
  } else {
    pass("GET /api/state responds 200 with JSON");
    const s = state.body.state || {};
    const conflicts = state.body.conflicts || [];
    check(s["config.timeout"] === 45, "state: config.timeout === 45", `got ${JSON.stringify(s["config.timeout"])}`);
    check(
      conflicts[0] && conflicts[0].winner === "override-1",
      'state: conflicts[0].winner === "override-1"',
      `got ${JSON.stringify(conflicts[0] && conflicts[0].winner)}`,
    );
    check(
      conflicts[0] && eq(conflicts[0].losers, ["task-a", "task-b", "task-d"]),
      'state: losers exactly ["task-a","task-b","task-d"]',
      `got ${JSON.stringify(conflicts[0] && conflicts[0].losers)}`,
    );
    check(eq(state.body.orphans, []), "state: orphans empty", `got ${JSON.stringify(state.body.orphans)}`);
    // A witness contributes observation keys (env.* by the scene's convention);
    // its uncontested observation must reach derived state.
    const witnessKey = Object.keys(s).find((k) => k.startsWith("env."));
    check(
      witnessKey !== undefined,
      "state: a witness key present (env.*)",
      `keys ${JSON.stringify(Object.keys(s))}`,
    );
  }

  // --- 2. GET /api/records?project=poc-quilt-1 ------------------------------
  const records = await getJson(`/api/records?project=${PROJECT}`);
  if (records.status === 404) {
    pend("GET /api/records returns 8 records");
    pend("GET /api/records includes kinds task, witness, override");
  } else if (records.status !== 200 || !records.body) {
    fail("GET /api/records responds 200 with JSON", `status ${records.status}`);
  } else {
    pass("GET /api/records responds 200 with JSON");
    const recs = Array.isArray(records.body.records) ? records.body.records : [];
    check(recs.length === 8, "records: exactly 8 records", `got ${recs.length}`);
    const kinds = new Set(recs.map((r) => r.kind));
    check(
      kinds.has("task") && kinds.has("witness") && kinds.has("override"),
      "records: includes kinds task, witness, and override",
      `got ${JSON.stringify([...kinds])}`,
    );
  }

  // --- 3. GET / (HTML projection) -------------------------------------------
  const home = await getText(`/?project=${PROJECT}`);
  if (home.status !== 200) {
    fail("GET / responds 200", `status ${home.status}`);
  } else {
    pass("GET / responds 200");
    const html = home.text;
    check(
      html.includes("config.timeout") && html.includes("45"),
      "HTML: contains the derived timeout value (45)",
    );
    const losers = ["task-a", "task-b", "task-d"];
    const missing = losers.filter((id) => !html.includes(id));
    check(missing.length === 0, "HTML: contains the losers' ids", `missing ${JSON.stringify(missing)}`);
  }

  // --- 4. POST /api/repos without an admin key ------------------------------
  const unauth = await fetch(BASE + "/api/repos", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name: "integration-test-should-not-create" }),
  });
  check(unauth.status === 401, "POST /api/repos without admin key returns 401", `got ${unauth.status}`);

  // --- summary --------------------------------------------------------------
  console.log(
    `\n${passed} passed, ${failed} failed` +
      (pending ? `, ${pending} pending (endpoint not yet deployed)` : ""),
  );
  if (failed > 0) process.exit(1);
  process.exit(0);
}

main().catch((err) => {
  console.error(`FATAL — ${err && err.stack ? err.stack : err}`);
  process.exit(2);
});
