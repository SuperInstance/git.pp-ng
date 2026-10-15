#!/usr/bin/env python3
"""Production-scale Python referee timer: the mirror of
prod_scale_driver.ts. Reads {name, records}[], times referee derive()
in-process (one warmup, then N timed reps), writes {name, timesMs, derive,
maxRssMb} per case.
Usage: python3 prod_scale_referee.py <cases.json> <results.json> <reps>
"""
import importlib.util
import json
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFEREE = HERE.parent / "referee" / "fold.py"


def load_referee():
    spec = importlib.util.spec_from_file_location("fold_referee", REFEREE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    cases_path, results_path, reps = sys.argv[1], sys.argv[2], int(sys.argv[3])
    ref = load_referee()
    cases = json.loads(Path(cases_path).read_text())
    out = []
    for c in cases:
        recs = [dict(r) for r in c["records"]]
        ref.derive([dict(r) for r in recs])  # warmup, untimed
        times = []
        result = None
        for _ in range(reps):
            t0 = time.perf_counter()
            result = ref.derive([dict(r) for r in recs])
            times.append((time.perf_counter() - t0) * 1000.0)
        out.append({
            "name": c["name"],
            "timesMs": times,
            "derive": result,
            "maxRssMb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        })
        print(f"{c['name']}: reps "
              + "/".join(f"{t:.1f}" for t in times) + " ms", flush=True)
    Path(results_path).write_text(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
