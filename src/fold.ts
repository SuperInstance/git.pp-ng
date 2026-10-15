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

// Reproduce Python's float repr (CPython 'r' format == json.dumps of a float)
// for a non-integral finite value, or an integral value outside the plain-int
// band: shortest round-trip digits, scientific when the decimal point position
// `decpt` satisfies `decpt <= -4 || decpt > 16`, exponent sign-prefixed and
// zero-padded to >=2 digits. The shortest digits come from `toExponential()`,
// which (with no argument) yields the minimal unique representation — the same
// digit sequence Python's shortest-repr produces, so the two ports agree.
function pyFloatRepr(n: number): string {
  const neg = n < 0;
  const abs = Math.abs(n);
  const exp = abs.toExponential();
  const eIdx = exp.indexOf("e");
  const mant = exp.slice(0, eIdx);
  const E = parseInt(exp.slice(eIdx + 1), 10);
  const decpt = E + 1;
  let s: string;
  if (decpt <= -4 || decpt > 16) {
    const eSign = E < 0 ? "-" : "+";
    s = mant + "e" + eSign + String(Math.abs(E)).padStart(2, "0");
  } else {
    // Fixed notation in this band never uses an exponent in either port, so
    // JS's shortest fixed form already matches Python's.
    s = String(abs);
  }
  return neg ? "-" + s : s;
}

function pyNumber(n: number): string {
  // JS numbers do not distinguish int from float after JSON.parse, so this
  // reproduces Python's json.dumps number rendering (after the referee's
  // _leaf_num, which spells an integral value within |v| < 1e21 as a plain
  // integer). An integral value in that band takes the EXACT integer spelling
  // via BigInt — `String(n)` would give the shortest round-trip, which diverges
  // from Python's int() for integral doubles in [1e16, 1e21) (e.g.
  // 724818294626474393600, not ...400000). Zero (incl. -0) is "0"; everything
  // else (non-integral, or |v| >= 1e21) goes through Python float repr.
  if (Number.isNaN(n)) return "NaN";
  if (!Number.isFinite(n)) return n > 0 ? "Infinity" : "-Infinity";
  if (n === 0) return "0";
  if (Number.isInteger(n) && Math.abs(n) < 1e21) return BigInt(n).toString();
  return pyFloatRepr(n);
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

// ---------------------------------------------------------------------------
// checkpoint v2 — closure-root pinning (see docs/CHECKPOINT-V2.md, DESIGN §6).
// Synchronous SHA-256 (the Worker's WebCrypto is async; the fold is sync) so
// the digest is a pure function reproducible by the Python referee and any
// third implementation. Validated against hashlib on the standard vectors.
// ---------------------------------------------------------------------------

const _SHA_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function sha256Hex(input: string): string {
  const H = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
    0x1f83d9ab, 0x5be0cd19,
  ]);
  const bytes = new TextEncoder().encode(input);
  const bitLen = bytes.length * 8;
  const padded = new Uint8Array(Math.ceil((bytes.length + 1 + 8) / 64) * 64);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const dv = new DataView(padded.buffer);
  dv.setUint32(padded.length - 4, bitLen >>> 0, false);
  dv.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000), false);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number): number => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = H[0], b = H[1], c = H[2], d = H[3];
    let e = H[4], f = H[5], g = H[6], h = H[7];
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + _SHA_K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
    H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
    H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  let hex = "";
  for (let i = 0; i < 8; i++) hex += H[i].toString(16).padStart(8, "0");
  return hex;
}

/** SHA-256 of a record's WHOLE canonical content: id, parents (as a set,
 * sorted by code point), kind (raw), effects (float64 domain), and reason
 * (raw, null if absent). Hashing the whole record — reason included — means
 * any content tamper changes the leaf, so it is caught: a tamper that alters
 * derived state is MISMATCH, and a reason re-version (which the fold ignores,
 * so derived state is unchanged) is STATE-ONLY rather than a silent VERIFIED.
 * Mirrors the referee's _record_leaf. */
