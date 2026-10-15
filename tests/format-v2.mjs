#!/usr/bin/env node
/**
 * format-v2.mjs — offline proof of format v2 at the Worker/collection
 * level (docs/FORMAT-V2.md). No network: the Artifacts binding is mocked
 * in memory (the worker-delta.mjs pattern) and the real Worker module
 * answers /api/state and /api/records.
 *
 * What is pinned here (the fold-level families live in conformance/):
 *
 *   1. BORN-CORRECT: a project whose records all carry correct born
 *      digests collects with NO warnings, the digests pass through
 *      /api/records unchanged, and /api/state is byte-identical to the
 *      same project written as v1.
 *   2. DEGRADE-AND-WARN, NEVER REJECT: a well-formed but wrong digest
 *      produces exactly one collectionWarning
 *      {id, kind: "digest-mismatch", claimed, computed} per lying copy,
 *      and the record still folds — state byte-identical to v1. A
 *      MALFORMED digest is not a claim at all: no warning, record folds.
 *   3. THE DUP TABLE (design §3.3): two copies of one id, K (first-seen,
 *      kept) and L (later, dropped). Divergence is judged on content with
 *      the digest claim set aside, so a digest-only difference never
 *      fires the divergent-duplicate warning, and identical digests never
 *      mask divergent content — including fmt-dup-copied-digest, where L
 *      carries K's digest copied onto different content.
 *   4. MIXED v1+v2: one project, both generations; v1 records show no
 *      digest key in /api/records, v2 records show theirs.
 *   5. DELTA CONSISTENCY: warnings and record views are identical on the
 *      delta and full paths, and a digest-only re-version of a covered
 *      record falls back to the full fold (the delta path never trusts
 *      the field either).
 *
 *   node tests/format-v2.mjs
 */
import worker from "../src/worker.ts";
import { recordLeaf, buildFrontierCheckpoint } from "../src/fold.ts";

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
const S = (x) => JSON.stringify(x);

// --- digest helpers (the referee dialect, via the TS leaf) -----------------
const born = (r) => ({ ...r, digest: "sha256:" + recordLeaf(r) });
const leafOf = (r) => "sha256:" + recordLeaf(r);
/** A well-formed digest that is wrong for `r`: r's own leaf, one hex digit
 * flipped — guaranteed well-formed, guaranteed unequal. */
const wrongFor = (r) => {
  const d = leafOf(r);
  return d.slice(0, 7) + (d[7] === "0" ? "1" : "0") + d.slice(8);
};

// --- in-memory Artifacts mock (worker-delta.mjs pattern) -------------------
function makeEnv(repos) {
  const binding = {
    async list({ cursor } = {}) {
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
      const r = repos[name];
      if (!r) throw new Error("no such repo: " + name);
      return {
        async log() {
          return { commits: (r.log ?? ["main"]).map((id) => ({ id })) };
        },
        async readFile({ ref, path }) {
          const text = r.files?.[ref]?.[path];
          if (text === undefined) return null;
          return { async text() { return text; } };
        },
      };
    },
  };
  return { ARTIFACTS: binding };
}

const U0 = "2026-01-01T00:00:00.000Z";
/** Project repo: one commit per record, oldest first in `commits`. */
function projectRepo(commits, extraFiles = {}) {
  const files = { main: { ...extraFiles } };
  const log = [];
  commits.forEach((rec, i) => {
    const c = `c${i}`;
    files[c] = { "record.json": JSON.stringify(rec, null, 2) + "\n" };
    log.unshift(c); // newest first
  });
  return { updatedAt: U0, log: log.length ? log : ["main"], files };
}
function taskRepo(rec, updatedAt = U0) {
  return {
    updatedAt,
    files: { main: { "record.json": JSON.stringify(rec, null, 2) + "\n" } },
  };
}

async function call(env, path, project) {
  const res = await worker.fetch(
    new Request(`http://x${path}?project=${project}`),
    env,
  );
  return { body: await res.json(), via: res.headers.get("x-gitpp-derivation") };
}
const stateOf = (env, p) => call(env, "/api/state", p);
const recordsOf = (env, p) => call(env, "/api/records", p);

// A small project: genesis in the project repo, two tasks in task repos.
const G = { id: "genesis", parents: [], kind: "task", effects: { project: "p", status: "open" } };
const A = { id: "task-a", parents: ["genesis"], kind: "task", effects: { "config.timeout": 30 } };
const B = { id: "task-b", parents: ["genesis"], kind: "task", effects: { "config.timeout": 60 } };

