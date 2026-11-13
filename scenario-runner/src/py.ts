/**
 * Python semantics the port has to keep.
 *
 * The scenario runner was written in Python, and a lot of its behaviour hides in the language:
 * `x or 0` treats `0`, `""`, `[]` and `{}` as missing; `int("3.5")` raises where `parseInt` would
 * answer 3; `round()` rounds half to EVEN; `str(['a'])` prints `['a']`. Every one of those reaches a
 * verdict or a prompt somewhere, so they live here, once, instead of being re-guessed at each call.
 *
 * Deliberate difference: JSON does not say whether `7.0` was a float, and a JavaScript number
 * cannot remember it. Python prints `7.0`; this prints `7`. Nothing branches on it — it only changes
 * how an integer-valued float reads in a prompt or a report. Everything else matches Python.
 */

export type Dict = Record<string, unknown>;

/** Python's TypeError — a value of the wrong kind (`float(None)`, `int([])`). */
export class PyTypeError extends Error {
  override name = "TypeError";
}

/** Python's ValueError — the right kind with an unusable value (`int("3.5")`, `float("x")`). */
export class PyValueError extends Error {
  override name = "ValueError";
}

/** Python's OverflowError (`int(float("inf"))`). NOT caught by the `tryInt`/`tryFloat` helpers,
 * because the Python code it was ported from only ever catches TypeError and ValueError. */
export class PyOverflowError extends Error {
  override name = "OverflowError";
}

/** True for the exceptions a Python `except (TypeError, ValueError)` catches. */
export function isConversionError(e: unknown): boolean {
  return e instanceof PyTypeError || e instanceof PyValueError;
}

// ── Kinds ────────────────────────────────────────────────────────────────────────────

/** `isinstance(v, dict)` — a plain JSON object. */
export function isDict(v: unknown): v is Dict {
  return typeof v === "object" && v !== null && !Array.isArray(v) && !(v instanceof Set) && !(v instanceof Map);
}

/** `isinstance(v, list)` (or tuple). */
export function isList(v: unknown): v is unknown[] {
  return Array.isArray(v);
}

/** `isinstance(v, (int, float))` — which, in Python, includes bool. */
export function isNumber(v: unknown): v is number | boolean {
  return typeof v === "number" || typeof v === "boolean";
}

/** `bool(v)`. Only `None`, `False`, `0`, `""` and empty containers are false; NaN is true. */
export function truthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false) return false;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (v instanceof Set || v instanceof Map) return v.size > 0;
  if (typeof v === "object") return Object.keys(v as object).length > 0;
  return true;
}

/** `a or b`. */
export function or<A, B>(a: A, b: B): A | B {
  return truthy(a) ? a : b;
}

/** `d.get(key)` — `null` (Python None) when the key is missing or the value is null. */
export function get(d: unknown, key: string): unknown {
  if (!isDict(d)) return null;
  const v = d[key];
  return v === undefined ? null : v;
}

/** `key in d`. */
export function has(d: unknown, key: string): boolean {
  return isDict(d) && Object.prototype.hasOwnProperty.call(d, key);
}

// ── Conversions ──────────────────────────────────────────────────────────────────────

const INT_RE = /^[+-]?\d(?:_?\d)*$/;
const FLOAT_RE = /^[+-]?(?:(?:\d(?:_?\d)*)?\.?\d(?:_?\d)*(?:[eE][+-]?\d(?:_?\d)*)?|\d(?:_?\d)*\.(?:[eE][+-]?\d(?:_?\d)*)?|inf|infinity|nan)$/i;

/** `int(v)`. Truncates a float, parses only a whole-number string, raises like Python. */
export function pyInt(v: unknown): number {
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") {
    if (Number.isNaN(v)) throw new PyValueError("cannot convert float NaN to integer");
    if (!Number.isFinite(v)) throw new PyOverflowError("cannot convert float infinity to integer");
    return Math.trunc(v) || 0;
  }
  if (typeof v === "string") {
    const s = v.trim();
    if (!INT_RE.test(s)) throw new PyValueError(`invalid literal for int() with base 10: ${pyRepr(v)}`);
    return Number(s.replace(/_/g, ""));
  }
  throw new PyTypeError(`int() argument must be a string, a bytes-like object or a real number, not '${pyTypeName(v)}'`);
}