function recordLeaf(rec: FoldRecord): string {
  const r = rec as { id?: unknown; kind?: unknown; reason?: unknown };
  const normalized = {
    id: String(r.id ?? ""),
    parents: parentsOf(rec).slice().sort(cmpCodePoints),
    kind: r.kind ?? null,
    effects: effectsOf(rec),
    reason: r.reason ?? null,
  };
  return sha256Hex(pyCanonical(normalized));
}

/** `at` plus all transitive parents within idx, or null if any is absent. */
function closureIds(
  at: string,
  idx: { [id: string]: FoldRecord },
  parents: ParentsMap,
): string[] | null {
  const closure = new Set<string>();
  const stack: string[] = [at];
  while (stack.length > 0) {
    const n = stack.pop() as string;
    if (closure.has(n)) continue;
    closure.add(n);
    for (const p of parents[n] ?? []) {
      if (!Object.prototype.hasOwnProperty.call(idx, p)) return null;
      stack.push(p);
    }
  }
  return [...closure];
}

/**
 * Deterministic digest of `at`'s full ancestor closure as seen in `records`;
 * null if `at` is absent or its closure is incomplete. Mirrors the referee's
 * checkpoint_root:
 *
 *   closureRoot = SHA-256( "\n".join( id + ":" + recordLeaf(rec)
 *                          for id in sorted(closureIds, code-point) ) )
 *
 * where recordLeaf covers the whole canonical record (id, parents sorted, kind,
 * effects, reason) in the float64 domain.
 */
export function checkpointRoot(
  records: FoldRecord[],
  at: string,
): string | null {
  const idx = indexRecords([...records]);
  const atId = String(at);
  if (!Object.prototype.hasOwnProperty.call(idx, atId)) return null;
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);
  const closure = closureIds(atId, idx, parents);
  if (closure === null) return null;
  const entries = closure
    .sort(cmpCodePoints)
    .map((rid) => `${rid}:${recordLeaf(idx[rid])}`);
  return sha256Hex(entries.join("\n"));
}

export type CheckpointResult = boolean | "VERIFIED" | "STATE-ONLY" | "MISMATCH";

/**
 * Verify a checkpoint against `records`.
 *
 * v1 (no `closureRoot` in the checkpoint): returns a bool, unchanged — true iff
 * deriving from exactly the ancestors of `at` reproduces `state`.
 *
 * v2 (a `closureRoot` present): returns a three-way string — "VERIFIED" (state
 * matches AND the closure digest matches the pinned root), "STATE-ONLY" (state
 * matches but the closure has changed since the checkpoint — the honest v1
 * answer, now labelled, never a silent VERIFIED), or "MISMATCH" (state differs,
 * or the checkpoint shape is unusable). Backwards compatible.
 */
export function verifyCheckpoint(
  records: FoldRecord[],
  checkpoint: { at?: unknown; state?: unknown; closureRoot?: unknown } | null,
): CheckpointResult {
  if (typeof checkpoint !== "object" || checkpoint === null) return false;
  const expectedRoot = checkpoint.closureRoot;
  const v2 = expectedRoot !== undefined && expectedRoot !== null;
  const atRaw = checkpoint.at;
  const want = checkpoint.state;
  if (
    atRaw === undefined ||
    atRaw === null ||
    typeof want !== "object" ||
    want === null ||
    Array.isArray(want)
  ) {
    return v2 ? "MISMATCH" : false;
  }
  const at = String(atRaw);

  const idx = indexRecords([...records]);
  let stateOk = false;
  if (Object.prototype.hasOwnProperty.call(idx, at)) {
    const parents: ParentsMap = {};
    for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);
    const closure = closureIds(at, idx, parents);
    if (closure !== null) {
      const subset = closure.sort(cmpCodePoints).map((rid) => idx[rid]);
      stateOk = pyDeepEqual(derive(subset).state, want);
    }
  }

  if (!v2) return stateOk;
  if (!stateOk) return "MISMATCH";
  const actualRoot = checkpointRoot(records, at);
  return actualRoot === String(expectedRoot) ? "VERIFIED" : "STATE-ONLY";
}

