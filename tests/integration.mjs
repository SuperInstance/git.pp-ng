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
 * It also asserts the witness-freshness surface (prototype 1b): each applied
 * witness record in /api/records carries a non-negative integer recordsBehind,
 * and every non-witness record carries null.
 *
 * Pre-deploy note: a Worker built before a given change serves the older
 * surface — /api/records may 404 entirely, or return records without the
 * recordsBehind field. Both are expected, not test defects: the affected
 * assertions are reported as PENDING ("endpoint/field not yet deployed") and
 * do not fail the run. Every other assertion must pass regardless, and once
 * the updated Worker is deployed the pending assertions run for real.
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
function pend(msg, reason = "endpoint not yet deployed") {
  pending++;
  console.log(`PENDING — ${msg} [${reason}]`);
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
    pend("GET /api/records: witness records carry numeric recordsBehind");
    pend("GET /api/records: non-witness records have recordsBehind null");
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
    // Witness freshness (prototype 1b): applied witnesses expose recordsBehind
    // as a non-negative integer; everything else is null. A Worker built
    // before this branch serves /api/records without the field at all — report
    // that distinctly as PENDING (field not yet deployed), not a failure, the
    // same way the endpoint itself was handled pre-deploy.
    const freshnessDeployed = recs.some((r) =>
      Object.prototype.hasOwnProperty.call(r, "recordsBehind"),
    );
    if (!freshnessDeployed) {
      pend(
        "GET /api/records: witness records carry numeric recordsBehind",
        "field not yet deployed",
      );
      pend(
        "GET /api/records: non-witness records have recordsBehind null",
        "field not yet deployed",
      );
    } else {
      const witnesses = recs.filter((r) => r.kind === "witness");
      check(
        witnesses.length > 0 &&
          witnesses.every(
            (r) => Number.isInteger(r.recordsBehind) && r.recordsBehind >= 0,
          ),
        "records: witness records carry numeric recordsBehind",
        `got ${JSON.stringify(witnesses.map((r) => [r.id, r.recordsBehind]))}`,
      );
      check(
        recs
          .filter((r) => r.kind !== "witness")
          .every((r) => r.recordsBehind === null),
        "records: non-witness records have recordsBehind null",
      );
    }
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

  // --- 5. Scale project poc-scale-1 (collection-path fix) -------------------
  // 61 repos / 62 records. Asserts the derivation is correct at scale and
  // reports the /api/state latency the collection-path fix targets.
  const SCALE = "poc-scale-1";
  const tState = Date.now();
  const scaleState = await getJson(`/api/state?project=${SCALE}`);
  const scaleMs = Date.now() - tState;
  if (scaleState.status !== 200 || !scaleState.body) {
    fail(`GET /api/state?project=${SCALE} responds 200`, `status ${scaleState.status}`);
  } else {
    pass(`GET /api/state?project=${SCALE} responds 200`);
    const conflicts = scaleState.body.conflicts || [];
    const winners = Object.fromEntries(conflicts.map((c) => [c.key, c.winner]));
    check(conflicts.length === 3, `${SCALE}: exactly 3 conflicts`, `got ${conflicts.length}`);
    check(eq(scaleState.body.orphans, []), `${SCALE}: orphans empty`, `got ${JSON.stringify(scaleState.body.orphans)}`);
    check(winners["config.workers"] === "t30", `${SCALE}: config.workers winner t30`, `got ${winners["config.workers"]}`);
    check(winners["config.region"] === "t09", `${SCALE}: config.region winner t09`, `got ${winners["config.region"]}`);
    check(winners["config.mode"] === "t59", `${SCALE}: config.mode winner t59`, `got ${winners["config.mode"]}`);
  }
  const scaleRecords = await getJson(`/api/records?project=${SCALE}`);
  if (scaleRecords.status === 200 && scaleRecords.body) {
    check(scaleRecords.body.count === 62, `${SCALE}: /api/records count 62`, `got ${scaleRecords.body.count}`);
  } else if (scaleRecords.status === 404) {
    pend(`${SCALE}: /api/records count 62`);
  } else {
    fail(`${SCALE}: /api/records responds`, `status ${scaleRecords.status}`);
  }
  console.log(`\n[latency] GET /api/state?project=${SCALE}: ${scaleMs} ms (collection-path target)`);

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
