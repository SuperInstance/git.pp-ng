# DESIGN.md for git.pp Submission

## 1. Fold's Contract

### Inputs
- **Record Set**: A collection of records, each containing:
  - `id`: Unique identifier for the record.
  - `key`: The key to which the record pertains.
  - `value`: The value associated with the key.
  - `dependencies`: A list of record IDs that this record depends on.

### Outputs
- **State**: The final state after processing all records, mapping keys to their resolved values.
- **Order**: The topological order in which records are processed.
- **Conflicts**: A list of keys for which conflicting values were detected.
- **Orphans**: A list of records that have no valid dependencies in the record set.

### VerifyCheckpoint Semantics
- The `verifyCheckpoint` function ensures that a given checkpoint is consistent with the record set. It verifies that:
  - The state in the checkpoint matches the state derived from the record set.
  - The order of record processing in the checkpoint is valid given the dependencies.
  - No conflicts or orphans are present that are not accounted for in the checkpoint.

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

### Witness-Absolute Reading
- Chooses the most direct witness as the absolute authority.
- Two independent implementations both converged on this approach after judging bouts.

### Ancestry-First Reading
- Would prioritize ancestors over witnesses.
- If flipped to ancestry-first, the change would be minimal, encapsulated in a one-line scope adjustment.

### Rationale for Choosing Absolute
- Witness-absolute was chosen due to its simplicity and the consensus from independent implementations.

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
```

This DESIGN.md provides a comprehensive overview of the git.pp submission, detailing the fold's contract, the resolution algorithm, the determinism argument, the rationale for design choices, performance metrics, known limitations, and a list of features that were deliberately not included.