// ---------------------------------------------------------------------------
// delta derivation (checkpoint + tail) — the read-path optimisation.
// A checkpoint pins a record's ancestor closure (state + closure root + the
// closure records). The read path re-derives without re-reading the closure:
// supply the checkpoint and only the TAIL (records outside the closure), and
// derive() runs over closure ∪ tail — the full record set, so the result is
// byte-identical to derive(all). The win is on READS; the fold is unchanged.
// Mirrors the referee's build_checkpoint / derive_delta.
// ---------------------------------------------------------------------------

export interface Checkpoint {
  at: string;
  state: { [key: string]: unknown };
  closureRoot: string;
  closure: FoldRecord[];
}

/** Construct a checkpoint for `at` over `records`, or null if `at`'s closure is
 * incomplete. Closure records are emitted in canonical (code-point id) order so
 * both ports build an identical object. */
export function buildCheckpoint(
  records: FoldRecord[],
  at: string,
): Checkpoint | null {
  const idx = indexRecords([...records]);
  const atId = String(at);
  if (!Object.prototype.hasOwnProperty.call(idx, atId)) return null;
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);
  const closure = closureIds(atId, idx, parents);
  if (closure === null) return null;
  const closureRecords = closure.sort(cmpCodePoints).map((rid) => idx[rid]);
  const root = checkpointRoot(records, atId);
  if (root === null) return null;
  return {
    at: atId,
    state: derive(closureRecords).state,
    closureRoot: root,
    closure: closureRecords,
  };
}

/** Delta derivation. Returns a DeriveOutput equal to derive(closure ∪ tail), or
 * null to signal "fall back to the full fold". Null on any doubt: malformed
 * checkpoint, a duplicate id in the closure, a stored closure that is not
 * exactly `at`'s ancestor closure or whose root != the pinned closureRoot,
 * a closure that does not re-derive to the pinned state, or a tail record that
 * shares an id with the closure. */
export function deriveDelta(
  checkpoint: Checkpoint | null | undefined,
  tailRecords: FoldRecord[],
): DeriveOutput | null {
  if (typeof checkpoint !== "object" || checkpoint === null) return null;
  const { at, state, closureRoot, closure } = checkpoint;
  if (
    typeof at !== "string" ||
    typeof state !== "object" ||
    state === null ||
    Array.isArray(state) ||
    typeof closureRoot !== "string" ||
    !Array.isArray(closure)
  ) {
    return null;
  }
  const closureIdSet = new Set<string>();
  for (const r of closure) {
    const id = (r as { id?: unknown }).id;
    if (typeof id !== "string") return null;
    if (closureIdSet.has(id)) return null; // duplicate id in the closure
    closureIdSet.add(id);
  }
  if (!closureIdSet.has(at)) return null;
  // The stored closure must be exactly `at`'s ancestor closure within itself.
  const idx = indexRecords([...closure]);
  const parents: ParentsMap = {};
  for (const rid of Object.keys(idx)) parents[rid] = parentsOf(idx[rid]);
  const cl = closureIds(at, idx, parents);
  if (cl === null) return null;
  if (cl.length !== closureIdSet.size || !cl.every((id) => closureIdSet.has(id))) {
    return null;
  }
  if (checkpointRoot(closure, at) !== closureRoot) return null;
  if (!pyDeepEqual(derive(closure).state, state)) return null;
  for (const r of tailRecords) {
    const id = (r as { id?: unknown }).id;
    if (typeof id === "string" && closureIdSet.has(id)) return null;
  }
  return derive([...closure, ...tailRecords]);
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
