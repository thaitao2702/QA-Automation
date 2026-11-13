/**
 * Python semantics the AI pass needs beyond `src/py.ts`: `json.loads` / `JSONDecoder.raw_decode`
 * (CPython's C scanner — the error TEXT reaches the report as "AI error: Response was not valid
 * JSON: ..."), `str.strip()`, full `repr()` of strings (non-printables escaped), `f"{x:.3f}"`
 * (half-to-even on the exact double), `float()` of a string (Unicode digits accepted), code-point
 * slicing (`s[:200]`), and the `logging` lastResort handler the Python ran under.
 *
 * Everything here works on CODE POINTS, as a Python `str` does, not on UTF-16 units.
 */
import { isDict, pyFloat, pyFloatRepr, pyRound, pyTypeName, type Dict } from "../../py.js";

export type { Dict };

// ── Errors with Python names ─────────────────────────────────────────────────────────

/** An error whose `name` is the Python exception type and whose `message` is `str(exc)`. */
export function pyError(type: string, message: string): Error {
  const e = new Error(message);
  e.name = type;
  return e;
}

/** `d[k]` on a missing key: KeyError, whose `str()` is the repr of the key. */
export function keyError(k: unknown): Error {
  return pyError("KeyError", reprOf(k));
}

/** `str(exc)` for anything a port catches. */
export function excStr(e: unknown): string {
  if (e instanceof Error) return e.message;
  return strOf(e);
}

/** `type(exc).__name__`. */
export function excType(e: unknown): string {
  return e instanceof Error ? e.name : "Exception";
}

/** `d[k]` — the value, or KeyError when `d` lacks the key. */
export function need(d: Dict, k: string): any {
  if (!Object.prototype.hasOwnProperty.call(d, k)) throw keyError(k);
  return d[k];
}

/** `x.get(k, default)` on a value that should be a dict: AttributeError if it is not. */
export function dget(x: unknown, k: string, dflt: unknown = null): any {
  if (!isDict(x)) throw pyError("AttributeError", `'${pyTypeName(x)}' object has no attribute 'get'`);
  return Object.prototype.hasOwnProperty.call(x, k) ? x[k] : dflt;
}

/** `a + b` where `b` is a str: TypeError unless `a` is a str too. */
export function strAdd(a: unknown, b: unknown): string {
  if (typeof a === "string" && typeof b === "string") return a + b;
  if (typeof a === "string") throw pyError("TypeError", `can only concatenate str (not "${pyTypeName(b)}") to str`);
  if (Array.isArray(a)) throw pyError("TypeError", `can only concatenate list (not "${pyTypeName(b)}") to list`);
  throw pyError("TypeError", `unsupported operand type(s) for +: '${pyTypeName(a)}' and '${pyTypeName(b)}'`);
}

/** `dict(v)`: a shallow copy of a dict, or a dict built from a sequence of pairs. */
export function pyDict(v: unknown): Dict {
  if (isDict(v)) return { ...v };
  if (typeof v === "string" || Array.isArray(v)) {
    const out: Dict = {};
    Array.from(v as ArrayLike<unknown>).forEach((el, i) => {
      const seq = typeof el === "string" ? Array.from(el) : Array.isArray(el) ? el : null;
      if (seq === null) throw pyError("TypeError", `cannot convert dictionary update sequence element #${i} to a sequence`);
      if (seq.length !== 2) throw pyError("ValueError", `dictionary update sequence element #${i} has length ${seq.length}; 2 is required`);
      out[strOf(seq[0])] = seq[1];
    });
    return out;
  }
  throw pyError("TypeError", `'${pyTypeName(v)}' object is not iterable`);
}

/** `float(v)` for any value: a str is read the way CPython reads it (see `pyFloatStr`). */
export function pyFloatAny(v: unknown): number {
  return typeof v === "string" ? pyFloatStr(v) : pyFloat(v);
}

