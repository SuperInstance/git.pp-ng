/**
 * fold.ts — deterministic fold from a record of task completions to current
 * state. TypeScript port of the Python referee (referee/fold.py), held
 * bit-for-bit identical to it by the conformance harness in ../conformance/.
 *
 * CLASS PRIORITY is ABSOLUTE: override > task > witness. A witness never
 * outranks a task or an override, even when the witness is a descendant —
 * seeing a value is not deciding it. Ancestry decides only *within* the
 * highest class present for a key. Record kinds outside
 * {"task","witness","override"} rank as witnesses, because we cannot verify
 * that they carry intent.
 *
 * TIE-BREAK — the cleanest chain wins: among equally-derived candidates of the
 * same class where none is an ancestor of another, the record whose ancestry
 * holds the most intent records (task + override); if equal, the fewest witness
 * records; if still equal, the lexicographically (code-point) smallest id.
 *
 * Port notes (this file is verified against the Python referee by the
 * conformance harness in ../conformance/):
 * - The duplicate-id canonical ordering reproduces Python's
 *   json.dumps(..., sort_keys=True) byte-for-byte (see pyCanonical), so the
 *   record that survives a duplicate id is the same one Python keeps.
 * - Distinct-value detection for conflicts reproduces Python equality for
 *   JSON scalars (in Python, 1 == 1.0 == True; "1" != 1; None only == None).
 * - Checkpoint state comparison is key-order-insensitive deep equality
 *   with the same scalar semantics (Python dict ==), not JSON text equality.
 */

export interface FoldRecord {
  id: string;
  parents?: string[];
  kind?: string;
  effects?: { [key: string]: unknown };
  reason?: string;
  [key: string]: unknown;
}

export interface Conflict {
  key: string;
  winner: string;
  losers: string[];
}

export interface DeriveOutput {
  state: { [key: string]: unknown };
  order: string[];
  conflicts: Conflict[];
  orphans: string[];
}

const CLASS_RANK: { [key: string]: number } = {
  override: 0,
  task: 1,
  witness: 2,
};
const FALLBACK_RANK = 2;
const WITNESS_RANK = CLASS_RANK["witness"];

// ---------------------------------------------------------------------------
// Python-compatible canonical JSON (json.dumps with sort_keys=True,
// ensure_ascii=True, default ", " / ": " separators). Used wherever the
// Python implementation lets a serialized form decide an outcome.
// ---------------------------------------------------------------------------

function pyEscapeString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    const ch = s[i];
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (code < 0x20 || code >= 0x7f) {
      out += "\\u" + code.toString(16).padStart(4, "0");
    } else out += ch;
  }
  return out + '"';
}

function pyNumber(n: number): string {
  // JS numbers do not distinguish int from float after JSON.parse; the
  // referee's fixtures (and real record.json effects) use ints, so integral
  // values take the int spelling. Non-integral values use the shortest
  // round-trip form, which agrees with Python repr for ordinary decimals.
  if (Number.isNaN(n)) return "NaN";
  if (!Number.isFinite(n)) return n > 0 ? "Infinity" : "-Infinity";
  if (Object.is(n, -0)) return "-0.0";
  return String(n);
}

export function pyCanonical(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return pyNumber(value);
  if (typeof value === "string") return pyEscapeString(value);
  if (Array.isArray(value)) {
    return "[" + value.map(pyCanonical).join(", ") + "]";
  }
  if (typeof value === "object") {
    const obj = value as { [key: string]: unknown };
    // Python json.dumps(sort_keys=True) orders keys by code point; match it so
    // the canonical form (and the value/dup-id keys built from it) agree for
    // non-BMP object keys too.
    const keys = Object.keys(obj).sort(cmpCodePoints);
    return (
      "{" +
      keys.map((k) => pyEscapeString(k) + ": " + pyCanonical(obj[k])).join(", ") +
      "}"
    );
  }
  // Python json.dumps(default=str): fall back to the string form.
  return pyEscapeString(String(value));
}

