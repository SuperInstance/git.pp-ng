# Where git.pp sits on the frontier

An honest map of the prior art git.pp touches, what it borrows, what it
deliberately does differently, and the three evolutions with the most leverage.
The audience is an engineer who will check the citations, so each external claim
names a source you can find.

## The one-sentence placement

git.pp is **event sourcing** (state is a fold over an append-only record, never
stored) whose **conflict resolution** borrows the *convergence* and
*loser-preservation* instincts of **CRDTs** but reaches them with a single
deterministic read-time fold rather than per-replica commutative merges — and
whose arbiter is **provenance** (class + ancestry), not a clock.

## What it borrows

**Event sourcing — state as a projection.** The record is the source of truth;
the derived state is a pure, recomputable projection, "never a second source of
truth" (the Worker re-derives on every request). This is the event-sourcing
contract: persist the events, fold them to a view, rebuild the view at will.
See Martin Fowler, "Event Sourcing" (martinfowler.com/eaaDev/EventSourcing.html,
2005), and Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017),
ch. 11 on event logs and derived state. git.pp's `derive()` is the left-fold;
`/api/state` is the projection; there is "no cache to invalidate" (DEPLOY.md).

**CRDTs — convergence under reordering, and keeping concurrent values.** git.pp's
headline property — the same records folded in any arrival order produce
identical state (measured over 50 shuffles; the conformance harness pins
parents-order invariance too) — is the *strong eventual consistency* goal CRDTs
formalise: replicas that have seen the same updates are in the same state,
regardless of order. And git.pp's conflict report, which preserves every losing
value instead of discarding it, mirrors a **multi-value register** (MV-Register),
which retains concurrent writes rather than collapsing them. See Shapiro,
Preguiça, Baquero, Zawirski, "Conflict-free Replicated Data Types" (SSS 2011) and
the companion INRIA technical report RR-7687, "A comprehensive study of
Convergent and Commutative Replicated Data Types" (2011).

**Git / Merkle-DAGs — ancestry as the substrate.** Records carry `parents`; the
record set is a DAG; repos store it. The ancestry graph *is* the ordering and
dominance substrate, exactly as a commit DAG is in Git. See the Git object model,
*Pro Git* 2nd ed., "Git Internals — Git Objects" and "Git References"
(git-scm.com/book/en/v2/Git-Internals-Git-Objects). The Merkle-DAG idea —
nodes referencing parents by identity to form a verifiable history — traces to
Merkle, "A Digital Signature Based on a Conventional Encryption Function"
(CRYPTO '87), and is the backbone of content-addressed stores like IPFS (Benet,
"IPFS — Content Addressed, Versioned, P2P File System," 2014).

**Provenance systems — who and why, as data.** Records carry a `kind` and a
`reason`; the tie-break is *provenance-cleanliness* (most intent ancestors,
fewest witnesses). Provenance as first-class, queryable metadata that explains
and arbitrates a value is the thrust of the W3C PROV data model (W3C
Recommendation, 2013, "PROV-DM: The PROV Data Model").

## What it deliberately does differently

**Not a CRDT.** A CRDT guarantees convergence by making each replica's *merge
function* commutative/associative/idempotent, so replicas reconcile pairwise with
no coordinator. git.pp instead collects the whole record and runs **one
deterministic fold per request** — a central pure function, order-independent by
*canonical sorting*, not by algebraic commutativity. The cost is real: no
incremental per-replica merge, and O(record) re-derivation (DESIGN.md §6). The
gain is a **typed priority** (`override > task > witness`, absolute) and a
*reasoned* tie-break that a CRDT register does not express — a CRDT merges values,
it does not know that one write is a captain's decision and another is a mere
observation. git.pp buys expressiveness with centralisation; a CRDT buys
decentralisation with value-blindness.

**Not last-writer-wins.** The most common register CRDT, LWW-Register, breaks ties
by timestamp (wall-clock or Lamport) — newest silently wins, losers vanish.
git.pp has **no clock in the decision**: winners are chosen by class, then
ancestry cleanliness, then id, and every loser is preserved and inspectable.
This is a deliberate rejection of "newest wins" because, with concurrent agents,
newest-wins silently discards completed work.

**Not content-addressed by value.** Git names objects by content hash, which buys
dedup and tamper-evidence for free. git.pp names records by an *author-assigned
id*; `parents` reference ids, not hashes. That is why a duplicate-id tie-break
has to exist at all (see ENGINEERING.md) — ids are not guaranteed unique or
integrity-bearing the way a hash is. Content-addressing records is a plausible
evolution (it would retire the dup-id tie-break), noted but not taken.

**Not operational transformation.** OT (Ellis & Gibbs, "Concurrency Control in
Groupware Systems," SIGMOD 1989) transforms concurrent operations against each
other at write time. git.pp does no transform; it resolves entirely at read time
by folding. Closer in spirit is the patch-commutation view of Darcs/Pijul (a
"theory of patches"), but git.pp commutes *derivation*, not patches.

## The three highest-value evolutions

Each is desirable; each must respect a hard constraint the current design earns.

1. **Incremental derivation from verified checkpoints.** Re-derive only the delta
   since a checkpoint instead of folding the whole record every request — the
   obvious answer to the O(record) latency in DESIGN.md §6.
   *Constraint it must respect:* the result must stay **byte-identical to a full
   re-derivation**, and `verify_checkpoint` is only as pinned as the record set
   it is checked against — a late-arriving ancestor grows the closure of `at`
   (ENGINEERING.md, `verify_checkpoint`). So a checkpoint used as an incremental
   base must carry a **closure-root identity** that makes a grown closure
   detectable, or incremental derivation will silently diverge from the full fold.

2. **Read-set preconditions** (declare the keys a task read, not just those it
   wrote) — widens the detection boundary from same-key write–write to
   read–write hazards (OPEN-QUESTIONS.md 1a).
   *Constraint:* preconditions must be **advisory** — they may flag a stale read
   but must not change who wins a key — or they break the determinism argument
   and the `/api/state` byte-identity guarantee. The fold's winner selection must
   remain a function of class + ancestry + id alone.

3. **Receipt verification** — make a completion *count* only against evidence
   (tool/exit/artifact hashes), with leaf vs composite demarcation
   (OPEN-QUESTIONS.md 1c).
   *Constraint:* the fold must stay a **pure function of the record** — it cannot
   run commands or hash artifacts. Verification therefore happens out-of-band and
   re-enters as a companion `override` record, preserving "no resolution service
   in the state path" (DESIGN.md §7). Moving verification into the ingest path
   would reintroduce the trusted service the design deliberately omitted — a
   governance call, not a free optimisation.

The through-line: every evolution is gated by the same two invariants the fold
already pays for — **determinism** (same record → same state, both ports,
byte-for-byte) and **no trusted decider in the state path**. An evolution that
cannot preserve both is a different system, not a next version of this one.