/** `a, b = v`. */
export function unpack2(v: unknown): [unknown, unknown] {
  if (!Array.isArray(v) && typeof v !== "string") {
    throw pyError("TypeError", `cannot unpack non-iterable ${pyTypeName(v)} object`);
  }
  const items = Array.from(v as ArrayLike<unknown>);
  if (items.length > 2) throw pyError("ValueError", "too many values to unpack (expected 2)");
  if (items.length < 2) throw pyError("ValueError", `not enough values to unpack (expected 2, got ${items.length})`);
  return [items[0], items[1]];
}

// ── Strings ──────────────────────────────────────────────────────────────────────────

/** `ch.isspace()` for one code point (CPython's Py_UNICODE_ISSPACE). */
export function isPySpace(cp: number): boolean {
  return (cp >= 0x09 && cp <= 0x0d) || (cp >= 0x1c && cp <= 0x20) || cp === 0x85 || cp === 0xa0
    || cp === 0x1680 || (cp >= 0x2000 && cp <= 0x200a) || cp === 0x2028 || cp === 0x2029
    || cp === 0x202f || cp === 0x205f || cp === 0x3000;
}

/** `s.strip()` / `s.strip(chars)`. */
export function pyStrip(s: string, chars?: string): string {
  const cps = Array.from(s);
  const strip = chars === undefined
    ? (c: string) => isPySpace(c.codePointAt(0)!)
    : (c: string) => chars.includes(c);
  let a = 0;
  let b = cps.length;
  while (a < b && strip(cps[a])) a++;
  while (b > a && strip(cps[b - 1])) b--;
  return cps.slice(a, b).join("");
}

/** `s[a:b]` on code points (negative indices count from the end, as in Python). */
export function cpSlice(s: string, start?: number, end?: number): string {
  const cps = Array.from(s);
  const n = cps.length;
  const norm = (i: number | undefined, dflt: number) => {
    if (i === undefined) return dflt;
    if (i < 0) return Math.max(0, n + i);
    return Math.min(i, n);
  };
  return cps.slice(norm(start, 0), norm(end, n)).join("");
}

/** `s.splitlines()`. */
export function pySplitlines(s: string): string[] {
  const out: string[] = [];
  let cur = "";
  const cps = Array.from(s);
  for (let i = 0; i < cps.length; i++) {
    const c = cps[i];
    if (c === "\r" && cps[i + 1] === "\n") {
      out.push(cur);
      cur = "";
      i++;
      continue;
    }
    if ("\n\r\v\f\x1c\x1d\x1e\x85\u{2028}\u{2029}".includes(c)) {
      out.push(cur);
      cur = "";
      continue;
    }
    cur += c;
  }
  if (cur) out.push(cur);
  return out;
}

const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

/** `repr(s)` for a str: CPython's quote choice and escapes (`\xa0`, zero-width spaces, ...). */
export function reprStr(s: string): string {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = q;
  for (const ch of s) {
    const c = ch.codePointAt(0)!;
    if (ch === "\\") out += "\\\\";
    else if (ch === q) out += `\\${q}`;
    else if (ch === "\t") out += "\\t";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (c === 0x20 || !NON_PRINTABLE.test(ch)) out += ch;
    else if (c < 0x100) out += `\\x${c.toString(16).padStart(2, "0")}`;
    else if (c < 0x10000) out += `\\u${c.toString(16).padStart(4, "0")}`;
    else out += `\\U${c.toString(16).padStart(8, "0")}`;
  }
  return out + q;
}

/** `repr(v)` for JSON-shaped values. */
export function reprOf(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "number") return pyFloatRepr(v);
  if (typeof v === "string") return reprStr(v);
  if (Array.isArray(v)) return `[${v.map(reprOf).join(", ")}]`;
  if (v instanceof Set) return v.size ? `{${[...v].map(reprOf).join(", ")}}` : "set()";
  if (isDict(v)) return `{${Object.entries(v).map(([k, x]) => `${reprStr(k)}: ${reprOf(x)}`).join(", ")}}`;
  return String(v);
}