/** Grouping key that reproduces Python `==` for JSON values: numbers and
 * booleans share one numeric bucket (1 == 1.0 == True), strings and null
 * are their own buckets, compound values compare by canonical form
 * (Python compares dicts/lists by value). */
function pyValueKey(v: unknown): string {
  if (typeof v === "boolean") return "n:" + (v ? "1" : "0");
  if (typeof v === "number") return "n:" + String(v);
  if (typeof v === "string") return "s:" + v;
  if (v === null || v === undefined) return "z:";
  return "c:" + pyCanonical(v);
}

/** Deep equality with Python semantics (used for checkpoint states). */
function pyDeepEqual(a: unknown, b: unknown): boolean {
  const aNum = typeof a === "number" || typeof a === "boolean";
  const bNum = typeof b === "number" || typeof b === "boolean";
  if (aNum || bNum) {
    if (!(aNum && bNum)) return false;
    return Number(a) === Number(b);
  }
  if (a === null || b === null || a === undefined || b === undefined) {
    return (a ?? null) === (b ?? null);
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!(Array.isArray(a) && Array.isArray(b))) return false;
    if (a.length !== b.length) return false;
    return a.every((v, i) => pyDeepEqual(v, b[i]));
  }
  if (typeof a === "object" || typeof b === "object") {
    if (!(typeof a === "object" && typeof b === "object")) return false;
    const ao = a as { [k: string]: unknown };
    const bo = b as { [k: string]: unknown };
    const ak = Object.keys(ao);
    const bk = Object.keys(bo);
    if (ak.length !== bk.length) return false;
    return ak.every(
      (k) => Object.prototype.hasOwnProperty.call(bo, k) && pyDeepEqual(ao[k], bo[k]),
    );
  }
  return a === b;
}

// ---------------------------------------------------------------------------
// Record accessors (mirror fold.py's _parents/_effects/_rank)
// ---------------------------------------------------------------------------

function parentsOf(rec: FoldRecord): string[] {
  const ps = (rec as { parents?: unknown }).parents;
  if (!Array.isArray(ps)) return [];
  return ps.map((p) => String(p));
}

function effectsOf(rec: FoldRecord): { [key: string]: unknown } {
  const eff = (rec as { effects?: unknown }).effects;
  const out: { [key: string]: unknown } = {};
  if (eff && typeof eff === "object" && !Array.isArray(eff)) {
    for (const [k, v] of Object.entries(eff as { [key: string]: unknown })) {
      out[String(k)] = v;
    }
  }
  return out;
}

function rankOf(rec: FoldRecord): number {
  const kind = (rec as { kind?: unknown }).kind;
  if (typeof kind === "string" &&
      Object.prototype.hasOwnProperty.call(CLASS_RANK, kind)) {
    return CLASS_RANK[kind];
  }
  return FALLBACK_RANK;
}

/**
 * Compare two strings by Unicode CODE POINT sequence, matching Python's native
 * string ordering. JavaScript's built-in `<`/`>` and `Array.sort` compare by
 * UTF-16 code UNIT, which disagrees with Python for ids/keys containing non-BMP
 * characters: a high surrogate (U+D800–U+DBFF) sorts below a BMP char like
 * U+FFFF under UTF-16, but the astral code point it encodes (≥ U+10000) sorts
 * ABOVE it by code point. Every ordering in the fold that can decide an outcome
 * (id tie-breaks, effect-key order, the Kahn heap) therefore routes through
 * this comparator so TS and Python elect the same winner (ADDENDUM #1 fix).
 */
function cmpCodePoints(a: string, b: string): number {
  const ai = Array.from(a); // iterates by code point, not UTF-16 unit
  const bi = Array.from(b);
  const n = Math.min(ai.length, bi.length);
  for (let i = 0; i < n; i++) {
    const ca = ai[i].codePointAt(0) as number;
    const cb = bi[i].codePointAt(0) as number;
    if (ca !== cb) return ca < cb ? -1 : 1;
  }
  return ai.length - bi.length;
}

