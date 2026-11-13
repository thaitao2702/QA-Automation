/**
 * Python behaviour the BO layer depends on that `src/py.ts` does not already cover: `json.dumps`
 * with its options (the request body, the paytable string and the token cache are all written by
 * it), `json.loads` (which accepts `NaN` / `Infinity`), `str.strip()` / `str.split()` on Python's
 * whitespace set, `str.casefold()`, `d[k]` raising KeyError, and `%`-formatting for log lines.
 */
import { EOL } from "node:os";
import { isDict, pyFloatRepr, pyInt, pyRepr, pyRound, pyStr, PyTuple } from "../../py.js";

/** Python's KeyError — `d["data"]` on a reply without `data`. `str(KeyError('data'))` is `'data'`. */
export class KeyError extends Error {
  override name = "KeyError";

  constructor(key: unknown) {
    super(pyRepr(key));
  }
}

/** `d[k]`: the value, or KeyError when the key is missing (a stored JSON `null` is returned). */
export function item(d: unknown, key: string): any {
  if (!isDict(d) || !Object.prototype.hasOwnProperty.call(d, key)) throw new KeyError(key);
  const v = d[key];
  return v === undefined ? null : v;
}

/** `d.get(k, default)`: the default only when the key is MISSING — a stored `null` stays `null`. */
export function getOr<T>(d: unknown, key: string, dflt: T): any {
  if (!isDict(d) || !Object.prototype.hasOwnProperty.call(d, key)) return dflt;
  const v = d[key];
  return v === undefined ? null : v;
}

/** `str(exc)` — the message Python prints for an exception. */
export function excStr(e: unknown): string {
  if (e instanceof Error) return e.message;
  return pyStr(e);
}

/** `str(x)` for a Python FLOAT: `40.0`, not `40` (the value is known to be a float here). */
export function pyFloatStr(x: number): string {
  const s = pyFloatRepr(x);
  return /[.e]|inf|nan/.test(s) ? s : `${s}.0`;
}

// ── whitespace and case ──────────────────────────────────────────────────────────────

/** The characters `str.isspace()` is true for — what `strip()` / `split()` with no argument use. */
const PY_WS = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const STRIP_RE = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, "g");
const SPLIT_RE = new RegExp(`[${PY_WS}]+`);

/** `s.strip()`. */
export function pyStrip(s: string): string {
  return s.replace(STRIP_RE, "");
}

/** `s.split()` — runs of whitespace, no empty pieces. */
export function pySplitWs(s: string): string[] {
  return s.split(SPLIT_RE).filter((p) => p.length > 0);
}

/** Full case folds that differ from plain lowercasing (CaseFolding.txt, status F and the common S). */
const FOLD: Record<string, string> = {
  "ß": "ss", "ẞ": "ss", "µ": "μ", "ſ": "s", "ς": "σ",
  "ϐ": "β", "ϑ": "θ", "ϕ": "φ", "ϖ": "π", "ϰ": "κ",
  "ϱ": "ρ", "ϵ": "ε", "ẛ": "ṡ", "ι": "ι", "ŉ": "ʼn",
  "ǰ": "ǰ", "ΐ": "ΐ", "ΰ": "ΰ", "և": "եւ",
  "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
};

/** `s.casefold()`: lowercase per character (no final-sigma context), plus the full folds above. */
export function casefold(s: string): string {
  let out = "";
  for (const ch of s) out += FOLD[ch] ?? ch.toLowerCase();
  return out;
}

// ── json.dumps ───────────────────────────────────────────────────────────────────────

export interface DumpOptions {
  /** `separators=(item, key)`. Default `(", ", ": ")`, or `(",", ": ")` with an indent. */
  separators?: [string, string];
  indent?: number | null;
  /** default true, as in Python */
  ensureAscii?: boolean;
  /** default true, as in Python; false raises ValueError on NaN / Infinity */
  allowNan?: boolean;
}

