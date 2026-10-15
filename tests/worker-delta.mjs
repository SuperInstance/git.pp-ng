#!/usr/bin/env node
/**
 * worker-delta.mjs — offline proof of the Worker's checkpoint+tail (delta) read
 * path. No network: the Artifacts binding is mocked in memory and every get /
 * log / readFile is counted, so we can assert both halves of the contract:
 *
 *   1. CORRECTNESS: a delta read is byte-identical to the full fold — same
 *      /api/state JSON, same /api/records JSON — for the live record sets, and
 *      the delta path falls back to the full fold on any doubt (tampered
 *      checkpoint, a re-versioned covered record, a vanished covered repo).
 *   2. READ COST: a checkpointed project reads record.json only for the tail
 *      (records outside the closure) plus the project-repo commits; the covered
 *      task repos cost neither a get nor a readFile.
 *
 *   3. FRONTIER FORM: a checkpoint whose `at` is the array of maximal applied
 *      records (docs/FRONTIER-CHECKPOINTS.md) covers the whole applied set;
 *      the read path must ENGAGE the delta path for it (readCheckpoint once
 *      rejected the array form outright) and stay byte-identical, and a
 *      tampered covered record under it still falls back to the full fold.
 *
 *   4. LIVE LAYOUT (the frontier self-clash): the checkpoint commit itself
 *      sits at HEAD of the project repo's log, uncovered by construction,
 *      and its tree inherits the previous HEAD's record.json — for
 *      poc-scale-1 that is witness-1, INSIDE the frontier closure. The
 *      inherited record is re-read as tail; being an identical duplicate
 *      of the covered record it must NOT defeat the delta path (a
 *      divergent copy under the same id still does — see 1(e)).
 *
 * The full fold stays the reference: every delta result is compared to the full
 * result computed by the same Worker with no checkpoint present.
 *
 *   node tests/worker-delta.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import worker from "../src/worker.ts";
import { buildCheckpoint, buildFrontierCheckpoint } from "../src/fold.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const LIVE = JSON.parse(
  readFileSync(join(HERE, "..", "conformance", "live-records.json"), "utf8"),
);

let passed = 0;
let failed = 0;
const check = (cond, msg, detail = "") => {
  if (cond) {
    passed++;
    console.log(`PASS — ${msg}`);
  } else {
    failed++;
    console.log(`FAIL — ${msg}${detail ? ` (${detail})` : ""}`);
  }
};

// --- in-memory Artifacts mock ---------------------------------------------
// A repo is { updatedAt, log: [commitId...] (newest first), files: {ref: {path: text}} }.
// Project repos carry one record.json per commit (+ an optional checkpoint.json
// at main); task repos carry record.json at main.
function makeEnv(repos) {
  const counts = { get: 0, log: 0, readFile: 0, list: 0 };
  const binding = {
    async list({ cursor } = {}) {
      counts.list++;
      // one page, no cursor — the whole namespace
      if (cursor) return { repos: [], cursor: null };
      return {
        repos: Object.entries(repos).map(([name, r]) => ({
          name,
          updatedAt: r.updatedAt,
        })),
        cursor: null,
      };
    },
    async get(name) {
      counts.get++;
      const r = repos[name];
      if (!r) throw new Error("no such repo: " + name);
      return {
        async log() {
          counts.log++;
          return { commits: (r.log ?? ["main"]).map((id) => ({ id })) };
        },
        async readFile({ ref, path }) {
          counts.readFile++;
          const text = r.files?.[ref]?.[path];
          if (text === undefined) return null;
          return { async text() { return text; } };
        },
      };
    },
  };
  return { env: { ARTIFACTS: binding }, counts };
}

// Build a project's repo layout from a flat record list. Records whose id is
// "genesis" or starts with "witness" live as project-repo commits; the rest are
// task repos <project>--<id>. (Mirrors the live poc-scale-1 / poc-quilt-1
// layouts closely enough to exercise both the project-commit and task-repo
// cache paths.)
// opts.checkpointCommit: add the LIVE layout's checkpoint commit at HEAD of
// the project repo — a commit whose tree INHERITS the previous HEAD's
// record.json (the newest project record) and which appears in NO cover
// manifest (it did not exist when the checkpoint it carries was built).
function layout(project, records, opts = {}) {
  const repos = {};
  const projectCommits = []; // [{commit, id}]
  const projFiles = {};
  const taskOf = {}; // id -> repo name
  let n = 0;
  const projRecs = records.filter(
    (r) => r.id === "genesis" || String(r.id).startsWith("witness"),
  );
  const taskRecs = records.filter(
    (r) => !(r.id === "genesis" || String(r.id).startsWith("witness")),
  );
  // project repo: one commit per project record (genesis first == oldest)
  const projLog = [];
  for (const r of projRecs) {
    const commit = `c-${project}-${n++}`;
    projFiles[commit] = { "record.json": JSON.stringify(r, null, 2) + "\n" };
    projLog.unshift(commit); // newest first
    projectCommits.push({ commit, id: r.id });
  }
  if (opts.checkpointCommit && projRecs.length) {
    const headRec = projRecs[projRecs.length - 1];
    const commit = `c-${project}-ckpt`;
    projFiles[commit] = {
      "record.json": JSON.stringify(headRec, null, 2) + "\n",
    };
    projLog.unshift(commit); // HEAD, uncovered by construction
  }
  repos[project] = {
    updatedAt: "2026-01-01T00:00:00.000Z",
    log: projLog.length ? projLog : ["main"],
    files: { ...projFiles, main: {} },
  };
  for (const r of taskRecs) {
    const name = `${project}--${r.id}`;
    taskOf[r.id] = name;
    repos[name] = {
      updatedAt: `2026-01-01T00:00:0${(n++ % 9) + 1}.000Z`,
      files: { main: { "record.json": JSON.stringify(r, null, 2) + "\n" } },
    };
  }
  return { repos, projectCommits, taskOf };
}

// Assemble checkpoint.json for `at` (as the runner will), writing it to the
// project repo's main ref. Covers exactly the closure records.
function installCheckpoint(repos, project, records, at, projectCommits, taskOf) {
  const cp = buildCheckpoint(records, at);
  if (!cp) throw new Error("no checkpoint at " + at);
  const closureIds = new Set(cp.closure.map((r) => r.id));
  const coverProjectCommits = {};
  for (const { commit, id } of projectCommits) {
    if (closureIds.has(id)) coverProjectCommits[commit] = id;
  }
  const coverTaskRepos = {};
  for (const id of closureIds) {
    const name = taskOf[id];
    if (name) coverTaskRepos[name] = { updatedAt: repos[name].updatedAt, id };
  }
  const stored = { v: 2, project, ...cp, coverProjectCommits, coverTaskRepos };
  repos[project].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
  return stored;
}

// Assemble a FRONTIER checkpoint.json (as the runner now writes by default):
// `at` is the array of maximal applied records and the covered set is the
// union of their closures — for these fixtures the whole applied set, so the
// cover manifest spans every repo in the layout. Frontier files carry v: 3.
function installFrontierCheckpoint(repos, project, records, projectCommits, taskOf) {
  const cp = buildFrontierCheckpoint(records);
  if (!cp) throw new Error("no frontier checkpoint for " + project);
  const closureIds = new Set(cp.closure.map((r) => r.id));
  const coverProjectCommits = {};
  for (const { commit, id } of projectCommits) {
    if (closureIds.has(id)) coverProjectCommits[commit] = id;
  }
  const coverTaskRepos = {};
  for (const id of closureIds) {
    const name = taskOf[id];
    if (name) coverTaskRepos[name] = { updatedAt: repos[name].updatedAt, id };
  }
  const stored = {
    v: Array.isArray(cp.at) ? 3 : 2,
    project,
    ...cp,
    coverProjectCommits,
    coverTaskRepos,
  };
  repos[project].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
  return stored;
}

async function stateJson(env, project) {
  const res = await worker.fetch(
    new Request(`http://x/api/state?project=${project}`),
    env,
  );
  return { body: await res.json(), via: res.headers.get("x-gitpp-derivation") };
}
async function recordsJson(env, project) {
  const res = await worker.fetch(
    new Request(`http://x/api/records?project=${project}`),
    env,
  );
  return { body: await res.json(), via: res.headers.get("x-gitpp-derivation") };
}

const S = (x) => JSON.stringify(x);

async function run() {
  for (const project of Object.keys(LIVE)) {
    const records = LIVE[project];
    // full reference (no checkpoint)
    const full = layout(project, records);
    const envFull = makeEnv(full.repos);
    const fullState = await stateJson(envFull.env, project);
    const fullRecords = await recordsJson(envFull.env, project);
    check(fullState.via === "full", `${project}: no checkpoint → full fold`);

    // pick the applied id with the largest closure as the checkpoint position
    let best = null;
    let bestN = -1;
    for (const r of records) {
      const cp = buildCheckpoint(records, r.id);
      if (cp && cp.closure.length > bestN) {
        bestN = cp.closure.length;
        best = r.id;
      }
    }
    check(best !== null, `${project}: found a checkpoint position`, "none");

    // delta: same layout + a checkpoint at `best`
    const d = layout(project, records);
    installCheckpoint(d.repos, project, records, best, d.projectCommits, d.taskOf);
    const envDelta = makeEnv(d.repos);
    const deltaState = await stateJson(envDelta.env, project);
    const deltaRecords = await recordsJson(envDelta.env, project);

    check(deltaState.via === "delta", `${project}: checkpoint present → delta path`);
    check(
      S(deltaState.body) === S(fullState.body),
      `${project}: delta /api/state byte-identical to full`,
    );
    check(
      S(deltaRecords.body) === S(fullRecords.body),
      `${project}: delta /api/records byte-identical to full`,
    );
    // read-cost: the delta /api/state (one env, two requests: state+records)
    // should read record.json far less than the full fold. Count a fresh env.
    const envCost = makeEnv(
      (() => {
        const l = layout(project, records);
        installCheckpoint(l.repos, project, records, best, l.projectCommits, l.taskOf);
        return l.repos;
      })(),
    );
    await stateJson(envCost.env, project);
    const tailReads = envCost.counts.readFile; // includes checkpoint.json + tail record.json
    const fullEnvCost = makeEnv(layout(project, records).repos);
    await stateJson(fullEnvCost.env, project);
    const fullReads = fullEnvCost.counts.readFile;
    check(
      tailReads < fullReads,
      `${project}: delta reads fewer blobs than full (${tailReads} < ${fullReads})`,
      `${tailReads} vs ${fullReads}`,
    );
    console.log(
      `       ${project}: closure@${best}=${bestN}/${records.length}; ` +
        `readFile ${fullReads} (full) → ${tailReads} (delta, incl checkpoint.json)`,
    );

    // --- fallbacks: every one must reproduce the full result exactly ---------
    // (a) tampered closure root
    {
      const l = layout(project, records);
      const stored = installCheckpoint(l.repos, project, records, best, l.projectCommits, l.taskOf);
      stored.closureRoot = "0".repeat(64);
      l.repos[project].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
      const e = makeEnv(l.repos);
      const r = await stateJson(e.env, project);
      check(r.via === "full", `${project}: tampered root → full fallback`);
      check(S(r.body) === S(fullState.body), `${project}: tampered root result still correct`);
    }
    // (b) a re-versioned covered task record (content + updatedAt changed)
    {
      const l = layout(project, records);
      installCheckpoint(l.repos, project, records, best, l.projectCommits, l.taskOf);
      // find a covered task repo and mutate it
      const stored = JSON.parse(l.repos[project].files.main["checkpoint.json"]);
      const names = Object.keys(stored.coverTaskRepos);
      if (names.length) {
        const name = names[0];
        const id = stored.coverTaskRepos[name].id;
        const mutated = { ...records.find((x) => x.id === id), effects: { reversioned: 1 } };
        l.repos[name].files.main["record.json"] = JSON.stringify(mutated, null, 2) + "\n";
        l.repos[name].updatedAt = "2099-12-31T23:59:59.000Z";
        const e = makeEnv(l.repos);
        const r = await stateJson(e.env, project);
        check(r.via === "full", `${project}: re-versioned covered record → full fallback`);
        // reference = full fold over the MUTATED layout
        const refEnv = makeEnv(
          (() => {
            const lf = layout(project, records);
            lf.repos[name] = l.repos[name];
            return lf.repos;
          })(),
        );
        const ref = await stateJson(refEnv.env, project);
        check(S(r.body) === S(ref.body), `${project}: re-versioned result matches full fold of new reality`);
      } else {
        console.log(`       ${project}: (no covered task repos to re-version)`);
      }
    }
    // (c) appending a NEW task repo (tail) keeps delta active and stays correct
    {
      const l = layout(project, records);
      installCheckpoint(l.repos, project, records, best, l.projectCommits, l.taskOf);
      const newRec = { id: "appended-x", parents: [best], kind: "task", effects: { "feature.appended": "on" } };
      l.repos[`${project}--appended-x`] = {
        updatedAt: "2030-01-01T00:00:00.000Z",
        files: { main: { "record.json": JSON.stringify(newRec, null, 2) + "\n" } },
      };
      const e = makeEnv(l.repos);
      const r = await stateJson(e.env, project);
      check(r.via === "delta", `${project}: appended tail record → delta still active`);
      // reference = full fold over records + the new one
      const refEnv = makeEnv(layout(project, [...records, newRec]).repos);
      const ref = await stateJson(refEnv.env, project);
      check(S(r.body) === S(ref.body), `${project}: appended-tail delta matches full fold`);
    }
    // (d) FRONTIER checkpoint: `at` is the array of maximal applied records;
    // the covered set is the union of their closures == the whole applied
    // set, so the cover manifest spans EVERY repo in the layout. The read
    // must engage the delta path (readCheckpoint once returned null for the
    // array form and the project silently read full) and stay byte-identical.
    {
      const l = layout(project, records);
      const stored = installFrontierCheckpoint(l.repos, project, records, l.projectCommits, l.taskOf);
      check(
        Array.isArray(stored.at) && stored.at.length >= 2,
        `${project}: frontier checkpoint carries the array form of at`,
        JSON.stringify(stored.at),
      );
      check(
        stored.closure.length === records.length,
        `${project}: frontier covered set == whole record set (${stored.closure.length}/${records.length})`,
      );
      check(
        Object.keys(stored.coverTaskRepos).length === Object.keys(l.taskOf).length &&
          Object.keys(stored.coverProjectCommits).length === l.projectCommits.length,
        `${project}: frontier cover manifest spans every repo`,
      );
      const e = makeEnv(l.repos);
      const r = await stateJson(e.env, project);
      check(r.via === "delta", `${project}: frontier checkpoint → delta path engaged`);
      check(
        S(r.body) === S(fullState.body),
        `${project}: frontier delta /api/state byte-identical to full`,
      );
      const rr = await recordsJson(e.env, project);
      check(
        S(rr.body) === S(fullRecords.body),
        `${project}: frontier delta /api/records byte-identical to full`,
      );
      // READ COST on a fresh env, one /api/state: every covered repo is
      // served from the checkpoint, so the ONLY binding calls are the
      // serial prefix — list, get(project) x2 (derivation + collection),
      // log(project), and the single readFile of checkpoint.json itself.
      // No task repo costs a get or a readFile. (Live adds one null readFile
      // for the commit that CARRIES checkpoint.json — the mock rides it on
      // the main ref; see docs/FRONTIER-CHECKPOINTS.md §4's ≈6-call table.)
      const envCost = makeEnv(
        (() => {
          const lf = layout(project, records);
          installFrontierCheckpoint(lf.repos, project, records, lf.projectCommits, lf.taskOf);
          return lf.repos;
        })(),
      );
      await stateJson(envCost.env, project);
      check(
        envCost.counts.get === 2 && envCost.counts.readFile === 1,
        `${project}: frontier read touches no covered repo (get=${envCost.counts.get}, readFile=${envCost.counts.readFile}; full readFile=${fullReads})`,
        JSON.stringify(envCost.counts),
      );
      console.log(
        `       ${project}: frontier at=${JSON.stringify(stored.at)}; binding calls ` +
          `${JSON.stringify(envCost.counts)} for a fully-covered state read`,
      );
    }
    // (e) a tampered covered record under a FRONTIER checkpoint → full
    // fallback, byte-identical to the full fold of the mutated reality.
    {
      const l = layout(project, records);
      installFrontierCheckpoint(l.repos, project, records, l.projectCommits, l.taskOf);
      const stored = JSON.parse(l.repos[project].files.main["checkpoint.json"]);
      const names = Object.keys(stored.coverTaskRepos);
      const name = names[0];
      const id = stored.coverTaskRepos[name].id;
      const mutated = { ...records.find((x) => x.id === id), effects: { reversioned: 1 } };
      l.repos[name].files.main["record.json"] = JSON.stringify(mutated, null, 2) + "\n";
      l.repos[name].updatedAt = "2099-12-31T23:59:59.000Z";
      const e = makeEnv(l.repos);
      const r = await stateJson(e.env, project);
      check(r.via === "full", `${project}: frontier + re-versioned covered record → full fallback`);
      const refEnv = makeEnv(
        (() => {
          const lf = layout(project, records);
          lf.repos[name] = l.repos[name];
          return lf.repos;
        })(),
      );
      const ref = await stateJson(refEnv.env, project);
      check(
        S(r.body) === S(ref.body),
        `${project}: frontier tamper result matches full fold of new reality`,
      );
    }
    // (f) LIVE LAYOUT — the frontier self-clash: the checkpoint commit is
    // at HEAD of the project log, in no cover manifest, carrying an
    // inherited copy of a covered, in-closure record. Its record.json is
    // read as tail; the fold must treat the identical duplicate as a
    // duplicate (drop it) and ENGAGE the delta path — before the fix,
    // deriveDelta rejected the tail/closure id clash and every frontier
    // read of poc-scale-1 silently fell back to the full fold.
    {
      const l = layout(project, records, { checkpointCommit: true });
      installFrontierCheckpoint(l.repos, project, records, l.projectCommits, l.taskOf);
      // Reference: the full fold over the SAME layout. The inherited
      // duplicate changes nothing there either (collection dedupes
      // identical duplicates), so it must equal the plain full fold.
      const refState = await stateJson(
        makeEnv(layout(project, records, { checkpointCommit: true }).repos).env,
        project,
      );
      check(
        refState.via === "full" && S(refState.body) === S(fullState.body),
        `${project}: checkpoint-commit layout full fold == plain full fold`,
      );
      const refRecords = await recordsJson(
        makeEnv(layout(project, records, { checkpointCommit: true }).repos).env,
        project,
      );
      const e = makeEnv(l.repos);
      const r = await stateJson(e.env, project);
      check(
        r.via === "delta",
        `${project}: checkpoint commit carrying an inherited in-closure record → delta path engaged`,
      );
      check(
        S(r.body) === S(refState.body),
        `${project}: inherited-duplicate delta /api/state byte-identical to full`,
      );
      const rr = await recordsJson(e.env, project);
      check(
        S(rr.body) === S(refRecords.body),
        `${project}: inherited-duplicate delta /api/records byte-identical to full`,
      );
    }
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