/** id -> record, built in the same canonical order the Python referee
 * uses, so input permutation (and duplicate-id resolution) cannot differ. */
function indexRecords(
  records: FoldRecord[],
): { [id: string]: FoldRecord } {
  const ordered = [...records].sort((a, b) => {
    const ida = String((a as { id?: unknown }).id ?? "");
    const idb = String((b as { id?: unknown }).id ?? "");
    const byId = cmpCodePoints(ida, idb);
    if (byId !== 0) return byId;
    // Secondary key: Python sorts by json.dumps(..., sort_keys=True), which
    // ensure_ascii escapes to pure ASCII; pyCanonical matches byte-for-byte,
    // so a code-point compare here is identical to Python's.
    return cmpCodePoints(pyCanonical(a), pyCanonical(b));
  });
  const idx: { [id: string]: FoldRecord } = {};
  for (const r of ordered) {
    idx[String((r as { id?: unknown }).id)] = r;
  }
  return idx;
}

// ---------------------------------------------------------------------------
// DAG helpers (min-heap Kahn, exactly as fold.py)
// ---------------------------------------------------------------------------

class MinHeap {
  private h: string[] = [];
  push(x: string): void {
    const h = this.h;
    h.push(x);
    let i = h.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (cmpCodePoints(h[p], h[i]) <= 0) break;
      [h[p], h[i]] = [h[i], h[p]];
      i = p;
    }
  }
  pop(): string | undefined {
    const h = this.h;
    if (h.length === 0) return undefined;
    const top = h[0];
    const last = h.pop() as string;
    if (h.length > 0) {
      h[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < h.length && cmpCodePoints(h[l], h[m]) < 0) m = l;
        if (r < h.length && cmpCodePoints(h[r], h[m]) < 0) m = r;
        if (m === i) break;
        [h[m], h[i]] = [h[i], h[m]];
        i = m;
      }
    }
    return top;
  }
  get size(): number {
    return this.h.length;
  }
}

type ParentsMap = { [id: string]: string[] };

/** Nodes that cannot be topologically ordered (in, or blocked behind, a
 * parent cycle). Mirrors fold.py's _cycle_affected. */
function cycleAffected(nodes: Set<string>, parents: ParentsMap): Set<string> {
  const indeg: { [id: string]: number } = {};
  const children: { [id: string]: string[] } = {};
  for (const r of nodes) {
    indeg[r] = 0;
    children[r] = [];
  }
  for (const r of nodes) {
    for (const p of parents[r]) {
      if (Object.prototype.hasOwnProperty.call(indeg, p)) {
        indeg[r] += 1;
        children[p].push(r);
      }
    }
  }
  const heap = new MinHeap();
  for (const r of nodes) if (indeg[r] === 0) heap.push(r);
  const done = new Set<string>();
  let n: string | undefined;
  while ((n = heap.pop()) !== undefined) {
    done.add(n);
    for (const c of children[n]) {
      indeg[c] -= 1;
      if (indeg[c] === 0) heap.push(c);
    }
  }
  const out = new Set<string>(nodes);
  for (const d of done) out.delete(d);
  return out;
}

/** Kahn's algorithm, always taking the smallest ready id: one canonical
 * order. Mirrors fold.py's _topological (indegree counts all parents). */
function topological(nodes: Set<string>, parents: ParentsMap): string[] {
  const indeg: { [id: string]: number } = {};
  const children: { [id: string]: string[] } = {};
  for (const r of nodes) {
    indeg[r] = parents[r].length;
    children[r] = [];
  }
  for (const r of nodes) {
    for (const p of parents[r]) {
      if (Object.prototype.hasOwnProperty.call(children, p)) {
        children[p].push(r);
      }
    }
  }
  const heap = new MinHeap();
  for (const r of nodes) if (indeg[r] === 0) heap.push(r);
  const order: string[] = [];
  let n: string | undefined;
  while ((n = heap.pop()) !== undefined) {
    order.push(n);
    for (const c of children[n]) {
      indeg[c] -= 1;
      if (indeg[c] === 0) heap.push(c);
    }
  }
  return order;
}

