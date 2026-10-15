# git.pp — A Git Platform for Concurrent Work

git.pp is a Git platform where multiple agents work on one project concurrently: the atom is the **task**, each task works in its own repository forked from a shared baseline, and project state is never stored — it is derived by folding the append-only record of task completions.

**Live Example:** [Explore a live project](https://gitpp.superinstance.dev/?project=poc-quilt-1)
*Raw state:* [API endpoint](https://gitpp.superinstance.dev/api/state?project=poc-quilt-1) · *Records:* [API endpoint](https://gitpp.superinstance.dev/api/records?project=poc-quilt-1)

**Replay the Fold:** [The Desk](https://gitpp-desk.superinstance.dev) lets you step through the fold, rebuild the state, and test different arrival orders.

**▶ Watch the demo (6:12)** — the full walkthrough on the live system: the race, the override inheritance, the arrival-order invariance proof:

https://github.com/SuperInstance/git.pp-ng/blob/main/demo/gitpp-demo.mp4

(The file also lives in this repo at `demo/gitpp-demo.mp4`.)

## Prove it in two commands

Everything claimed below is checkable from a cold clone — no Cloudflare account, no admin key:

```sh
cd conformance && python3 harness.py
python3 runner/pp.py state poc-quilt-1
```

The harness ends with **863/863 cases identical** — the TypeScript fold in `src/fold.ts` against the Python referee in `referee/fold.py`, plus 1,029 checkpoint probes, 3,231 witness-freshness checks, 16,290 closure-root digests, 2,812 oracle checks, 3 transport pins, and 8 live-golden checks, all passing. (`npm test` runs the same harness.) The runner reads the live demo project and prints `config.timeout = 45` — conflict winner **override-1**, losers task-a, task-b, task-d preserved, orphans none.

## Mechanism

- The atom is the **task**. For each task, a dedicated **Artifacts repository** is forked from a shared baseline, with a repo-scoped token, and agents push their competing completions into an append-only record. Project state is not stored anywhere — it is derived by folding the record.
- **Resolution is mechanical.** Class precedence is absolute: `override` > `task` > `witness`. Within a class, a candidate that is an ancestor of another candidate is dominated and drops out; the survivors are the most-derived. Any remaining tie breaks toward the candidate with the most intent records (`task` + `override`) in its ancestry, then the fewest `witness` records, then the lexicographically smallest `id`. Losing versions are preserved in their own repositories and stay cloneable.

## Fact Sheet

- **Conformance:** 863/863 cases identical; 1,029 checkpoint probes; 3,231 witness-freshness checks; 16,290 closure-root digests; 2,812 oracle checks; 3 transport pins; 8 live-golden checks.
- **Scale project:** 62 records; 3 conflicts; 21 losers preserved. The fold runs in 12 ms over the 62-record project.
- **Race scenario:** Five pairs of agents pushed simultaneously; one 10-way conflict; all 9 losers preserved.
- **Live reads:** derived state reads take 2.9–5.7 s.
- An override's 45 stands against a later push of 99 — checkable at the records link above.

## The record model

A **record** in git.pp is a structured entry with the following fields:
- **id:** Unique identifier for the record.
- **parents:** References to the state against which the record was completed.
- **kind:** Type of the record, which can be `task`, `witness`, or `override`.
- **effects:** The changes or effects introduced by the record.
- **signer/reason:** Information about who created the record and why.

## How derivation works

The state of the repository is derived through a pure function called `fold(records)`. The derivation follows these rules:
- **Class priority:** `override` > `task` > `witness`, with `override` having the highest priority.
- **Ancestry reduction:** The most-derived candidates are selected based on their ancestry.
- **Tie-break:** In case of a tie, the following criteria are applied in order:
  1. The record with the most intent records (`task` + `override`) in its ancestry.
  2. The record with the fewest `witness` records in its ancestry.
  3. The record with the lexicographically smallest `id`.
- **Orphans:** Records that are missing ancestors or are affected by cycles are excluded and reported.

## Determinism, measured

Measured, not asserted: the same completions folded in shuffled arrival orders produce identical state, conflicts, and application order. The TypeScript implementation in `src/fold.ts` was checked against the Python reference (`referee/fold.py`) on 863 generated cases and 1,029 checkpoint probes (plus 3,231 witness-freshness checks, 16,290 closure-root digests, and 2,812 brute-force maximality-oracle checks) — zero divergences (harness in `conformance/`).

## Seeing versus doing

A `witness` record carries an observation and no intent. Its uncontested observations reach state — the next agent inherits what was seen — but a witness never wins a contested key against a task or an override. The record distinguishes *done by us* from *seen by us*, and only one of those gets a vote.

## Inheritance

A reasoned `override` joins the record and binds every later derivation — including branches forked before the override existed, and branches that never descend from it — because derivation reads the whole record, and an override outranks a task wherever it sits. Watch it happen: in the live project above, `task-d` pushed `config.timeout: 99` *after* the override — and the derived state is still `45`, with `task-d` named in the conflict's losers. `task-d`'s ancestry never passes through `override-1`; the override binds it anyway. Replay the whole scene yourself with `runner/scene.sh`, or read the state endpoint and check the losers list.

## Checkpoints

A checkpoint pins a derived state together with the closure root of the records that produced it. Verification re-derives both and returns one of three verdicts — VERIFIED, STATE-ONLY, or MISMATCH — so a tamper that touches only a record's reason, invisible to the derived state, reads STATE-ONLY, never a silent pass.

## Run it yourself

To run git.pp, you need:
- A Cloudflare account (Artifacts requires Workers Paid).
- `wrangler` and `Node` for the Worker.
- `bash`, `git`, `curl`, and `python3` for the runner.

The `runner/scene.sh` script replays the full scene against a live deployment in under a minute and prints per-assertion `PASS`/`FAIL`. The `referee/fold.py` script is the Python reference implementation.

## Architecture

- **Record and protocol:** Portable Git.
- **Coordinator:** Cloudflare-shaped Worker that forks, coordinates, and serves derivations. The Worker coordinates forks, tokens, and events but never decides the state.
- **Fold:** The fold function decides the state.

Push events (`cf.artifacts.repo.pushed`) feed a Queue for observability. Derivation is per-request, ensuring that the projection is never stale.

## License

git.pp is licensed under the MIT License.