/** `str(v)`. */
export function strOf(v: unknown): string {
  return typeof v === "string" ? v : reprOf(v);
}

// ── Numbers ──────────────────────────────────────────────────────────────────────────

/** `f"{x:.{n}f}"`: half-to-even on the double's exact value; `-0.000` keeps its sign. */
export function pyFixed(x: number, n: number): string {
  if (Number.isNaN(x)) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  const neg = x < 0 || Object.is(x, -0);
  const r = Math.abs(pyRound(x, n));
  let s: string;
  if (r >= 1e21) {
    s = BigInt(Math.round(r)).toString() + (n > 0 ? `.${"0".repeat(n)}` : "");
  } else {
    s = r.toFixed(n);
  }
  return (neg ? "-" : "") + s;
}

/** Python `max(a, b)` for floats: `b` only if `b > a` (so `max(0.0, nan)` is 0.0). */
export function pyMax(a: number, b: number): number {
  return b > a ? b : a;
}

/** `math.ceil(x)` (raises on nan / inf, as Python does). */
export function pyCeil(x: number): number {
  if (Number.isNaN(x)) throw pyError("ValueError", "cannot convert float NaN to integer");
  if (!Number.isFinite(x)) throw pyError("OverflowError", "cannot convert float infinity to integer");
  return Math.ceil(x) || 0;
}

/** `a // b` for ints. */
export function floorDiv(a: number, b: number): number {
  if (b === 0) throw pyError("ZeroDivisionError", "integer division or modulo by zero");
  return Math.floor(a / b);
}

/** The value of a Unicode decimal digit (`\p{Nd}`). Unicode encodes them in contiguous ascending
 * 0-9 blocks (the mathematical digits are five such blocks back to back), so the value is the
 * number of digits before this one in its run, mod 10. */
function decimalValue(cp: number): number {
  let run = 0;
  while (cp - run - 1 >= 0 && /\p{Nd}/u.test(String.fromCodePoint(cp - run - 1))) run++;
  return run % 10;
}

/**
 * `float(s)` for a str exactly as CPython reads it: code points below 127 kept, Unicode whitespace
 * read as a space, Unicode decimal digits (Thai, Arabic-Indic, fullwidth...) read as ASCII digits,
 * anything else invalid. Raises ValueError like Python.
 */
export function pyFloatStr(s: string): number {
  let t = "";
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (cp < 127) t += ch;
    else if (isPySpace(cp)) t += " ";
    else if (/\p{Nd}/u.test(ch)) t += String(decimalValue(cp));
    else throw pyError("ValueError", `could not convert string to float: ${reprStr(s)}`);
  }
  try {
    return pyFloat(t);
  } catch {
    throw pyError("ValueError", `could not convert string to float: ${reprStr(s)}`);
  }
}

// ── json.loads / JSONDecoder.raw_decode (CPython 3.13 C scanner) ─────────────────────

export class JSONDecodeError extends Error {
  override name = "JSONDecodeError";
  constructor(readonly msg: string, doc: readonly string[], readonly pos: number) {
    let lineno = 1;
    let last = -1;
    for (let i = 0; i < pos && i < doc.length; i++) {
      if (doc[i] === "\n") {
        lineno++;
        last = i;
      }
    }
    super(`${msg}: line ${lineno} column ${pos - last} (char ${pos})`);
  }
}

class StopIteration {
  constructor(readonly value: number) {}
}

const isWs = (c: string | undefined) => c === " " || c === "\t" || c === "\n" || c === "\r";
const isDigit = (c: string | undefined) => c !== undefined && c >= "0" && c <= "9";

function setKey(obj: Dict, key: string, val: unknown): void {
  if (key === "__proto__") Object.defineProperty(obj, key, { value: val, writable: true, enumerable: true, configurable: true });
  else obj[key] = val;
}

/** A JSON scanner over code points, error positions as CPython reports them. */
class Scanner {
  readonly len: number;
  constructor(readonly s: readonly string[]) {
    this.len = s.length;
  }

