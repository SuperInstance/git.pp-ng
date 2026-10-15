// Conformance runner: reads cases JSON, runs the TS fold, writes results.
// Usage: node runner.ts <cases.json> <results.json>
import { readFileSync, writeFileSync } from "node:fs";
import {
  derive,
  verifyCheckpoint,
  witnessFreshness,
  checkpointRoot,
  retractionStatus,
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
  // SPIKE (retraction): the settlement pass as data, compared Python-vs-TS.
  retraction: retractionStatus(c.records),
  // checkpoint v2 digest for every applied id, compared Python-vs-TS by the
  // harness to prove the closure-root construction is identical across ports.
  roots: Object.fromEntries(
    derive(c.records).order.map((id) => [id, checkpointRoot(c.records, id)]),
  ),
}));
writeFileSync(resultsPath, JSON.stringify(results));
console.log(`runner: ${results.length} cases`);