/** `float(v)`. */
export function pyFloat(v: unknown): number {
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const s = v.trim();
    if (!FLOAT_RE.test(s)) throw new PyValueError(`could not convert string to float: ${pyRepr(v)}`);
    const low = s.toLowerCase().replace(/^[+]/, "");
    if (low === "inf" || low === "infinity") return Infinity;
    if (low === "-inf" || low === "-infinity") return -Infinity;
    if (low === "nan" || low === "-nan") return NaN;
    return Number(s.replace(/_/g, ""));
  }
  throw new PyTypeError(`float() argument must be a string or a real number, not '${pyTypeName(v)}'`);
}

/** `float(v)`, or `fallback` where Python's `except (TypeError, ValueError)` would catch. */
export function tryFloat<F = null>(v: unknown, fallback: F = null as F): number | F {
  try {
    return pyFloat(v);
  } catch (e) {
    if (isConversionError(e)) return fallback;
    throw e;
  }
}

/** `int(v)`, or `fallback` where Python's `except (TypeError, ValueError)` would catch. */
export function tryInt<F = null>(v: unknown, fallback: F = null as F): number | F {
  try {
    return pyInt(v);
  } catch (e) {
    if (isConversionError(e)) return fallback;
    throw e;
  }
}

/** `float(d.get(key) or 0)` — the commonest money read in the Python. Raises on a non-number. */
export function floatOr0(v: unknown): number {
  return pyFloat(or(v, 0));
}

/** `int(d.get(key) or 0)`. Raises on a non-number. */
export function intOr0(v: unknown): number {
  return pyInt(or(v, 0));
}

export function pyTypeName(v: unknown): string {
  if (v === null || v === undefined) return "NoneType";
  if (typeof v === "boolean") return "bool";
  if (typeof v === "number") return Number.isInteger(v) ? "int" : "float";
  if (typeof v === "string") return "str";
  if (Array.isArray(v)) return "list";
  if (v instanceof Set) return "set";
  return "dict";
}

// ── Rounding ─────────────────────────────────────────────────────────────────────────

/** The exact decimal expansion of a double (finite, |x| < 1e21), unsigned, as [int, frac]. */
function exactDigits(x: number): [string, string] {
  const s = Math.abs(x).toFixed(100);
  const [ip, fp = ""] = s.split(".");
  return [ip, fp];
}

/**
 * `round(x)` / `round(x, nd)` with Python's rules: half to EVEN, decided on the double's EXACT
 * value — so `round(2.675, 2)` is 2.67 (2.675 is really 2.67499…) and `round(0.125, 2)` is 0.12.
 * `round(x)` with no digits returns an integer, as Python does.
 */
export function pyRound(x: number, nd?: number): number {
  if (typeof x === "boolean") x = x ? 1 : 0;
  if (!Number.isFinite(x)) {
    if (nd === undefined) throw new PyOverflowError("cannot convert float to integer");
    return x;
  }
  if (nd === undefined) {
    const f = Math.floor(x);
    const diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f || 0;
    return (f % 2 === 0 ? f : f + 1) || 0;
  }
  if (nd < 0) {
    const p = 10 ** -nd;
    return pyRound(x / p) * p;
  }
  if (Number.isInteger(x) || Math.abs(x) >= 1e21 || nd > 90) return x;
  const [ip, fp] = exactDigits(x);
  const keep = fp.slice(0, nd);
  const rest = fp.slice(nd);
  let up = false;
  const first = rest[0] ?? "0";
  if (first > "5") up = true;
  else if (first === "5") {
    if (/[1-9]/.test(rest.slice(1))) up = true;
    else {
      const last = nd > 0 ? keep[nd - 1] : ip[ip.length - 1];
      up = Number(last) % 2 === 1;
    }
  }
  let digits = BigInt(ip + keep);
  if (up) digits += 1n;
  let s = digits.toString().padStart(nd + 1, "0");
  if (nd > 0) s = `${s.slice(0, s.length - nd)}.${s.slice(s.length - nd)}`;
  const v = Number(s);
  return x < 0 ? -v : v;
}