  scanString(end: number): [string, number] {
    const s = this.s;
    const len = this.len;
    const begin = end - 1;
    let out = "";
    for (;;) {
      let next = end;
      let c = "";
      for (; next < len; next++) {
        c = s[next];
        if (c === '"' || c === "\\") break;
        if (c.codePointAt(0)! <= 0x1f) throw new JSONDecodeError("Invalid control character at", s, next);
      }
      if (next >= len) throw new JSONDecodeError("Unterminated string starting at", s, begin);
      out += s.slice(end, next).join("");
      next++;
      if (c === '"') return [out, next];
      if (next === len) throw new JSONDecodeError("Unterminated string starting at", s, begin);
      c = s[next];
      if (c !== "u") {
        end = next + 1;
        const map: Record<string, string> = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (!(c in map)) throw new JSONDecodeError("Invalid \\escape", s, end - 2);
        out += map[c];
        continue;
      }
      next++;
      end = next + 4;
      if (end >= len) throw new JSONDecodeError("Invalid \\uXXXX escape", s, next - 1);
      let u = 0;
      for (; next < end; next++) {
        const h = s[next];
        if (!/^[0-9a-fA-F]$/.test(h)) throw new JSONDecodeError("Invalid \\uXXXX escape", s, end - 5);
        u = (u << 4) | parseInt(h, 16);
      }
      if (u >= 0xd800 && u <= 0xdbff && end + 6 < len && s[next] === "\\" && s[next + 1] === "u") {
        next += 2;
        let u2 = 0;
        end += 6;
        for (; next < end; next++) {
          const h = s[next];
          if (!/^[0-9a-fA-F]$/.test(h)) throw new JSONDecodeError("Invalid \\uXXXX escape", s, end - 5);
          u2 = (u2 << 4) | parseInt(h, 16);
        }
        if (u2 >= 0xdc00 && u2 <= 0xdfff) u = 0x10000 + (((u - 0xd800) << 10) | (u2 - 0xdc00));
        else end -= 6;
      }
      out += String.fromCodePoint(u);
    }
  }

  scanOnce(idx: number): [unknown, number] {
    const s = this.s;
    const len = this.len;
    if (idx >= len) throw new StopIteration(idx);
    const at = (i: number, word: string) => s.slice(i, i + word.length).join("") === word;
    switch (s[idx]) {
      case '"':
        return this.scanString(idx + 1);
      case "{":
        return this.parseObject(idx + 1);
      case "[":
        return this.parseArray(idx + 1);
      case "n":
        if (at(idx, "null")) return [null, idx + 4];
        break;
      case "t":
        if (at(idx, "true")) return [true, idx + 4];
        break;
      case "f":
        if (at(idx, "false")) return [false, idx + 5];
        break;
      case "N":
        if (at(idx, "NaN")) return [NaN, idx + 3];
        break;
      case "I":
        if (at(idx, "Infinity")) return [Infinity, idx + 8];
        break;
      case "-":
        if (at(idx, "-Infinity")) return [-Infinity, idx + 9];
        break;
      default:
        break;
    }
    return this.matchNumber(idx);
  }

  matchNumber(start: number): [unknown, number] {
    const s = this.s;
    const endIdx = this.len - 1;
    let idx = start;
    if (s[idx] === "-") {
      idx++;
      if (idx > endIdx) throw new StopIteration(start);
    }
    if (s[idx] >= "1" && s[idx] <= "9" && s[idx].length === 1) {
      idx++;
      while (idx <= endIdx && isDigit(s[idx])) idx++;
    } else if (s[idx] === "0") {
      idx++;
    } else {
      throw new StopIteration(start);
    }
    let isFloat = false;
    if (idx < endIdx && s[idx] === "." && isDigit(s[idx + 1])) {
      isFloat = true;
      idx += 2;
      while (idx <= endIdx && isDigit(s[idx])) idx++;
    }
    if (idx < endIdx && (s[idx] === "e" || s[idx] === "E")) {
      const eStart = idx;
      idx++;
      if (idx < endIdx && (s[idx] === "-" || s[idx] === "+")) idx++;
      while (idx <= endIdx && isDigit(s[idx])) idx++;
      if (isDigit(s[idx - 1])) isFloat = true;
      else idx = eStart;
    }
    const text = s.slice(start, idx).join("");
    const v = Number(text);
    return [isFloat ? v : v === 0 ? 0 : v, idx];
  }