function simpleProject(name, g, a, b) {
  return {
    [name]: projectRepo([g]),
    [`${name}--task-a`]: taskRepo(a),
    [`${name}--task-b`]: taskRepo(b),
  };
}

async function run() {
  // --- 1. born-correct ------------------------------------------------------
  {
    const v2 = simpleProject("fv2ok", born(G), born(A), born(B));
    const v1 = simpleProject("fv2ok", G, A, B);
    const env2 = makeEnv(v2);
    const recs = await recordsOf(env2, "fv2ok");
    check(S(recs.body.collectionWarnings) === "[]",
      "born-correct: no collection warnings", S(recs.body.collectionWarnings));
    const views = Object.fromEntries(recs.body.records.map((r) => [r.id, r]));
    check(views["task-a"].digest === leafOf(A) && views["task-b"].digest === leafOf(B)
      && views["genesis"].digest === leafOf(G),
      "born-correct: digests pass through /api/records unchanged");
    const s2 = await stateOf(env2, "fv2ok");
    const s1 = await stateOf(makeEnv(v1), "fv2ok");
    check(S(s2.body) === S(s1.body),
      "born-correct: /api/state byte-identical to the v1 project");
  }

  // --- 2. degrade-and-warn, never reject ------------------------------------
  {
    const lying = [G, A, B].map((r) => ({ ...r, digest: wrongFor(r) }));
    const env = makeEnv(simpleProject("fv2lie", lying[0], lying[1], lying[2]));
    const recs = await recordsOf(env, "fv2lie");
    const want = [G, A, B].map((r) => ({
      id: r.id, kind: "digest-mismatch", claimed: wrongFor(r), computed: leafOf(r),
    }));
    // collection order is project commits then task repos by name; the
    // genesis copy is ingested first, then task-a, then task-b.
    check(S(recs.body.collectionWarnings) === S(want),
      "wrong digests: one digest-mismatch warning per lying copy, exact shape",
      S(recs.body.collectionWarnings));
    check(recs.body.count === 3,
      "wrong digests: every record still collected (never reject)");
    const s = await stateOf(env, "fv2lie");
    const s1 = await stateOf(makeEnv(simpleProject("fv2lie", G, A, B)), "fv2lie");
    check(S(s.body) === S(s1.body),
      "wrong digests: state byte-identical to v1 (treated exactly as v1)");
    // malformed digests are not claims: silence, and the records fold.
    const junk = [
      { ...G, digest: "garbage" },
      { ...A, digest: "sha256:xyz" },
      { ...B, digest: 42 },
    ];
    const envJ = makeEnv(simpleProject("fv2junk", junk[0], junk[1], junk[2]));
    const recsJ = await recordsOf(envJ, "fv2junk");
    check(S(recsJ.body.collectionWarnings) === "[]" && recsJ.body.count === 3,
      "malformed digests: no warnings, records fold (not claims at all)",
      S(recsJ.body.collectionWarnings));
    const sJ = await stateOf(envJ, "fv2junk");
    check(S(sJ.body) === S(s1.body), "malformed digests: state identical to v1");
  }

  // --- 3. the dup table (design §3.3) ---------------------------------------
  // K is served from a project-repo commit (first-seen, kept); L from the
  // task repo dup--x (later, dropped). keptFrom/droppedFrom name them.
  const K = { id: "x", parents: [], kind: "task", effects: { k: "kept" } };
  const Ldiv = { id: "x", parents: [], kind: "task", effects: { k: "other" } };
  const dupEnv = (kRec, lRec) => makeEnv({
    dup: projectRepo([kRec]),
    "dup--x": taskRepo(lRec),
  });
  const DIV = { id: "x", keptFrom: "dup", droppedFrom: "dup--x" };
  const mm = (rec, claimed) => ({
    id: "x", kind: "digest-mismatch", claimed, computed: leafOf(rec),
  });

  { // row 1: identical content, both digests correct -> silence
    const w = (await recordsOf(dupEnv(born(K), born(K)), "dup")).body.collectionWarnings;
    check(S(w) === "[]", "fmt-dup row 1: identical + both correct -> no warnings", S(w));
  }
  { // row 2: identical content, L's digest wrong -> digest-mismatch on L only
    const lBad = { ...K, digest: wrongFor(K) };
    const w = (await recordsOf(dupEnv(born(K), lBad), "dup")).body.collectionWarnings;
    check(S(w) === S([mm(K, wrongFor(K))]),
      "fmt-dup row 2: identical content, L wrong -> digest-mismatch on L, NO divergent warning", S(w));
  }
  { // row 2b: K is v1, L carries the wrong digest -> same single warning
    const lBad = { ...K, digest: wrongFor(K) };
    const w = (await recordsOf(dupEnv(K, lBad), "dup")).body.collectionWarnings;
    check(S(w) === S([mm(K, wrongFor(K))]),
      "fmt-dup row 2b: v1 kept copy + wrong digest on L -> digest-mismatch only", S(w));
  }
  { // row 3: divergent content, both digests correct for their own content
    const w = (await recordsOf(dupEnv(born(K), born(Ldiv)), "dup")).body.collectionWarnings;
    check(S(w) === S([DIV]),
      "fmt-dup row 3: divergent + both correct -> divergent-duplicate only", S(w));
  }
  { // row 4: divergent, K's digest wrong, L's correct -> both warnings
    const kBad = { ...K, digest: wrongFor(K) };
    const w = (await recordsOf(dupEnv(kBad, born(Ldiv)), "dup")).body.collectionWarnings;
    check(S(w) === S([mm(K, wrongFor(K)), DIV]),
      "fmt-dup row 4: divergent + kept copy lies -> digest-mismatch (kept) + divergent", S(w));
  }
  { // row 5: divergent, both digests wrong -> both copies warned + divergent
    const kBad = { ...K, digest: wrongFor(K) };
    const lBad = { ...Ldiv, digest: wrongFor(Ldiv) };
    const w = (await recordsOf(dupEnv(kBad, lBad), "dup")).body.collectionWarnings;
    check(S(w) === S([mm(K, wrongFor(K)), mm(Ldiv, wrongFor(Ldiv)), DIV]),
      "fmt-dup row 5: divergent + both lie -> digest-mismatch x2 + divergent", S(w));
  }
  { // fmt-dup-copied-digest: L carries K's digest on different content.
    // The copied digest must never make the pair read as identical: the
    // divergent warning fires, the copied claim is itself a mismatch for
    // L's content, and the fold sees K's content — the forgery gains nothing.
    const forged = { ...Ldiv, digest: leafOf(K) };
    const env = dupEnv(born(K), forged);
    const recs = await recordsOf(env, "dup");
    check(S(recs.body.collectionWarnings) === S([mm(Ldiv, leafOf(K)), DIV]),
      "fmt-dup-copied-digest: copied digest on different content -> digest-mismatch + divergent",
      S(recs.body.collectionWarnings));
    const s = await stateOf(env, "dup");
    check(s.body.state.k === "kept",
      "fmt-dup-copied-digest: kept copy's content wins; forgery gained nothing",
      S(s.body.state));
  }

  // --- 4. born-stale: digest correct for content the record no longer has --
  {
    const stale = { ...A, effects: { "config.timeout": 99 }, digest: leafOf(A) };
    const env = makeEnv(simpleProject("fv2stale", G, stale, B));
    const recs = await recordsOf(env, "fv2stale");
    const want = [{ id: "task-a", kind: "digest-mismatch", claimed: leafOf(A), computed: leafOf(stale) }];
    check(S(recs.body.collectionWarnings) === S(want),
      "born-stale: one digest-mismatch warning (claimed = the old leaf)", S(recs.body.collectionWarnings));
    const s = await stateOf(env, "fv2stale");
    check(s.body.state["config.timeout"] === 99,
      "born-stale: record treated as v1 — its CURRENT content (99) folds, not the content its digest claims (30)",
      S(s.body.state));
  }

  // --- 5. mixed v1 + v2 in one project ---------------------------------------
  {
    const env = makeEnv(simpleProject("fv2mix", G, born(A), B));
    const recs = await recordsOf(env, "fv2mix");
    check(S(recs.body.collectionWarnings) === "[]",
      "mixed v1+v2: no warnings", S(recs.body.collectionWarnings));
    const views = Object.fromEntries(recs.body.records.map((r) => [r.id, r]));
    check(views["task-a"].digest === leafOf(A)
      && !("digest" in views["genesis"]) && !("digest" in views["task-b"]),
      "mixed v1+v2: v2 views carry digest, v1 views have no digest key");
    const s = await stateOf(env, "fv2mix");
    const s1 = await stateOf(makeEnv(simpleProject("fv2mix", G, A, B)), "fv2mix");
    check(S(s.body) === S(s1.body), "mixed v1+v2: state identical to all-v1");
  }

  // --- 6. delta-path consistency ---------------------------------------------
  {
    const recsAll = [born(G), born(A), born(B)];
    const mk = () => {
      const repos = simpleProject("fv2d", recsAll[0], recsAll[1], recsAll[2]);
      return repos;
    };
    const installFrontier = (repos) => {
      const cp = buildFrontierCheckpoint(recsAll);
      const closureIds = new Set(cp.closure.map((r) => r.id));
      const coverTaskRepos = {};
      for (const id of closureIds) {
        const name = `fv2d--${id}`;
        if (repos[name]) coverTaskRepos[name] = { updatedAt: repos[name].updatedAt, id };
      }
      const stored = {
        v: Array.isArray(cp.at) ? 3 : 2, project: "fv2d", ...cp,
        coverProjectCommits: { c0: "genesis" }, coverTaskRepos,
      };
      repos["fv2d"].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
      return stored;
    };
    const full = await recordsOf(makeEnv(mk()), "fv2d");
    const dRepos = mk();
    installFrontier(dRepos);
    const delta = await recordsOf(makeEnv(dRepos), "fv2d");
    check(delta.via === "delta", "delta path engages over born-digest records");
    check(S(delta.body) === S(full.body),
      "delta /api/records byte-identical to full (digests + warnings ride both paths)");

    // a covered record whose digest lies, in repo AND checkpoint alike:
    // both paths must warn identically (degrade-and-warn is path-independent)
    const lyingB = { ...B, digest: wrongFor(B) };
    const lyingAll = [born(G), born(A), lyingB];
    const mkLying = () => simpleProject("fv2d", lyingAll[0], lyingAll[1], lyingAll[2]);
    const fullL = await recordsOf(makeEnv(mkLying()), "fv2d");
    const dReposL = mkLying();
    {
      const cp = buildFrontierCheckpoint(lyingAll);
      const coverTaskRepos = {};
      for (const r of cp.closure) {
        const name = `fv2d--${r.id}`;
        if (dReposL[name]) coverTaskRepos[name] = { updatedAt: dReposL[name].updatedAt, id: r.id };
      }
      const stored = { v: 3, project: "fv2d", ...cp, coverProjectCommits: { c0: "genesis" }, coverTaskRepos };
      dReposL["fv2d"].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
    }
    const deltaL = await recordsOf(makeEnv(dReposL), "fv2d");
    const wantB = { id: "task-b", kind: "digest-mismatch", claimed: wrongFor(B), computed: leafOf(B) };
    check(deltaL.via === "delta" && S(deltaL.body) === S(fullL.body)
      && S(fullL.body.collectionWarnings) === S([wantB]),
      "lying covered record: delta and full warn identically (digest-mismatch)",
      S(fullL.body.collectionWarnings));

    // a covered record re-pushed with ONLY a digest added (content
    // identical): the stored closure no longer byte-matches reality, so
    // the delta path must decline and the full fold must decide.
    const v1All = [G, A, B];
    const reposR = simpleProject("fv2d", v1All[0], v1All[1], v1All[2]);
    {
      const cp = buildFrontierCheckpoint(v1All);
      const coverTaskRepos = {};
      for (const r of cp.closure) {
        const name = `fv2d--${r.id}`;
        if (reposR[name]) coverTaskRepos[name] = { updatedAt: reposR[name].updatedAt, id: r.id };
      }
      const stored = { v: 3, project: "fv2d", ...cp, coverProjectCommits: { c0: "genesis" }, coverTaskRepos };
      reposR["fv2d"].files.main["checkpoint.json"] = JSON.stringify(stored, null, 2) + "\n";
    }
    reposR["fv2d--task-a"] = taskRepo(born(A), "2027-06-01T00:00:00.000Z"); // digest added, updatedAt moved
    const rr = await recordsOf(makeEnv(reposR), "fv2d");
    check(rr.via === "full",
      "digest-only re-version of a covered record -> full fallback (delta never trusts the field)");
    check(S(rr.body.collectionWarnings) === "[]",
      "digest-only re-version: the fresh copy's correct digest verifies, no warnings");
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
