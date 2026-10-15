// Differential test: the memoised-bitset engine (derive / witnessFreshness)
// against the frozen legacy engine (deriveLegacy / witnessFreshnessLegacy).
//
// Three nets, in order:
//  1. The full conformance corpus (cases.json as written by harness.py):
//     new == legacy on derive() and witnessFreshness() for every case,
//     compared by canonical JSON (pyCanonical), so key order cannot hide
//     a divergence and array order cannot fake one.
//  2. Seeded random small graphs (cycles, ghost parents, duplicate ids,
//     unknown kinds, float edge values): new == legacy on the base graph.
//  3. Parents-order / input-order permutations of every fuzz graph: the
//     fold is a function of the record SET — every permutation must give
//     byte-identical output from the new engine (the §0.2 failure class),
//     and agree with the legacy engine's output on the same permutation.
//
// Usage: node differential.ts [cases.json]
// Exit code 0 iff every comparison agrees.
import { readFileSync } from "node:fs";
import {
  derive,
  deriveLegacy,
  witnessFreshness,
  witnessFreshnessLegacy,
  pyCanonical,
  type FoldRecord,
} from "../src/fold.ts";

let failures = 0;
function check(label: string, a: unknown, b: unknown): void {
  const ca = pyCanonical(a as never);
  const cb = pyCanonical(b as never);
  if (ca !== cb) {
    failures += 1;
    console.log(`MISMATCH ${label}\n  new:    ${ca.slice(0, 400)}\n  legacy: ${cb.slice(0, 400)}`);
  }
}

// --- Net 1: the conformance corpus ---------------------------------------
const casesPath = process.argv[2] ?? new URL("./cases.json", import.meta.url).pathname;
const cases = JSON.parse(readFileSync(casesPath, "utf8")) as {
  name: string;
  records: FoldRecord[];
}[];
for (const c of cases) {
  check(`corpus ${c.name} derive`, derive(c.records), deriveLegacy(c.records));
  check(
    `corpus ${c.name} freshness`,
    witnessFreshness(c.records),
    witnessFreshnessLegacy(c.records),
  );
}
console.log(`net 1: ${cases.length} corpus cases compared (derive + freshness)`);

// --- Nets 2+3: seeded fuzz with permutations ------------------------------
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const KINDS = ["task", "task", "task", "witness", "witness", "override", "bogus"];
const VALUES: unknown[] = [0, 1, 2, 99, 1.5, 1e-5, "x", "y", true, false, null];
const KEYS = ["k", "config.hot", "cfg.k1"];

function genGraph(rng: () => number): FoldRecord[] {
  const n = 2 + Math.floor(rng() * 8); // 2..9 records
  const ids = Array.from({ length: n }, (_, i) => `n${i}`);
  const recs: FoldRecord[] = [];
  for (let i = 0; i < n; i++) {
    const parents: string[] = [];
    for (let j = 0; j < n; j++) {
      if (j !== i && rng() < 0.28) parents.push(ids[j]); // any direction: cycles happen
    }
    if (rng() < 0.12) parents.push("ghost"); // missing parent -> orphan subtree
    const effects: { [k: string]: unknown } = {};
    for (const k of KEYS) {
      if (rng() < 0.4) effects[k] = VALUES[Math.floor(rng() * VALUES.length)];
    }
    const rec: FoldRecord = {
      id: ids[i],
      parents,
      kind: KINDS[Math.floor(rng() * KINDS.length)],
      effects,
    };
    if (rec.kind === "override") rec.reason = `ruling ${i}`;
    recs.push(rec);
  }
  if (rng() < 0.15 && recs.length > 1) {
    // duplicate id, different content: canonical-form resolution must agree
    const dup = { ...recs[0], effects: { k: 424242 } } as FoldRecord;
    recs.push(dup);
  }
  return recs;
}

function shuffled<T>(arr: T[], rng: () => number): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const GRAPHS = 400;
const VARIANTS = 24;
let fuzzComparisons = 0;
for (let g = 0; g < GRAPHS; g++) {
  const rng = mulberry32(0xbeef + g);
  const base = genGraph(rng);
  const baseOut = derive(base);
  check(`fuzz ${g} derive`, baseOut, deriveLegacy(base));
  check(`fuzz ${g} freshness`, witnessFreshness(base), witnessFreshnessLegacy(base));
  fuzzComparisons += 2;
  for (let v = 0; v < VARIANTS; v++) {
    const variant = shuffled(
      base.map((r) => ({ ...r, parents: shuffled(r.parents ?? [], rng) })),
      rng,
    );
    check(`fuzz ${g}v${v} derive vs base`, derive(variant), baseOut);
    check(`fuzz ${g}v${v} derive vs legacy`, derive(variant), deriveLegacy(variant));
    fuzzComparisons += 2;
  }
}
console.log(
  `nets 2+3: ${GRAPHS} fuzz graphs x (1 + ${VARIANTS} permutations), ${fuzzComparisons} comparisons`,
);

console.log(
  failures === 0
    ? "DIFFERENTIAL: all comparisons agree — new engine is byte-identical to legacy"
    : `DIFFERENTIAL: ${failures} MISMATCHES`,
);
process.exit(failures === 0 ? 0 : 1);