  parseObject(idx: number): [unknown, number] {
    const s = this.s;
    const endIdx = this.len - 1;
    const obj: Dict = {};
    while (idx <= endIdx && isWs(s[idx])) idx++;
    if (idx > endIdx || s[idx] !== "}") {
      for (;;) {
        if (idx > endIdx || s[idx] !== '"') {
          throw new JSONDecodeError("Expecting property name enclosed in double quotes", s, idx);
        }
        const [key, afterKey] = this.scanString(idx + 1);
        idx = afterKey;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        if (idx > endIdx || s[idx] !== ":") throw new JSONDecodeError("Expecting ':' delimiter", s, idx);
        idx++;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        const [val, afterVal] = this.scanOnce(idx);
        setKey(obj, key, val);
        idx = afterVal;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        if (idx <= endIdx && s[idx] === "}") break;
        if (idx > endIdx || s[idx] !== ",") throw new JSONDecodeError("Expecting ',' delimiter", s, idx);
        const comma = idx;
        idx++;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        if (idx <= endIdx && s[idx] === "}") {
          throw new JSONDecodeError("Illegal trailing comma before end of object", s, comma);
        }
      }
    }
    return [obj, idx + 1];
  }

  parseArray(idx: number): [unknown, number] {
    const s = this.s;
    const endIdx = this.len - 1;
    const arr: unknown[] = [];
    while (idx <= endIdx && isWs(s[idx])) idx++;
    if (idx > endIdx || s[idx] !== "]") {
      for (;;) {
        const [val, afterVal] = this.scanOnce(idx);
        arr.push(val);
        idx = afterVal;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        if (idx <= endIdx && s[idx] === "]") break;
        if (idx > endIdx || s[idx] !== ",") throw new JSONDecodeError("Expecting ',' delimiter", s, idx);
        const comma = idx;
        idx++;
        while (idx <= endIdx && isWs(s[idx])) idx++;
        if (idx <= endIdx && s[idx] === "]") {
          throw new JSONDecodeError("Illegal trailing comma before end of array", s, comma);
        }
      }
    }
    return [arr, idx + 1];
  }

  /** `JSONDecoder().raw_decode(s, idx)`. */
  rawDecode(idx: number): [unknown, number] {
    try {
      return this.scanOnce(idx);
    } catch (e) {
      if (e instanceof StopIteration) throw new JSONDecodeError("Expecting value", this.s, e.value);
      throw e;
    }
  }
}

/** A decoder bound to one document (code points), for repeated `raw_decode` calls. */
export class JSONDocument {
  readonly cps: string[];
  private readonly scanner: Scanner;
  constructor(s: string) {
    this.cps = Array.from(s);
    this.scanner = new Scanner(this.cps);
  }
  rawDecode(idx: number): [unknown, number] {
    return this.scanner.rawDecode(idx);
  }
}

/** `json.loads(s)`. Raises JSONDecodeError with CPython's message. */
export function jsonLoads(s: string): unknown {
  const doc = new JSONDocument(s);
  const cps = doc.cps;
  if (cps[0] === "\u{feff}") throw new JSONDecodeError("Unexpected UTF-8 BOM (decode using utf-8-sig)", cps, 0);
  let idx = 0;
  while (idx < cps.length && isWs(cps[idx])) idx++;
  const [obj, end0] = doc.rawDecode(idx);
  let end = end0;
  while (end < cps.length && isWs(cps[end])) end++;
  if (end !== cps.length) throw new JSONDecodeError("Extra data", cps, end);
  return obj;
}

