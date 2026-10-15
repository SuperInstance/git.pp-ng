// Conformance runner: reads cases JSON, runs the TS fold, writes results.
// Usage: node runner.ts <cases.json> <results.json>
import { readFileSync, writeFileSync } from "node:fs";
import {
  derive,
  verifyCheckpoint,
  witnessFreshness,
  checkpointRoot,
  buildCheckpoint,
  buildFrontierCheckpoint,
  deriveDelta,
  type FoldRecord,
} from "../src/fold.ts";

interface Probe {
  at: unknown;
  state: unknown;
  closureRoot?: unknown;
}
interface Case {
  name: string;
  records: FoldRecord[];
  probes: Probe[];
}

const [, , casesPath, resultsPath] = process.argv;
const cases = JSON.parse(readFileSync(casesPath, "utf8")) as Case[];
const results = cases.map((c) => ({
  name: c.name,
  derive: derive(c.records),
  // Pass closureRoot through so v2 probes get the three-way result; probes
  // without it get v1 bool behaviour, unchanged.
  verify: (c.probes ?? []).map((p) =>
    verifyCheckpoint(c.records, {
      at: p.at,
      state: p.state,
      closureRoot: p.closureRoot,
    }),
  ),
  freshness: witnessFreshness(c.records),
  // checkpoint v2 digest for every applied id, compared Python-vs-TS by the
  // harness to prove the closure-root construction is identical across ports.
  roots: Object.fromEntries(
    derive(c.records).order.map((id) => [id, checkpointRoot(c.records, id)]),
  ),
  // Delta derivation at every legal checkpoint position: build the checkpoint
  // at each applied id, split the records into closure + tail, and delta-derive.
  // The harness checks each result equals the full derive AND matches the Python
  // referee's delta. null entries mark positions with no valid checkpoint.
  deltas: Object.fromEntries(
    derive(c.records).order.map((id) => {
      const cp = buildCheckpoint(c.records, id);
      if (cp === null) return [id, null];
      const closureSet = new Set(cp.closure.map((r) => String((r as { id?: unknown }).id)));
      const tail = c.records.filter(
        (r) => !closureSet.has(String((r as { id?: unknown }).id)),
      );
      return [id, deriveDelta(cp, tail)];
    }),
  ),
  // Frontier checkpoint (auto frontier = maximal applied records): the
  // harness checks the built checkpoint (members, root, covered count)
  // against the referee's, and that delta derivation over it equals the
  // full derive. null when no frontier checkpoint exists for the case.
  frontier: (() => {
    const fcp = buildFrontierCheckpoint(c.records);
    if (fcp === null) return null;
    const closureSet = new Set(
      fcp.closure.map((r) => String((r as { id?: unknown }).id)),
    );
    const tail = c.records.filter(
      (r) => !closureSet.has(String((r as { id?: unknown }).id)),
    );
    return {
      at: fcp.at,
      closureRoot: fcp.closureRoot,
      covered: fcp.closure.length,
      delta: deriveDelta(fcp, tail),
    };
  })(),
  // TAIL-DUP scenarios over the auto-frontier checkpoint (the frontier
  // self-clash): a tail record carrying a COVERED id whose content is
  // identical to the closure's copy is a duplicate serving — deriveDelta
  // drops it and engages (dup must equal the full derive); divergent
  // content under a covered id is a re-version — deriveDelta must answer
  // null (div). The harness checks both against the referee. null when no
  // frontier checkpoint exists for the case.
  taildup: (() => {
    const fcp = buildFrontierCheckpoint(c.records);
    if (fcp === null) return null;
    const closureSet = new Set(
      fcp.closure.map((r) => String((r as { id?: unknown }).id)),
    );
    const tail = c.records.filter(
      (r) => !closureSet.has(String((r as { id?: unknown }).id)),
    );
    const covered = fcp.closure[0];
    const dup = JSON.parse(JSON.stringify(covered)) as FoldRecord;
    const div = JSON.parse(JSON.stringify(covered)) as FoldRecord;
    div.effects = { __taildup__: "divergent" };
    return {
      dup: deriveDelta(fcp, [...tail, dup]),
      div: deriveDelta(fcp, [...tail, div]),
    };
  })(),
}));
writeFileSync(resultsPath, JSON.stringify(results));
console.log(`runner: ${results.length} cases`);