// ---------------------------------------------------------------------------
// derive / verify_checkpoint
// ---------------------------------------------------------------------------

export function derive(records: FoldRecord[]): DeriveOutput {
  const idx = indexRecords([...records]);
  const ids = new Set<string>(Object.keys(idx));
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);

  const children: { [id: string]: string[] } = {};
  for (const rid of Object.keys(idx)) children[rid] = [];
  for (const [rid, ps] of Object.entries(parents)) {
    for (const p of ps) {
      if (Object.prototype.hasOwnProperty.call(children, p)) {
        children[p].push(rid);
      }
    }
  }

  // Orphans: a record is orphaned if any ancestor is absent, or is itself
  // already orphaned. Worklist propagation, as in fold.py.
  const orphan = new Set<string>();
  const work: string[] = [];
  for (const rid of Object.keys(idx)) {
    if (parents[rid].some((p) => !ids.has(p))) work.push(rid);
  }
  for (const rid of work) orphan.add(rid);
  let head = 0;
  while (head < work.length) {
    for (const c of children[work[head]] ?? []) {
      if (!orphan.has(c)) {
        orphan.add(c);
        work.push(c);
      }
    }
    head += 1;
  }
  const nonOrphan = new Set<string>();
  for (const rid of ids) if (!orphan.has(rid)) nonOrphan.add(rid);
  for (const rid of cycleAffected(nonOrphan, parents)) orphan.add(rid);

  const applied: string[] = [];
  for (const rid of ids) if (!orphan.has(rid)) applied.push(rid);
  applied.sort(cmpCodePoints);
  const appliedSet = new Set<string>(applied);
  const order = topological(new Set<string>(applied), parents);

  // Full applied-ancestor set of every applied record, memoised once per call
  // in topological order (parents precede children). anc[rid] is naturally
  // applied-restricted: an applied record's parents are all applied, or it
  // would itself be an orphan. Precomputing here means no ancestor walk is
  // repeated across candidates or keys — the per-candidate re-walks were
  // superlinear on deep contested keys (ADDENDUM #4 P5). Mirrors fold.py.
  const anc: { [id: string]: Set<string> } = {};
  for (const rid of order) {
    const a = new Set<string>();
    for (const p of parents[rid]) {
      if (appliedSet.has(p)) {
        a.add(p);
        for (const x of anc[p]) a.add(x);
      }
    }
    anc[rid] = a;
  }

  // Who sets each key, among applied records only.
  const setters: { [key: string]: Set<string> } = {};
  for (const rid of applied) {
    for (const k of Object.keys(effectsOf(idx[rid]))) {
      if (!setters[k]) setters[k] = new Set<string>();
      setters[k].add(rid);
    }
  }

  const state: { [key: string]: unknown } = {};
  const conflicts: Conflict[] = [];
  for (const k of Object.keys(setters).sort(cmpCodePoints)) {
    const cands = [...setters[k]].sort(cmpCodePoints);
    let best = Infinity;
    for (const c of cands) best = Math.min(best, rankOf(idx[c]));
    const pool = new Set<string>(cands.filter((c) => rankOf(idx[c]) === best));
    // A candidate is dominated iff it is a (transitive) ancestor of ANY other
    // pool candidate; the survivors are the most-derived. Exact maximality
    // straight from the precomputed ancestor sets, so the result cannot depend
    // on parents-array order (ADDENDUM #3 fix).
    const dominated = new Set<string>();
    for (const d of pool) {
      for (const a of anc[d]) if (pool.has(a)) dominated.add(a);
    }
    const maximal = [...pool].filter((c) => !dominated.has(c)).sort(cmpCodePoints);
    let winner: string;
    if (maximal.length === 1) {
      winner = maximal[0];
    } else {
      // Tie-break: the cleanest chain wins — the candidate whose ancestry holds
      // the most intent records (task + override), then the fewest witness
      // records, then the smallest id. More deeds behind a value makes it
      // cleaner; more bare observations, weaker.
      const cleanliness = (c: string): [number, number, string] => {
        let intent = 0;
        let wit = 0;
        for (const a of anc[c]) {
          const r = rankOf(idx[a]);
          if (r < WITNESS_RANK) intent += 1;
          if (r === WITNESS_RANK) wit += 1;
        }
        return [-intent, wit, c];
      };
      winner = [...maximal].sort((a, b) => {
        const ca = cleanliness(a);
        const cb = cleanliness(b);
        if (ca[0] !== cb[0]) return ca[0] - cb[0];
        if (ca[1] !== cb[1]) return ca[1] - cb[1];
        return cmpCodePoints(ca[2], cb[2]);
      })[0];
    }

    state[k] = effectsOf(idx[winner])[k];
    const distinct = new Set<string>();
    for (const c of cands) distinct.add(pyValueKey(effectsOf(idx[c])[k]));
    if (distinct.size >= 2) {
      conflicts.push({
        key: k,
        winner,
        losers: cands.filter((c) => c !== winner).sort(cmpCodePoints),
      });
    }
  }

  const orphans = [...orphan].sort(cmpCodePoints);
  return { state, order, conflicts, orphans };
}