// ── logging (the Python ran with no logging config: WARNING+ to stderr, bare message) ─

/** `"%s ... %r" % args` for log lines. */
export function pyPercent(fmt: string, args: unknown[]): string {
  let i = 0;
  return fmt.replace(/%([srd%])/g, (_m, c: string) => {
    if (c === "%") return "%";
    const v = args[i++];
    if (c === "r") return reprOf(v);
    if (c === "d") return typeof v === "number" ? String(Math.trunc(v)) : strOf(v);
    return strOf(v);
  });
}

export const logger = {
  warning(fmt: string, ...args: unknown[]): void {
    process.stderr.write(`${pyPercent(fmt, args)}\n`);
  },
  error(fmt: string, ...args: unknown[]): void {
    process.stderr.write(`${pyPercent(fmt, args)}\n`);
  },
  /** INFO / DEBUG were below the lastResort handler's WARNING level: never printed. */
  info(_fmt: string, ..._args: unknown[]): void {},
  debug(_fmt: string, ..._args: unknown[]): void {},
};

// ── paths (pathlib) ──────────────────────────────────────────────────────────────────

const WIN = process.platform === "win32";

/** `Path(a) / b` as a string: separators normalised, `.` and empty parts dropped, `..` KEPT. */
export function pathJoin(a: string, ...parts: string[]): string {
  let out = a;
  for (const p of parts) {
    const isAbs = WIN ? /^([a-zA-Z]:)?[\\/]/.test(p) || /^[a-zA-Z]:/.test(p) : p.startsWith("/");
    out = isAbs ? p : `${out}${WIN ? "\\" : "/"}${p}`;
  }
  return pathNorm(out);
}

/** `str(Path(p))`. */
export function pathNorm(p: string): string {
  if (!WIN) {
    const abs = p.startsWith("/");
    const segs = p.split("/").filter((x) => x !== "" && x !== ".");
    const body = segs.join("/");
    return abs ? (p.startsWith("//") && !p.startsWith("///") ? `//${body}` : `/${body}`) : body || ".";
  }
  const m = /^([a-zA-Z]:)?([\\/])?(.*)$/s.exec(p)!;
  const drive = m[1] ?? "";
  const root = m[2] ? "\\" : "";
  const segs = (m[3] ?? "").split(/[\\/]/).filter((x) => x !== "" && x !== ".");
  const body = segs.join("\\");
  const out = drive + root + body;
  return out || ".";
}

/** `Path(p).stem`. */
export function pathStem(p: string): string {
  const name = pathName(p);
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(0, dot) : name;
}

/** `Path(p).name`. */
export function pathName(p: string): string {
  const n = pathNorm(p);
  const segs = n.split(WIN ? /[\\/]/ : /\//);
  return segs[segs.length - 1] ?? "";
}

/** `Path(p).suffix`. */
export function pathSuffix(p: string): string {
  const name = pathName(p);
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot) : "";
}

/** `Path(p).parent`. */
export function pathParent(p: string): string {
  const n = pathNorm(p);
  const sep = WIN ? "\\" : "/";
  const i = n.lastIndexOf(sep);
  if (i < 0) {
    if (WIN && /^[a-zA-Z]:/.test(n)) return n.slice(0, 2);
    return ".";
  }
  if (i === 0) return sep;
  if (WIN && i === 2 && /^[a-zA-Z]:\\/.test(n)) return n.slice(0, 3);
  return n.slice(0, i);
}

/** OSError text as Python prints it: `[Errno 2] No such file or directory: 'p'`. */
export function osErrorText(e: unknown, path: string): string {
  const code = (e as NodeJS.ErrnoException)?.code;
  if (code === "ENOENT") return `[Errno 2] No such file or directory: ${reprStr(path)}`;
  if (code === "EACCES" || code === "EPERM") return `[Errno 13] Permission denied: ${reprStr(path)}`;
  if (code === "EISDIR") return `[Errno 21] Is a directory: ${reprStr(path)}`;
  return excStr(e);
}