// ── Decimal display (money as the client prints it) ──────────────────────────────────

/** Parse a decimal literal the way `Decimal(str)` accepts it. null = InvalidOperation. */
function parseDecimal(s: string): { neg: boolean; digits: bigint; exp: number } | null {
  const t = s.trim().replace(/_/g, "");
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(t);
  if (!m || (m[2] === "" && (m[3] ?? "") === "")) return null;
  const frac = m[3] ?? "";
  return {
    neg: m[1] === "-",
    digits: BigInt((m[2] || "0") + frac),
    exp: Number(m[4] ?? 0) - frac.length,
  };
}

/** `str(x)` for a number, as Decimal(str(x)) would read it — shortest round-trip digits. */
function numberLiteral(x: number): string {
  return Number.isInteger(x) && Math.abs(x) < 1e16 ? String(x) : pyFloatRepr(x);
}

/**
 * `str(Decimal(str(v)).quantize(10**-places, rounding))` — or null where Python's Decimal raises
 * InvalidOperation on construction (`None`, `True`, a list, a non-numeric string).
 * `mode`: "half_up" = ROUND_HALF_UP, "down" = ROUND_DOWN (truncate toward zero).
 */
export function quantizeDecimal(v: unknown, places: number, mode: "half_up" | "down"): string | null {
  let lit: string;
  if (typeof v === "number") {
    if (!Number.isFinite(v)) throw new Error("InvalidOperation: quantize of a non-finite value");
    lit = numberLiteral(v);
  } else if (typeof v === "string") {
    lit = v;
  } else {
    return null;
  }
  const d = parseDecimal(lit);
  if (!d) return null;
  let { digits } = d;
  const shift = d.exp + places;            // >= 0: pad with zeros; < 0: drop -shift digits
  if (shift >= 0) {
    digits *= 10n ** BigInt(shift);
  } else {
    const div = 10n ** BigInt(-shift);
    const q = digits / div;
    const r = digits % div;
    digits = mode === "half_up" && r * 2n >= div ? q + 1n : q;
  }
  let s = digits.toString();
  if (places > 0) {
    s = s.padStart(places + 1, "0");
    s = `${s.slice(0, s.length - places)}.${s.slice(s.length - places)}`;
  }
  return (d.neg ? "-" : "") + s;
}

// ── Printing: str() and repr() ───────────────────────────────────────────────────────

/** `repr(x)` for a float, on JavaScript's shortest round-trip digits (the same digits Python
 * picks). Integer-valued numbers print without `.0` — see the module note. */
export function pyFloatRepr(x: number): string {
  if (Number.isNaN(x)) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  if (Number.isInteger(x) && Math.abs(x) < 1e16) return String(x === 0 ? 0 : x);
  const [mant, expStr] = x.toExponential().split("e");
  const exp = Number(expStr);
  const neg = mant.startsWith("-");
  const digits = mant.replace("-", "").replace(".", "");
  const sign = neg ? "-" : "";
  if (exp >= -4 && exp < 16) {
    if (exp >= 0) {
      const ip = digits.slice(0, exp + 1).padEnd(exp + 1, "0");
      const fp = digits.slice(exp + 1);
      return sign + ip + (fp ? `.${fp}` : "");
    }
    return `${sign}0.${"0".repeat(-exp - 1)}${digits}`;
  }
  const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
  const e = Math.abs(exp).toString().padStart(2, "0");
  return `${sign}${m}e${exp < 0 ? "-" : "+"}${e}`;
}

