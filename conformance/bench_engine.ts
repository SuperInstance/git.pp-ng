// Engine bench: times ONE case from a prod_scale-format cases file
// ({name, records}[]) under one engine, in its own process so peak RSS
// is attributable. Peak RSS is the process's own VmHWM high-water mark
// from /proc/self/status (the same quantity /usr/bin/time -v reports as
// maximum resident set size; /usr/bin/time is absent on this VM).
// Usage: node bench_engine.ts <cases.json> <new|legacy> <reps>
import { readFileSync } from "node:fs";
import {
  derive,
  deriveLegacy,
  type FoldRecord,
} from "../src/fold.ts";

const [, , casesPath, engine, repsArg] = process.argv;
const reps = Number(repsArg ?? "3");
const cases = JSON.parse(readFileSync(casesPath, "utf8")) as {
  name: string;
  records: FoldRecord[];
}[];
const fold = engine === "legacy" ? deriveLegacy : derive;
const c = cases[0];
fold(c.records); // warmup, untimed
const times: number[] = [];
for (let i = 0; i < reps; i++) {
  const t0 = performance.now();
  fold(c.records);
  times.push(performance.now() - t0);
}
times.sort((a, b) => a - b);
const hwm = readFileSync("/proc/self/status", "utf8").match(/VmHWM:\s*(\d+)\s*kB/);
console.log(
  `${c.name} engine=${engine} n=${c.records.length} median_ms=${times[Math.floor(times.length / 2)].toFixed(1)} reps=[${times.map((t) => t.toFixed(1)).join(", ")}] peak_rss_mb=${hwm ? (Number(hwm[1]) / 1024).toFixed(1) : "?"} end_rss_mb=${(process.memoryUsage().rss / 1048576).toFixed(1)}`,
);
