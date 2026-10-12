# DESIGN.md for git.pp Submission

## 1. Fold's Contract

### Inputs
- **Record Set**: A collection of records, each containing:
  - `id`: Unique identifier for the record.
  - `parents`: A list of record IDs this record completed against — its ancestry.
  - `kind`: One of `task` (a completion), `witness` (an observation), or `override` (a reasoned human decision).
  - `effects`: A map of state keys to the values this record sets.
  - `reason`: Present on overrides — the stated reason, carried in the record.

### Outputs
- **State**: The final state after processing all records, mapping keys to their resolved values.
- **Order**: The topological order in which records are processed.
- **Conflicts**: A list of keys for which conflicting values were detected.
- **Orphans**: A list of records that have no valid dependencies in the record set.

### VerifyCheckpoint Semantics
- `verify_checkpoint(records, checkpoint)` performs exactly one check, and it is the strong one: it re-derives state from exactly the ancestors of the record named in `checkpoint["at"]` and returns true only if that derived state equals the checkpoint's claimed state. A checkpoint that summarises anything else — a superset, a subset, a tampered value — fails. Checkpoints are therefore legal only when provenance-stamped: they name the record position they summarise and are reproducible from it.

## 2. Resolution Algorithm

### Step-by-Step Process

1. **Canonical Indexing by ID**:
   - Records are indexed by their unique `id` to ensure constant-time access.

2. **Orphan Worklist and Cycle-Affected Exclusion**:
   - Identify and collect records with no valid dependencies into an orphan worklist.
   - Exclude records that are part of dependency cycles from the processing order.

3. **Kahn Order (Smallest-ID-First)**:
   - Apply Kahn's algorithm to determine a topological order of record processing.
   - In the case of ties (records with the same in-degree), process the record with the smallest `id` first.

4. **Per Key Resolution**:
   - **Class Floor Override > Task > Witness Absolute**:
     - For each key, resolve values based on a precedence hierarchy: class floor overrides take precedence over tasks, which in turn take precedence over witness absolutes.
   - **Ancestry Reduction**:
     - Reduce the set of candidate values by considering only the most direct ancestors.
   - **Grafted Tie-Break**:
     - If multiple candidates remain, apply tie-breaks in the following order:
       1. **Most Intent Ancestors**: Prefer candidates with the most intent ancestors.
       2. **Fewest Witness Ancestors**: If still tied, prefer candidates with the fewest witness ancestors.
       3. **Smallest ID**: If still tied, prefer the candidate with the smallest `id`.

5. **Conflict Detection**:
   - If, after resolution, there are two or more distinct values for a key, a conflict is emitted.
   - The losers are all candidates except the winner, which is the value chosen by the resolution algorithm.

## 3. Determinism Argument

Every step of the algorithm is a function of the record set only:
- The order of processing is determined by the dependencies and the `id` of records.
- The resolution of values is based on a fixed precedence hierarchy and deterministic tie-breaks.
- Conflict detection is based on the presence of multiple distinct values for a key.
- Therefore, given the same record set, the algorithm will always produce the same outputs.

## 4. Witness-Absolute vs. Ancestry-First Reading

### Witness-Absolute Reading (implemented)
- Class precedence is absolute: an `override` outranks a `task`, a `task` outranks a `witness` — even when the witness is a *descendant* of the task. An observation can never displace a completion on a contested key, no matter how recent. "Seen by us" does not overwrite "done by us"; changing a completed value takes a task, or an override that cites the witness.
- Uncontested witness observations DO reach the state — that is their purpose: ambient truth (a disk at 93%, a load average) is part of the derived picture, labelled by kind.

### Ancestry-First Reading (the alternative)
- Would let ancestry dominate class: a descendant witness could displace its own ancestor task on the same key.
- The flip is small and localised (the class check inside the per-key reduction), and the conformance harness would arbitrate either reading identically in both implementations.

### Rationale for Choosing Absolute
- The record distinguishes "done" from "seen"; letting the seen overwrite the done silently would erase exactly the distinction the record exists to keep. Absolute precedence keeps every displacement attributable: if a value changed, a task or a reasoned override changed it.

## 5. Measured Performance

### Python Reference Implementation
- **10k Chain**: 0.6 seconds
- **2k Writers One Key**: 0.04 seconds

### TypeScript Port Conformance
- **Test Suite Results**: 839/839, 960/960 tests passed
- Ensures that the TypeScript implementation conforms to the specification.

## 6. Known Limits

- **Detection Boundary**: The fold labels every conflict it can detect from the record set.
- **Semantic Incompatibilities**: Incompatibilities between individually valid completions are not detectable from the record alone and are out of scope.
- **JS Int/Float Collapse Post-JSON**: The algorithm does not handle the collapse of integers and floats in JavaScript post-JSON parsing.
- **Derivation Performance**: Derivation is O(record) per request at demo scale. Incremental derivation from verified checkpoints is future work.

## 7. What Was Deliberately Not Built

- **No Lock**: The algorithm does not use any locks for synchronization.
- **No Queue**: Queues are not used for processing records, except for event observability.
- **No Resolution Service in State Path**: The state path does not include a resolution service. Queues are only used for observability of events.