/** `repr(s)` for a string. */
export function pyStrRepr(s: string): string {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = q;
  for (const ch of s) {
    const c = ch.codePointAt(0)!;
    if (ch === "\\") out += "\\\\";
    else if (ch === q) out += `\\${q}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (c < 0x20 || c === 0x7f) out += `\\x${c.toString(16).padStart(2, "0")}`;
    else out += ch;
  }
  return out + q;
}

/** A Python TUPLE, for printing: `repr` shows `(0, 1)`, not `[0, 1]`. */
export class PyTuple {
  constructor(readonly items: readonly unknown[]) {}
}

/** `repr(v)`. */
export function pyRepr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (v instanceof PyTuple) {
    return v.items.length === 1 ? `(${pyRepr(v.items[0])},)` : `(${v.items.map(pyRepr).join(", ")})`;
  }
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "number") return pyFloatRepr(v);
  if (typeof v === "string") return pyStrRepr(v);
  if (Array.isArray(v)) return `[${v.map(pyRepr).join(", ")}]`;
  if (v instanceof Set) return v.size ? `{${[...v].map(pyRepr).join(", ")}}` : "set()";
  if (isDict(v)) return `{${Object.entries(v).map(([k, x]) => `${pyStrRepr(k)}: ${pyRepr(x)}`).join(", ")}}`;
  return String(v);
}

/** `str(v)`. */
export function pyStr(v: unknown): string {
  return typeof v === "string" ? v : pyRepr(v);
}

/** A value already rendered — `py` inserts it as is (an `!r` conversion, a pre-formatted figure). */
export class PyText {
  constructor(readonly s: string) {}
}

/** `f"{x!r}"`. */
export function r(x: unknown): PyText {
  return new PyText(pyRepr(x));
}

/**
 * An f-string: every interpolated value goes through `str()` as Python would print it
 * (`None`, `True`, `[1, 2]`, `{'a': 1}`), except a `PyText`, which is inserted verbatim.
 *
 *     py`round ${i}: balance ${b.balance} != ${known}`
 */
export function py(strings: TemplateStringsArray, ...values: unknown[]): string {
  let out = strings[0];
  values.forEach((v, i) => {
    out += (v instanceof PyText ? v.s : pyStr(v)) + strings[i + 1];
  });
  return out;
}

/** Python `a < b` on JSON-shaped values: numbers (and bools) numerically, strings by code unit,
 * lists lexicographically. Anything else — `None < 1`, `"a" < 1` — raises TypeError, as Python does. */
export function pyLt(a: unknown, b: unknown): boolean {
  if (isNumber(a) && isNumber(b)) return Number(a) < Number(b);
  if (typeof a === "string" && typeof b === "string") return a < b;
  if (Array.isArray(a) && Array.isArray(b)) {
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      if (!pyEq(a[i], b[i])) return pyLt(a[i], b[i]);
    }
    return a.length < b.length;
  }
  throw new PyTypeError(`'<' not supported between instances of '${pyTypeName(a)}' and '${pyTypeName(b)}'`);
}

/** Python's RuntimeError — the engine's "this cannot go on" error, named so in run logs. */
export class PyRuntimeError extends Error {
  override name = "RuntimeError";
}

/** `json.dumps(v)` with Python's default formatting: `", "` and `": "` separators, non-ASCII
 * escaped. Used where a payload is quoted into a message. */
export function pyJson(v: unknown): string {
  const esc = (s: string) => JSON.stringify(s).replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  const walk = (x: unknown): string => {
    if (x === null || x === undefined) return "null";
    if (typeof x === "boolean") return x ? "true" : "false";
    if (typeof x === "number") return Number.isFinite(x) ? pyFloatRepr(x) : Number.isNaN(x) ? "NaN" : x > 0 ? "Infinity" : "-Infinity";
    if (typeof x === "string") return esc(x);
    if (Array.isArray(x)) return `[${x.map(walk).join(", ")}]`;
    if (isDict(x)) return `{${Object.entries(x).map(([k, y]) => `${esc(k)}: ${walk(y)}`).join(", ")}}`;
    return esc(String(x));
  };
  return walk(v);
}

/** Python's AttributeError — reading something an object does not have. */
export class PyAttributeError extends Error {
  override name = "AttributeError";
}

/**
 * `obj` as Python would let you use it: reading an attribute or method it does not have RAISES
 * (AttributeError) instead of quietly answering `undefined`. The checks were written duck-typed —
 * `GAME.expected_payout(...)` on a game without it raises, and the runner reports the error — so a
 * port that carried on with `undefined` would turn a loud failure into a silent wrong answer.
 */
export function strict<T extends object>(obj: T): any {
  return new Proxy(obj, {
    get(target, prop, receiver) {
      if (typeof prop === "string" && !(prop in target)) {
        throw new PyAttributeError(`'${target.constructor.name}' object has no attribute '${prop}'`);
      }
      const v = Reflect.get(target, prop, receiver);
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
}

/** `list(v)`: a copy of a list, the characters of a string, the keys of a dict, the items of a set. */
export function pyList(v: unknown): unknown[] {
  if (Array.isArray(v)) return [...v];
  if (typeof v === "string") return [...v];
  if (v instanceof Set) return [...v];
  if (isDict(v)) return Object.keys(v);
  throw new PyTypeError(`'${pyTypeName(v)}' object is not iterable`);
}

/** `sorted(xs)` on numbers or strings. Mixed kinds raise, as Python's `<` does. */
export function pySorted<T>(xs: Iterable<T>): T[] {
  const arr = [...xs];
  const kinds = new Set(arr.map((x) => (typeof x === "boolean" ? "number" : typeof x)));
  if (kinds.size > 1) {
    const e = new PyTypeError("'<' not supported between instances of mixed types");
    throw e;
  }
  if (kinds.has("number")) return arr.sort((a, b) => Number(a) - Number(b));
  if (kinds.has("string")) return arr.sort((a, b) => (String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0));
  return arr;
}

/** `"...{i}...".format(i=…)` for the button templates. A missing field raises, like KeyError. */
export function pyFormat(tmpl: string, fmt: Record<string, unknown>): string {
  return tmpl.replace(/\{(\w+)\}/g, (_m, k: string) => {
    if (!(k in fmt)) throw new Error(`KeyError: '${k}'`);
    return pyStr(fmt[k]);
  });
}

// ── Containers ───────────────────────────────────────────────────────────────────────

/** Python `==` on JSON-shaped values: numbers and bools compare numerically (True == 1), lists and
 * dicts compare deeply, None only equals None. */
export function pyEq(a: unknown, b: unknown): boolean {
  if (a === undefined) a = null;
  if (b === undefined) b = null;
  if (isNumber(a) && isNumber(b)) return Number(a) === Number(b);
  if (a === null || b === null) return a === b;
  if (typeof a === "string" || typeof b === "string") return a === b;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((x, i) => pyEq(x, b[i]));
  }
  if (a instanceof Set && b instanceof Set) {
    return a.size === b.size && [...a].every((x) => b.has(x));
  }
  if (isDict(a) && isDict(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => has(b, k) && pyEq(a[k], b[k]));
  }
  return false;
}

/**
 * Builtin `sum(xs)` as CPython 3.12+ computes it for floats: Neumaier-compensated, so
 * `sum([0.1] * 10)` is exactly 1.0 (a plain loop gives 0.9999999999999999). Use this ONLY where the
 * Python called `sum()`; a Python `total += x` loop is plain addition and stays plain addition.
 */
export function sum(xs: Iterable<number | boolean>): number {
  let f = 0;
  let c = 0;
  for (const item of xs) {
    const x = Number(item);
    const t = f + x;
    if (Math.abs(f) >= Math.abs(x)) c += f - t + x;
    else c += x - t + f;
    f = t;
  }
  if (c && Number.isFinite(c)) f += c;
  return f;
}

// ── random.Random(seed) — CPython's Mersenne Twister, bit for bit ─────────────────────

/**
 * Python's `random.Random`, reproduced exactly for the calls the engine makes (`sample`,
 * `randrange`, `random`). A seeded language selection is printed in the run log so it can be pinned
 * later; that only works if the same seed picks the same languages after the port.
 */
export class PyRandom {
  private readonly mt = new Uint32Array(624);
  private mti = 625;

  constructor(seed: number | bigint) {
    this.seed(seed);
  }

  private initGenrand(s: number): void {
    this.mt[0] = s >>> 0;
    for (this.mti = 1; this.mti < 624; this.mti++) {
      const prev = this.mt[this.mti - 1] ^ (this.mt[this.mti - 1] >>> 30);
      this.mt[this.mti] = (Math.imul(1812433253, prev) + this.mti) >>> 0;
    }
  }

  private initByArray(key: number[]): void {
    this.initGenrand(19650218);
    let i = 1;
    let j = 0;
    for (let k = Math.max(624, key.length); k > 0; k--) {
      const prev = this.mt[i - 1] ^ (this.mt[i - 1] >>> 30);
      this.mt[i] = ((this.mt[i] ^ Math.imul(prev, 1664525)) + key[j] + j) >>> 0;
      i++;
      j++;
      if (i >= 624) {
        this.mt[0] = this.mt[623];
        i = 1;
      }
      if (j >= key.length) j = 0;
    }
    for (let k = 623; k > 0; k--) {
      const prev = this.mt[i - 1] ^ (this.mt[i - 1] >>> 30);
      this.mt[i] = ((this.mt[i] ^ Math.imul(prev, 1566083941)) - i) >>> 0;
      i++;
      if (i >= 624) {
        this.mt[0] = this.mt[623];
        i = 1;
      }
    }
    this.mt[0] = 0x80000000;
  }

  /** `random.seed(n)` for an int: the 32-bit words of |n|, least significant first. */
  seed(n: number | bigint): void {
    let x = BigInt(n);
    if (x < 0n) x = -x;
    const key: number[] = [];
    while (x > 0n) {
      key.push(Number(x & 0xffffffffn));
      x >>= 32n;
    }
    if (!key.length) key.push(0);
    this.initByArray(key);
  }

  private genrandUint32(): number {
    const mag01 = [0, 0x9908b0df];
    if (this.mti >= 624) {
      let kk = 0;
      for (; kk < 624 - 397; kk++) {
        const y = (this.mt[kk] & 0x80000000) | (this.mt[kk + 1] & 0x7fffffff);
        this.mt[kk] = this.mt[kk + 397] ^ (y >>> 1) ^ mag01[y & 1];
      }
      for (; kk < 623; kk++) {
        const y = (this.mt[kk] & 0x80000000) | (this.mt[kk + 1] & 0x7fffffff);
        this.mt[kk] = this.mt[kk + (397 - 624)] ^ (y >>> 1) ^ mag01[y & 1];
      }
      const y = (this.mt[623] & 0x80000000) | (this.mt[0] & 0x7fffffff);
      this.mt[623] = this.mt[396] ^ (y >>> 1) ^ mag01[y & 1];
      this.mti = 0;
    }
    let y = this.mt[this.mti++];
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }

  /** `random.random()` */
  random(): number {
    const a = this.genrandUint32() >>> 5;
    const b = this.genrandUint32() >>> 6;
    return (a * 67108864 + b) / 9007199254740992;
  }

  /** `getrandbits(k)` for k <= 32. */
  getrandbits(k: number): number {
    if (k <= 0) return 0;
    if (k > 32) throw new Error("getrandbits > 32 bits is not needed by the engine");
    return this.genrandUint32() >>> (32 - k);
  }

  /** `_randbelow(n)`: rejection sampling on `n.bit_length()` bits. */
  randbelow(n: number): number {
    if (n <= 0) return 0;
    const k = Math.floor(Math.log2(n)) + 1;
    let r = this.getrandbits(k);
    while (r >= n) r = this.getrandbits(k);
    return r;
  }

  /** `randrange(stop)` */
  randrange(stop: number): number {
    return this.randbelow(stop);
  }

  /** `sample(population, k)` — same algorithm and the same draws as CPython 3.11+. */
  sample<T>(population: readonly T[], k: number): T[] {
    const n = population.length;
    if (k < 0 || k > n) throw new PyValueError("Sample larger than population or is negative");
    const result: T[] = new Array(k);
    let setsize = 21;
    if (k > 5) setsize += 4 ** Math.ceil(Math.log(k * 3) / Math.log(4));
    if (n <= setsize) {
      const pool = [...population];
      for (let i = 0; i < k; i++) {
        const j = this.randbelow(n - i);
        result[i] = pool[j];
        pool[j] = pool[n - i - 1];
      }
    } else {
      const selected = new Set<number>();
      for (let i = 0; i < k; i++) {
        let j = this.randbelow(n);
        while (selected.has(j)) j = this.randbelow(n);
        selected.add(j);
        result[i] = population[j];
      }
    }
    return result;
  }
}
