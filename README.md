# git.pp — Concurrent Git Substrate

**One-liner:** git.pp is a Git substrate where multiple agents work on a single repository concurrently, with state derived from a task record rather than stored.

## What it is

In git.pp, the atom is the **task**. For each task, a dedicated **Artifacts repository** is forked from a shared baseline, with a repo-scoped token, and agents push their competing completions into an append-only record. Project state is not stored anywhere — it is derived by folding the record.

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

Measured, not asserted: the same completions folded in 50 shuffled arrival orders produce identical state, conflicts, and application order. The TypeScript implementation in `src/fold.ts` was checked against the Python reference (`referee/fold.py`) on 839 generated cases and 960 checkpoint probes — zero divergences (harness in `conformance/`).

## Seeing versus doing

A `witness` record carries an observation and no intent. Its uncontested observations reach state — the next agent inherits what was seen — but a witness never wins a contested key against a task or an override. The record distinguishes *done by us* from *seen by us*, and only one of those gets a vote.

## Inheritance

A reasoned `override` joins the record and binds its descendants, including branches forked before the override existed. This is demonstrated by the fact that a later `99` from a non-descendant branch loses to the override's `45`.

## Checkpoints

Checkpoints allow you to name a record position. Verification re-derives the state at that position, and any tampered checkpoints are rejected.

## Scoped defaults

The system uses scoped defaults with the following hierarchy: global → org → user → repo → folder → file. The narrowest scope takes precedence.

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