function escapeString(s: string, ensureAscii: boolean): string {
  const j = JSON.stringify(s);
  if (!ensureAscii) return j;
  return j.replace(/[\u007f-￿]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

/** `json.dumps(v, ...)` with Python's formatting. A float prints by its shortest repr (an
 * integer-valued one without `.0` — see PORTING.md). */
export function jsonDumps(v: unknown, opts: DumpOptions = {}): string {
  const indent = opts.indent ?? null;
  const [itemSep, keySep] = opts.separators ?? (indent === null ? [", ", ": "] : [",", ": "]);
  const ensureAscii = opts.ensureAscii ?? true;
  const allowNan = opts.allowNan ?? true;
  const floatText = (x: number): string => {
    if (Number.isFinite(x)) return pyFloatRepr(x);
    if (!allowNan) {
      const e = new Error(`Out of range float values are not JSON compliant: ${pyFloatRepr(x)}`);
      e.name = "ValueError";
      throw e;
    }
    return Number.isNaN(x) ? "NaN" : x > 0 ? "Infinity" : "-Infinity";
  };
  const walk = (x: unknown, level: number): string => {
    if (x === null || x === undefined) return "null";
    if (x === true) return "true";
    if (x === false) return "false";
    if (typeof x === "number") return floatText(x);
    if (typeof x === "string") return escapeString(x, ensureAscii);
    const items: string[] = [];
    let open: string;
    let close: string;
    if (Array.isArray(x) || x instanceof PyTuple) {
      const arr = Array.isArray(x) ? x : x.items;
      if (!arr.length) return "[]";
      for (const y of arr) items.push(walk(y, level + 1));
      [open, close] = ["[", "]"];
    } else if (isDict(x)) {
      const entries = Object.entries(x);
      if (!entries.length) return "{}";
      for (const [k, y] of entries) items.push(`${escapeString(k, ensureAscii)}${keySep}${walk(y, level + 1)}`);
      [open, close] = ["{", "}"];
    } else {
      const e = new Error(`Object of type ${typeof x} is not JSON serializable`);
      e.name = "TypeError";
      throw e;
    }
    if (indent === null) return open + items.join(itemSep) + close;
    const pad = "\n" + " ".repeat(indent * (level + 1));
    return open + pad + items.join(itemSep + pad) + "\n" + " ".repeat(indent * level) + close;
  };
  return walk(v, 0);
}

/** Text written the way Python's text-mode `write_text` writes it: `\n` becomes the OS line end. */
export function textModeNewlines(s: string): string {
  return EOL === "\n" ? s : s.replace(/\n/g, EOL);
}

// ── json.loads ───────────────────────────────────────────────────────────────────────

/** Python's json.JSONDecodeError (the message text is JavaScript's, not CPython's). */
export class JSONDecodeError extends Error {
  override name = "JSONDecodeError";
}

/** `json.loads(text)`: JSON plus the bare `NaN`, `Infinity` and `-Infinity` Python also accepts. */
export function pyJsonLoads(text: string): any {
  try {
    return JSON.parse(text);
  } catch (first) {
    // Retry with the non-standard constants swapped for marker strings, outside string literals.
    const mark = `\u0000py-const-${Math.random().toString(36).slice(2)}:`;
    let out = "";
    let swapped = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === '"') {
        let j = i + 1;
        while (j < text.length && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
        out += text.slice(i, j + 1);
        i = j;
        continue;
      }
      const rest = text.slice(i);
      const m = /^(-?Infinity|NaN)/.exec(rest);
      if (m) {
        out += JSON.stringify(mark + m[1]);
        i += m[1].length - 1;
        swapped = true;
        continue;
      }
      out += ch;
    }
    if (swapped) {
      try {
        return JSON.parse(out, (_k, v) => {
          if (typeof v === "string" && v.startsWith(mark)) {
            const c = v.slice(mark.length);
            return c === "NaN" ? NaN : c === "Infinity" ? Infinity : -Infinity;
          }
          return v;
        });
      } catch {
        // fall through to the original error
      }
    }
    throw new JSONDecodeError((first as Error).message);
  }
}

/** `json.loads(bytes)`: the encoding detected the way `json.detect_encoding` does. */
export function pyJsonLoadsBytes(b: Uint8Array): any {
  let enc: string;
  const starts = (...xs: number[]) => xs.every((x, i) => b[i] === x);
  if (starts(0x00, 0x00, 0xfe, 0xff)) enc = "utf-32be";
  else if (starts(0xff, 0xfe, 0x00, 0x00)) enc = "utf-32le";
  else if (starts(0xfe, 0xff)) enc = "utf-16be";
  else if (starts(0xff, 0xfe)) enc = "utf-16le";
  else if (starts(0xef, 0xbb, 0xbf)) enc = "utf-8-sig";
  else if (b.length >= 4) {
    if (!b[0]) enc = b[1] ? "utf-16be" : "utf-32be";
    else if (!b[1]) enc = b[2] || b[3] ? "utf-16le" : "utf-32le";
    else enc = "utf-8";
  } else if (b.length === 2) {
    enc = !b[0] ? "utf-16be" : !b[1] ? "utf-16le" : "utf-8";
  } else enc = "utf-8";
  let text: string;
  if (enc.startsWith("utf-32")) {
    const le = enc === "utf-32le";
    if (b.length % 4) throw new JSONDecodeError("truncated data");
    let start = 0;
    const cp = (i: number) => (le ? (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) : ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3])) >>> 0;
    if (b.length >= 4 && cp(0) === 0xfeff) start = 4;
    text = "";
    for (let i = start; i < b.length; i += 4) text += String.fromCodePoint(cp(i));
  } else {
    const label = enc === "utf-8-sig" ? "utf-8" : enc;
    text = new TextDecoder(label, { fatal: true, ignoreBOM: false }).decode(b);
  }
  return pyJsonLoads(text);
}

// ── %-formatting (log lines) ─────────────────────────────────────────────────────────

/** `fmt % args` for the conversions the BO log lines use: `%s`, `%r`, `%d`, `%.Nf`, `%%`. */
export function pyPercent(fmt: string, args: readonly unknown[]): string {
  let i = 0;
  return fmt.replace(/%(%|s|r|d|\.(\d+)f)/g, (_m, conv: string, prec: string | undefined) => {
    if (conv === "%") return "%";
    const a = args[i++];
    if (conv === "s") return pyStr(a);
    if (conv === "r") return pyRepr(a);
    if (conv === "d") return String(pyInt(a));
    const n = Number(prec);
    const x = typeof a === "boolean" ? Number(a) : (a as number);
    if (!Number.isFinite(x)) return Number.isNaN(x) ? "nan" : x > 0 ? "inf" : "-inf";
    return pyRound(x, n).toFixed(n);
  });
}
