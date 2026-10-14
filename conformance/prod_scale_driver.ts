// Production-scale TS timing driver: reads a cases file written by
// prod_scale.py ({name, records}[]), times derive() per case in-process
// (one warmup, then N timed reps), and writes {name, timesMs, derive,
// maxRssMb} per case. Timing excludes process start and JSON ingest of the
// corpus file itself is included only in the sense that records are already
// parsed objects when derive() is called — the same convention the Python
// referee timer uses.
// Usage: node prod_scale_driver.ts <cases.json> <results.json> <reps>
import { readFileSync, writeFileSync } from "node:fs";
import { derive, type FoldRecord } from "../src/fold.ts";

const [, , casesPath, resultsPath, repsArg] = process.argv;
const reps = Number(repsArg ?? "3");
const cases = JSON.parse(readFileSync(casesPath, "utf8")) as {
  name: string;
  records: FoldRecord[];
}[];

const out = [];
for (const c of cases) {
  derive(c.records); // warmup, untimed
  const times: number[] = [];
  let result: unknown = null;
  for (let i = 0; i < reps; i++) {
    const t0 = performance.now();
    result = derive(c.records);
    times.push(performance.now() - t0);
  }
  out.push({
    name: c.name,
    timesMs: times,
    derive: result,
    maxRssMb: process.memoryUsage().rss / 1048576,
  });
  console.log(
    `${c.name}: reps ${times.map((t) => t.toFixed(1)).join("/")} ms`,
  );
}
writeFileSync(resultsPath, JSON.stringify(out));
