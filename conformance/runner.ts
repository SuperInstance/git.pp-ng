// Conformance runner: reads cases JSON, runs the TS fold, writes results.
// Usage: node runner.ts <cases.json> <results.json>
import { readFileSync, writeFileSync } from "node:fs";
import {
  derive,
  verifyCheckpoint,
  witnessFreshness,
  type FoldRecord,
} from "../src/fold.ts";

interface Probe {
  at: unknown;
  state: unknown;
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
  verify: (c.probes ?? []).map((p) =>
    verifyCheckpoint(c.records, { at: p.at, state: p.state }),
  ),
  freshness: witnessFreshness(c.records),
}));
writeFileSync(resultsPath, JSON.stringify(results));
console.log(`runner: ${results.length} cases`);