export function verifyCheckpoint(
  records: FoldRecord[],
  checkpoint: { at?: unknown; state?: unknown } | null,
): boolean {
  if (typeof checkpoint !== "object" || checkpoint === null) return false;
  const atRaw = checkpoint.at;
  const want = checkpoint.state;
  if (atRaw === undefined || atRaw === null) return false;
  if (typeof want !== "object" || want === null || Array.isArray(want)) {
    return false;
  }
  const at = String(atRaw);

  const idx = indexRecords([...records]);
  if (!Object.prototype.hasOwnProperty.call(idx, at)) return false;
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);

  const closure = new Set<string>();
  const stack: string[] = [at];
  while (stack.length > 0) {
    const n = stack.pop() as string;
    if (closure.has(n)) continue;
    closure.add(n);
    for (const p of parents[n] ?? []) {
      if (!Object.prototype.hasOwnProperty.call(idx, p)) return false;
      stack.push(p);
    }
  }

  const subset = [...closure].sort(cmpCodePoints).map((rid) => idx[rid]);
  return pyDeepEqual(derive(subset).state, want);
}

/**
 * Per-witness staleness, as a record-count distance (TypeScript port of the
 * referee's witness_freshness; checked against it by the conformance harness).
 *
 * recordsBehind(w) = (number of applied records) - (number of applied
 * records in w's own causal past, w included). It counts the applied records
 * that landed without w in their ancestry: the records w did not witness. A
 * witness with recordsBehind 0 saw everything applied; a larger number is a
 * staler observation.
 *
 * Pure function of the record, like derive(). Returns a map from witness id
 * to recordsBehind for every APPLIED record of kind "witness" (orphaned and
 * cycle-excluded witnesses are absent, exactly as they are absent from
 * derive()'s state). derive()'s own output is untouched, so /api/state is
 * unaffected.
 */
export function witnessFreshness(records: FoldRecord[]): { [id: string]: number } {
  const idx = indexRecords([...records]);
  const out = derive(records);
  const applied = new Set<string>(out.order);
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);

  const ancestorsIncl = (start: string): Set<string> => {
    const seen = new Set<string>([start]);
    const stack: string[] = [...(parents[start] ?? [])];
    while (stack.length > 0) {
      const n = stack.pop() as string;
      if (seen.has(n) || !applied.has(n)) continue;
      seen.add(n);
      for (const p of parents[n] ?? []) stack.push(p);
    }
    return seen;
  };

  const total = applied.size;
  const result: { [id: string]: number } = {};
  for (const rid of out.order) {
    const kind = (idx[rid] as { kind?: unknown }).kind;
    if (kind === "witness") result[rid] = total - ancestorsIncl(rid).size;
  }
  return result;
}
