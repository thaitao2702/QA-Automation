/**
 * A safe evaluator for the inline `stream:` predicates — the expression part of Python, nothing more.
 *
 * The Python runner interpolated a predicate (`{facts.spin_balance} < {facts.chosen_bet}`) and handed
 * it to `eval` with empty builtins. This evaluates the same text with the same answers, without
 * running code: literals, names, `and`/`or`/`not`, chained comparisons, `in`/`is`, arithmetic,
 * subscripts, conditional expressions and the whitelisted calls (`abs`, `min`, `max`, `round`, `len`,
 * `sum`, `int`, `float`, `sorted`, `all`, `any`, `set`).
 *
 * Python's value model is kept where a predicate could tell: an int is a `bigint` (so a 19-digit
 * round id compares exactly), a float a `number`, `True + 1 == 2`, `1 == 1.0`, `[1] != (1,)`, and
 * the error messages are CPython 3.13's — they reach the violation text.
 */
import { pyFloat, pyFloatRepr, pyRound, pyStrRepr, PyTuple } from "../py.js";

export class PyEvalError extends Error {
  constructor(readonly kind: string, message: string) {
    super(message);
    this.name = kind;
  }
}

const err = (kind: string, msg: string) => new PyEvalError(kind, msg);
const typeErr = (msg: string) => err("TypeError", msg);

// ── values ───────────────────────────────────────────────────────────────────────────

class PyDict {
  constructor(readonly entries: Array<[Val, Val]>) {}
}

class PySetVal {
  constructor(readonly items: Val[]) {}
}

class PyFunc {
  constructor(readonly name: string, readonly fn: (args: Val[]) => Val) {}
}

class TupleVal {
  constructor(readonly items: Val[]) {}
}

type Val = null | boolean | bigint | number | string | Val[] | TupleVal | PyDict | PySetVal | PyFunc;

function typeName(v: Val): string {
  if (v === null) return "NoneType";
  if (typeof v === "boolean") return "bool";
  if (typeof v === "bigint") return "int";
  if (typeof v === "number") return "float";
  if (typeof v === "string") return "str";
  if (Array.isArray(v)) return "list";
  if (v instanceof TupleVal) return "tuple";
  if (v instanceof PyDict) return "dict";
  if (v instanceof PySetVal) return "set";
  return "builtin_function_or_method";
}

const isNum = (v: Val): v is boolean | bigint | number =>
  typeof v === "boolean" || typeof v === "bigint" || typeof v === "number";

/** bool -> int, as Python treats it in arithmetic. */
const asNum = (v: boolean | bigint | number): bigint | number => (typeof v === "boolean" ? (v ? 1n : 0n) : v);

function toFloat(v: bigint | number): number {
  return typeof v === "bigint" ? Number(v) : v;
}

export function truthy(v: Val): boolean {
  if (v === null || v === false) return false;
  if (typeof v === "bigint") return v !== 0n;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (v instanceof TupleVal || v instanceof PySetVal) return v.items.length > 0;
  if (v instanceof PyDict) return v.entries.length > 0;
  return true;
}

function repr(v: Val): string {
  if (v === null) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "number") return Number.isInteger(v) && Math.abs(v) < 1e16 ? `${v}.0` : pyFloatRepr(v);
  if (typeof v === "string") return pyStrRepr(v);
  if (Array.isArray(v)) return `[${v.map(repr).join(", ")}]`;
  if (v instanceof TupleVal) return v.items.length === 1 ? `(${repr(v.items[0])},)` : `(${v.items.map(repr).join(", ")})`;
  if (v instanceof PyDict) return `{${v.entries.map(([k, x]) => `${repr(k)}: ${repr(x)}`).join(", ")}}`;
  if (v instanceof PySetVal) return v.items.length ? `{${v.items.map(repr).join(", ")}}` : "set()";
  return `<built-in function ${v.name}>`;
}

/** Python `==`. */
function eq(a: Val, b: Val): boolean {
  if (isNum(a) && isNum(b)) {
    const x = asNum(a);
    const y = asNum(b);
    if (typeof x === "bigint" && typeof y === "bigint") return x === y;
    if (typeof x === "number" && typeof y === "number") return x === y;
    const [i, f] = typeof x === "bigint" ? [x, y as number] : [y as bigint, x as number];
    return Number.isInteger(f) ? i === BigInt(f) : false;
  }
  if (a === null || b === null) return a === b;
  if (typeof a === "string" || typeof b === "string") return a === b;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => eq(x, b[i]));
  if (a instanceof TupleVal && b instanceof TupleVal) {
    return a.items.length === b.items.length && a.items.every((x, i) => eq(x, b.items[i]));
  }
  if (a instanceof PySetVal && b instanceof PySetVal) {
    return a.items.length === b.items.length && a.items.every((x) => b.items.some((y) => eq(x, y)));
  }
  if (a instanceof PyDict && b instanceof PyDict) {
    return a.entries.length === b.entries.length
      && a.entries.every(([k, x]) => b.entries.some(([k2, y]) => eq(k, k2) && eq(x, y)));
  }
  return a === b;
}

/**
 * Python's `<`, `>`, `<=`, `>=`. Lists and tuples compare at the first differing element with the
 * SAME operator, sets by inclusion; mismatched kinds raise naming the operands in WRITTEN order
 * (`'none' > 0` -> "between instances of 'str' and 'int'"), as CPython does.
 */
function order(op: string, a: Val, b: Val): boolean {
  const cmp = (x: number | string | bigint, y: number | string | bigint): boolean => {
    switch (op) {
      case "<": return x < y;
      case ">": return x > y;
      case "<=": return x <= y;
      default: return x >= y;
    }
  };
  if (isNum(a) && isNum(b)) {
    const x = asNum(a);
    const y = asNum(b);
    if (typeof x === "bigint" && typeof y === "bigint") return cmp(x, y);
    return cmp(toFloat(x), toFloat(y));
  }
  if (typeof a === "string" && typeof b === "string") return cmp(a, b);
  const seqA = Array.isArray(a) ? a : a instanceof TupleVal ? a.items : null;
  const seqB = Array.isArray(b) ? b : b instanceof TupleVal ? b.items : null;
  if (seqA && seqB && Array.isArray(a) === Array.isArray(b)) {
    for (let i = 0; i < Math.min(seqA.length, seqB.length); i++) {
      if (!eq(seqA[i], seqB[i])) return order(op, seqA[i], seqB[i]);
    }
    return cmp(seqA.length, seqB.length);
  }
  if (a instanceof PySetVal && b instanceof PySetVal) {
    const sub = (x: PySetVal, y: PySetVal) => x.items.every((i) => y.items.some((j) => eq(i, j)));
    switch (op) {
      case "<": return sub(a, b) && a.items.length < b.items.length;
      case "<=": return sub(a, b);
      case ">": return sub(b, a) && b.items.length < a.items.length;
      default: return sub(b, a);
    }
  }
  throw typeErr(`'${op}' not supported between instances of '${typeName(a)}' and '${typeName(b)}'`);
}

function compare(op: string, a: Val, b: Val): boolean {
  switch (op) {
    case "==": return eq(a, b);
    case "!=": return !eq(a, b);
    case "<": case ">": case "<=": case ">=": return order(op, a, b);
    case "in": return contains(b, a);
    case "not in": return !contains(b, a);
    case "is": return identical(a, b);
    case "is not": return !identical(a, b);
    default: throw err("SyntaxError", "invalid syntax (<string>, line 1)");
  }
}

function identical(a: Val, b: Val): boolean {
  if (a === null || b === null || typeof a === "boolean" || typeof b === "boolean") return a === b;
  if (typeof a === "bigint" && typeof b === "bigint") return a === b && a >= -5n && a <= 256n;
  return a === b;
}

function contains(container: Val, item: Val): boolean {
  if (typeof container === "string") {
    if (typeof item !== "string") throw typeErr(`'in <string>' requires string as left operand, not ${typeName(item)}`);
    return container.includes(item);
  }
  if (Array.isArray(container)) return container.some((x) => eq(x, item));
  if (container instanceof TupleVal || container instanceof PySetVal) return container.items.some((x) => eq(x, item));
  if (container instanceof PyDict) return container.entries.some(([k]) => eq(k, item));
  throw typeErr(`argument of type '${typeName(container)}' is not iterable`);
}

function iterate(v: Val, what = ""): Val[] {
  if (Array.isArray(v)) return [...v];
  if (typeof v === "string") return [...v];
  if (v instanceof TupleVal || v instanceof PySetVal) return [...v.items];
  if (v instanceof PyDict) return v.entries.map(([k]) => k);
  throw typeErr(`${what}'${typeName(v)}' object is not iterable`);
}

// ── arithmetic ───────────────────────────────────────────────────────────────────────

function unsupported(op: string, a: Val, b: Val): PyEvalError {
  return typeErr(`unsupported operand type(s) for ${op}: '${typeName(a)}' and '${typeName(b)}'`);
}

function floorDivBig(a: bigint, b: bigint): bigint {
  const q = a / b;
  return (a % b !== 0n && (a < 0n) !== (b < 0n)) ? q - 1n : q;
}

function binop(op: string, a: Val, b: Val): Val {
  if (isNum(a) && isNum(b)) {
    const x = asNum(a);
    const y = asNum(b);
    const ints = typeof x === "bigint" && typeof y === "bigint";
    switch (op) {
      case "+": return ints ? (x as bigint) + (y as bigint) : toFloat(x) + toFloat(y);
      case "-": return ints ? (x as bigint) - (y as bigint) : toFloat(x) - toFloat(y);
      case "*": return ints ? (x as bigint) * (y as bigint) : toFloat(x) * toFloat(y);
      case "/": {
        if (toFloat(y) === 0) throw err("ZeroDivisionError", ints ? "division by zero" : "float division by zero");
        return toFloat(x) / toFloat(y);
      }
      case "//": {
        if (toFloat(y) === 0) {
          throw err("ZeroDivisionError", ints ? "integer division or modulo by zero" : "float floor division by zero");
        }
        return ints ? floorDivBig(x as bigint, y as bigint) : Math.floor(toFloat(x) / toFloat(y));
      }
      case "%": {
        if (toFloat(y) === 0) throw err("ZeroDivisionError", ints ? "integer modulo by zero" : "float modulo");
        if (ints) {
          const r = (x as bigint) % (y as bigint);
          return r !== 0n && (r < 0n) !== ((y as bigint) < 0n) ? r + (y as bigint) : r;
        }
        const fx = toFloat(x);
        const fy = toFloat(y);
        const r = fx % fy;
        return r !== 0 && (r < 0) !== (fy < 0) ? r + fy : r === 0 ? (fy < 0 ? -0 : 0) : r;
      }
      case "**": {
        if (ints && (y as bigint) >= 0n) return (x as bigint) ** (y as bigint);
        return toFloat(x) ** toFloat(y);
      }
      default: throw unsupported(op, a, b);
    }
  }
  if (op === "+") {
    if (typeof a === "string") {
      if (typeof b === "string") return a + b;
      throw typeErr(`can only concatenate str (not "${typeName(b)}") to str`);
    }
    if (Array.isArray(a)) {
      if (Array.isArray(b)) return [...a, ...b];
      throw typeErr(`can only concatenate list (not "${typeName(b)}") to list`);
    }
    if (a instanceof TupleVal) {
      if (b instanceof TupleVal) return new TupleVal([...a.items, ...b.items]);
      throw typeErr(`can only concatenate tuple (not "${typeName(b)}") to tuple`);
    }
  }
  if (op === "*") {
    const seq = typeof a === "string" || Array.isArray(a) || a instanceof TupleVal ? a : typeof b === "string" || Array.isArray(b) || b instanceof TupleVal ? b : null;
    const n = seq === a ? b : a;
    if (seq !== null) {
      if (typeof n !== "bigint" && typeof n !== "boolean") throw typeErr(`can't multiply sequence by non-int of type '${typeName(n)}'`);
      const k = Number(asNum(n));
      if (typeof seq === "string") return k > 0 ? seq.repeat(k) : "";
      const items = Array.isArray(seq) ? seq : (seq as TupleVal).items;
      const out: Val[] = [];
      for (let i = 0; i < k; i++) out.push(...items);
      return Array.isArray(seq) ? out : new TupleVal(out);
    }
  }
  if (op === "%" && typeof a === "string") throw typeErr("not all arguments converted during string formatting");
  if (op === "-" && a instanceof PySetVal && b instanceof PySetVal) {
    return new PySetVal(a.items.filter((x) => !b.items.some((y) => eq(x, y))));
  }
  throw unsupported(op, a, b);
}

function unary(op: string, v: Val): Val {
  if (op === "not") return !truthy(v);
  if (isNum(v)) {
    const x = asNum(v);
    if (op === "-") return typeof x === "bigint" ? -x : -x;
    if (op === "+") return x;
    if (op === "~") {
      if (typeof x === "bigint") return ~x;
      throw typeErr("bad operand type for unary ~: 'float'");
    }
  }
  throw typeErr(`bad operand type for unary ${op}: '${typeName(v)}'`);
}

// ── builtins ─────────────────────────────────────────────────────────────────────────

function argsOrIterable(name: string, args: Val[]): Val[] {
  if (!args.length) throw typeErr(`${name} expected at least 1 argument, got 0`);
  return args.length === 1 ? iterate(args[0]) : args;
}

function toPyInt(v: Val): Val {
  if (typeof v === "boolean") return v ? 1n : 0n;
  if (typeof v === "bigint") return v;
  if (typeof v === "number") {
    if (Number.isNaN(v)) throw err("ValueError", "cannot convert float NaN to integer");
    if (!Number.isFinite(v)) throw err("OverflowError", "cannot convert float infinity to integer");
    return BigInt(Math.trunc(v));
  }
  if (typeof v === "string") {
    const s = v.trim();
    if (!/^[+-]?\d(?:_?\d)*$/.test(s)) throw err("ValueError", `invalid literal for int() with base 10: ${pyStrRepr(v)}`);
    return BigInt(s.replace(/_/g, ""));
  }
  throw typeErr(`int() argument must be a string, a bytes-like object or a real number, not '${typeName(v)}'`);
}

function toPyFloat(v: Val): Val {
  if (typeof v === "boolean" || typeof v === "bigint") return Number(asNum(v));
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    try {
      return pyFloat(v);
    } catch {
      throw err("ValueError", `could not convert string to float: ${pyStrRepr(v)}`);
    }
  }
  throw typeErr(`float() argument must be a string or a real number, not '${typeName(v)}'`);
}

function pySum(items: Val[]): Val {
  let acc: Val = 0n;
  let f = 0;
  let c = 0;
  let floating = false;
  for (const item of items) {
    if (!floating && (typeof acc === "bigint") && (typeof item === "bigint" || typeof item === "boolean")) {
      acc = acc + (asNum(item) as bigint);
      continue;
    }
    if (typeof item === "number" && (typeof acc === "bigint" || floating)) {
      if (!floating) {
        f = Number(acc);
        floating = true;
      }
      const t = f + item;
      if (Math.abs(f) >= Math.abs(item)) c += f - t + item;
      else c += item - t + f;
      f = t;
      continue;
    }
    if (floating && (typeof item === "bigint" || typeof item === "boolean")) {
      const x = Number(asNum(item));
      const t = f + x;
      if (Math.abs(f) >= Math.abs(x)) c += f - t + x;
      else c += x - t + f;
      f = t;
      continue;
    }
    const left: Val = floating ? f + (c && Number.isFinite(c) ? c : 0) : acc;
    acc = binop("+", left, item);
    floating = false;
  }
  if (floating) return c && Number.isFinite(c) ? f + c : f;
  return acc;
}

function sortedVals(items: Val[]): Val[] {
  return [...items].sort((a, b) => (order("<", a, b) ? -1 : order("<", b, a) ? 1 : 0));
}

const BUILTINS: Record<string, PyFunc> = {
  abs: new PyFunc("abs", ([x]) => {
    if (isNum(x)) {
      const n = asNum(x);
      return typeof n === "bigint" ? (n < 0n ? -n : n) : Math.abs(n);
    }
    throw typeErr(`bad operand type for abs(): '${typeName(x)}'`);
  }),
  min: new PyFunc("min", (args) => {
    const xs = argsOrIterable("min", args);
    if (!xs.length) throw err("ValueError", "min() iterable argument is empty");
    return xs.reduce((m, x) => (order("<", x, m) ? x : m));
  }),
  max: new PyFunc("max", (args) => {
    const xs = argsOrIterable("max", args);
    if (!xs.length) throw err("ValueError", "max() iterable argument is empty");
    return xs.reduce((m, x) => (order(">", x, m) ? x : m));
  }),
  round: new PyFunc("round", ([x, nd]) => {
    if (isNum(x)) {
      const n = asNum(x);
      if (nd === undefined || nd === null) return typeof n === "bigint" ? n : BigInt(pyRound(n));
      const d = Number(asNum(nd as bigint));
      return typeof n === "bigint" ? (d >= 0 ? n : BigInt(pyRound(Number(n), d))) : pyRound(n, d);
    }
    throw typeErr(`type ${typeName(x)} doesn't define __round__ method`);
  }),
  len: new PyFunc("len", ([x]) => {
    if (typeof x === "string") return BigInt([...x].length);
    if (Array.isArray(x)) return BigInt(x.length);
    if (x instanceof TupleVal || x instanceof PySetVal) return BigInt(x.items.length);
    if (x instanceof PyDict) return BigInt(x.entries.length);
    throw typeErr(`object of type '${typeName(x)}' has no len()`);
  }),
  sum: new PyFunc("sum", ([xs]) => pySum(iterate(xs))),
  int: new PyFunc("int", ([x]) => (x === undefined ? 0n : toPyInt(x))),
  float: new PyFunc("float", ([x]) => (x === undefined ? 0 : toPyFloat(x))),
  sorted: new PyFunc("sorted", ([xs]) => sortedVals(iterate(xs))),
  all: new PyFunc("all", ([xs]) => iterate(xs).every(truthy)),
  any: new PyFunc("any", ([xs]) => iterate(xs).some(truthy)),
  set: new PyFunc("set", ([xs]) => {
    const out: Val[] = [];
    for (const x of xs === undefined ? [] : iterate(xs)) if (!out.some((y) => eq(x, y))) out.push(x);
    return new PySetVal(out);
  }),
};

// ── tokens ───────────────────────────────────────────────────────────────────────────

type Tok =
  | { t: "num"; v: bigint | number }
  | { t: "str"; v: string }
  | { t: "name"; v: string }
  | { t: "op"; v: string }
  | { t: "end" };

const SYNTAX = () => err("SyntaxError", "invalid syntax (<string>, line 1)");

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  const ops3 = ["**=", "//=", "...", ">>=", "<<="];
  const ops2 = ["==", "!=", "<=", ">=", "**", "//", "<<", ">>", "->", "+=", "-=", "*=", "/="];
  while (i < src.length) {
    const ch = src[i];
    if (/\s/.test(ch)) {
      i++;
      continue;
    }
    const num = /^(?:\d(?:_?\d)*)?(?:\.\d(?:_?\d)*|\.)?(?:[eE][+-]?\d(?:_?\d)*)?/.exec(src.slice(i));
    if (num && num[0] && /\d/.test(num[0]) && (/\d/.test(ch) || (ch === "." && /\d/.test(src[i + 1] ?? "")))) {
      const text = num[0].replace(/_/g, "");
      i += num[0].length;
      if (/[A-Za-z_]/.test(src[i] ?? "")) throw err("SyntaxError", "invalid decimal literal (<string>, line 1)");
      out.push({ t: "num", v: /[.eE]/.test(text) ? Number(text) : BigInt(text) });
      continue;
    }
    if (ch === "'" || ch === '"') {
      const q = ch;
      let j = i + 1;
      let s = "";
      while (j < src.length && src[j] !== q) {
        if (src[j] === "\\" && j + 1 < src.length) {
          const e = src[j + 1];
          const map: Record<string, string> = { n: "\n", t: "\t", r: "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0" };
          if (e in map) {
            s += map[e];
            j += 2;
            continue;
          }
          if (e === "x" && /^[0-9a-fA-F]{2}$/.test(src.slice(j + 2, j + 4))) {
            s += String.fromCharCode(parseInt(src.slice(j + 2, j + 4), 16));
            j += 4;
            continue;
          }
          s += "\\" + e;
          j += 2;
          continue;
        }
        s += src[j];
        j++;
      }
      if (j >= src.length) throw err("SyntaxError", "unterminated string literal (detected at line 1) (<string>, line 1)");
      out.push({ t: "str", v: s });
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      const m = /^[A-Za-z_][A-Za-z_0-9]*/.exec(src.slice(i))!;
      out.push({ t: "name", v: m[0] });
      i += m[0].length;
      continue;
    }
    const three = src.slice(i, i + 3);
    const two = src.slice(i, i + 2);
    if (ops3.includes(three)) {
      out.push({ t: "op", v: three });
      i += 3;
    } else if (ops2.includes(two)) {
      out.push({ t: "op", v: two });
      i += 2;
    } else if ("+-*/%()[]{},:.<>~&|^=@!".includes(ch)) {
      out.push({ t: "op", v: ch });
      i += 1;
    } else {
      throw err("SyntaxError", `invalid character '${ch}' (U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}) (<string>, line 1)`);
    }
  }
  out.push({ t: "end" });
  return out;
}

// ── parser (AST) ─────────────────────────────────────────────────────────────────────

type Node =
  | { k: "lit"; v: Val }
  | { k: "name"; v: string }
  | { k: "list" | "tuple" | "set"; items: Node[] }
  | { k: "dict"; items: Array<[Node, Node]> }
  | { k: "bin"; op: string; a: Node; b: Node }
  | { k: "un"; op: string; a: Node }
  | { k: "bool"; op: "and" | "or"; items: Node[] }
  | { k: "cmp"; first: Node; rest: Array<[string, Node]> }
  | { k: "if"; test: Node; then: Node; else: Node }
  | { k: "call"; fn: Node; args: Node[] }
  | { k: "sub"; obj: Node; idx: Node }
  | { k: "attr"; obj: Node; name: string };

const KEYWORDS = new Set(["and", "or", "not", "in", "is", "if", "else", "lambda", "for", "None", "True", "False",
  "import", "from", "def", "class", "return", "yield", "await", "async", "while", "with", "del", "pass", "raise",
  "try", "except", "finally", "global", "nonlocal", "assert", "break", "continue", "elif", "as"]);

class Parser {
  private i = 0;

  constructor(private readonly toks: Tok[]) {}

  private peek(off = 0): Tok {
    return this.toks[Math.min(this.i + off, this.toks.length - 1)];
  }

  private isOp(v: string, off = 0): boolean {
    const t = this.peek(off);
    return t.t === "op" && t.v === v;
  }

  private isKw(v: string, off = 0): boolean {
    const t = this.peek(off);
    return t.t === "name" && t.v === v;
  }

  private take(): Tok {
    return this.toks[this.i++];
  }

  private expectOp(v: string): void {
    if (!this.isOp(v)) throw SYNTAX();
    this.i++;
  }

  parseTop(): Node {
    const n = this.exprList(true);
    if (this.peek().t !== "end") throw SYNTAX();
    return n;
  }

  /** `a, b` at the top level or in parentheses is a tuple. */
  private exprList(top: boolean): Node {
    const first = this.expr();
    if (!this.isOp(",")) return first;
    const items = [first];
    while (this.isOp(",")) {
      this.i++;
      if (this.peek().t === "end" || this.isOp(")")) break;
      items.push(this.expr());
    }
    void top;
    return { k: "tuple", items };
  }

  private expr(): Node {
    if (this.isKw("lambda")) throw SYNTAX();
    const test = this.orTest();
    if (this.isKw("if")) {
      this.i++;
      const cond = this.orTest();
      if (!this.isKw("else")) throw SYNTAX();
      this.i++;
      return { k: "if", test: cond, then: test, else: this.expr() };
    }
    return test;
  }

  private orTest(): Node {
    const items = [this.andTest()];
    while (this.isKw("or")) {
      this.i++;
      items.push(this.andTest());
    }
    return items.length === 1 ? items[0] : { k: "bool", op: "or", items };
  }

  private andTest(): Node {
    const items = [this.notTest()];
    while (this.isKw("and")) {
      this.i++;
      items.push(this.notTest());
    }
    return items.length === 1 ? items[0] : { k: "bool", op: "and", items };
  }

  private notTest(): Node {
    if (this.isKw("not")) {
      this.i++;
      return { k: "un", op: "not", a: this.notTest() };
    }
    return this.comparison();
  }

  private compOp(): string | null {
    const t = this.peek();
    if (t.t === "op" && ["<", ">", "==", ">=", "<=", "!="].includes(t.v)) {
      this.i++;
      return t.v;
    }
    if (this.isKw("in")) {
      this.i++;
      return "in";
    }
    if (this.isKw("not") && this.isKw("in", 1)) {
      this.i += 2;
      return "not in";
    }
    if (this.isKw("is")) {
      this.i++;
      if (this.isKw("not")) {
        this.i++;
        return "is not";
      }
      return "is";
    }
    return null;
  }

  private comparison(): Node {
    const first = this.bitOr();
    const rest: Array<[string, Node]> = [];
    for (let op = this.compOp(); op !== null; op = this.compOp()) rest.push([op, this.bitOr()]);
    return rest.length ? { k: "cmp", first, rest } : first;
  }

  private bitOr(): Node {
    let a = this.bitXor();
    while (this.isOp("|")) {
      this.i++;
      a = { k: "bin", op: "|", a, b: this.bitXor() };
    }
    return a;
  }

  private bitXor(): Node {
    let a = this.bitAnd();
    while (this.isOp("^")) {
      this.i++;
      a = { k: "bin", op: "^", a, b: this.bitAnd() };
    }
    return a;
  }

  private bitAnd(): Node {
    let a = this.shift();
    while (this.isOp("&")) {
      this.i++;
      a = { k: "bin", op: "&", a, b: this.shift() };
    }
    return a;
  }

  private shift(): Node {
    let a = this.arith();
    while (this.isOp("<<") || this.isOp(">>")) {
      const op = (this.take() as { v: string }).v;
      a = { k: "bin", op, a, b: this.arith() };
    }
    return a;
  }

  private arith(): Node {
    let a = this.term();
    while (this.isOp("+") || this.isOp("-")) {
      const op = (this.take() as { v: string }).v;
      a = { k: "bin", op, a, b: this.term() };
    }
    return a;
  }

  private term(): Node {
    let a = this.factor();
    while (this.isOp("*") || this.isOp("/") || this.isOp("//") || this.isOp("%") || this.isOp("@")) {
      const op = (this.take() as { v: string }).v;
      a = { k: "bin", op, a, b: this.factor() };
    }
    return a;
  }

  private factor(): Node {
    if (this.isOp("-") || this.isOp("+") || this.isOp("~")) {
      const op = (this.take() as { v: string }).v;
      return { k: "un", op, a: this.factor() };
    }
    return this.power();
  }

  private power(): Node {
    const base = this.primary();
    if (this.isOp("**")) {
      this.i++;
      return { k: "bin", op: "**", a: base, b: this.factor() };
    }
    return base;
  }

  private primary(): Node {
    let n = this.atom();
    for (;;) {
      if (this.isOp("(")) {
        this.i++;
        const args: Node[] = [];
        while (!this.isOp(")")) {
          args.push(this.expr());
          if (this.isOp(",")) this.i++;
          else if (!this.isOp(")")) throw err("SyntaxError", "invalid syntax. Perhaps you forgot a comma? (<string>, line 1)");
        }
        this.i++;
        n = { k: "call", fn: n, args };
      } else if (this.isOp("[")) {
        this.i++;
        const idx = this.exprList(false);
        this.expectOp("]");
        n = { k: "sub", obj: n, idx };
      } else if (this.isOp(".")) {
        this.i++;
        const t = this.take();
        if (t.t !== "name") throw SYNTAX();
        n = { k: "attr", obj: n, name: t.v };
      } else {
        return n;
      }
    }
  }

  private seqItems(close: string): Node[] {
    const items: Node[] = [];
    while (!this.isOp(close)) {
      if (this.peek().t === "end") throw err("SyntaxError", `'${close === "]" ? "[" : close === ")" ? "(" : "{"}' was never closed (<string>, line 1)`);
      items.push(this.expr());
      if (this.isOp(",")) this.i++;
      else if (!this.isOp(close)) {
        if (this.peek().t === "end") {
          throw err("SyntaxError", `'${close === "]" ? "[" : close === ")" ? "(" : "{"}' was never closed (<string>, line 1)`);
        }
        throw err("SyntaxError", "invalid syntax. Perhaps you forgot a comma? (<string>, line 1)");
      }
    }
    this.i++;
    return items;
  }

  private atom(): Node {
    const t = this.take();
    if (t.t === "num") return { k: "lit", v: t.v };
    if (t.t === "str") {
      let s = t.v;
      while (this.peek().t === "str") s += (this.take() as { v: string }).v;
      return { k: "lit", v: s };
    }
    if (t.t === "name") {
      if (t.v === "None") return { k: "lit", v: null };
      if (t.v === "True") return { k: "lit", v: true };
      if (t.v === "False") return { k: "lit", v: false };
      if (KEYWORDS.has(t.v)) throw SYNTAX();
      return { k: "name", v: t.v };
    }
    if (t.t === "op") {
      if (t.v === "(") {
        if (this.isOp(")")) {
          this.i++;
          return { k: "tuple", items: [] };
        }
        const inner = this.exprList(false);
        if (!this.isOp(")")) {
          if (this.peek().t === "end") throw err("SyntaxError", "'(' was never closed (<string>, line 1)");
          throw err("SyntaxError", "invalid syntax. Perhaps you forgot a comma? (<string>, line 1)");
        }
        this.i++;
        return inner;
      }
      if (t.v === "[") return { k: "list", items: this.seqItems("]") };
      if (t.v === "{") {
        if (this.isOp("}")) {
          this.i++;
          return { k: "dict", items: [] };
        }
        const first = this.expr();
        if (this.isOp(":")) {
          this.i++;
          const items: Array<[Node, Node]> = [[first, this.expr()]];
          while (this.isOp(",")) {
            this.i++;
            if (this.isOp("}")) break;
            const k = this.expr();
            this.expectOp(":");
            items.push([k, this.expr()]);
          }
          this.expectOp("}");
          return { k: "dict", items };
        }
        const items = [first];
        while (this.isOp(",")) {
          this.i++;
          if (this.isOp("}")) break;
          items.push(this.expr());
        }
        if (!this.isOp("}")) {
          if (this.peek().t === "end") throw err("SyntaxError", "'{' was never closed (<string>, line 1)");
          throw err("SyntaxError", "invalid syntax. Perhaps you forgot a comma? (<string>, line 1)");
        }
        this.i++;
        return { k: "set", items };
      }
    }
    throw SYNTAX();
  }
}

// ── evaluation ───────────────────────────────────────────────────────────────────────

function evalNode(n: Node): Val {
  switch (n.k) {
    case "lit": return n.v;
    case "name": {
      const f = BUILTINS[n.v];
      if (f) return f;
      throw err("NameError", `name '${n.v}' is not defined`);
    }
    case "list": return n.items.map(evalNode);
    case "tuple": return new TupleVal(n.items.map(evalNode));
    case "set": {
      const out: Val[] = [];
      for (const x of n.items.map(evalNode)) if (!out.some((y) => eq(x, y))) out.push(x);
      return new PySetVal(out);
    }
    case "dict": {
      const out: Array<[Val, Val]> = [];
      for (const [kn, vn] of n.items) {
        const k = evalNode(kn);
        const v = evalNode(vn);
        const at = out.findIndex(([k2]) => eq(k, k2));
        if (at >= 0) out[at] = [out[at][0], v];
        else out.push([k, v]);
      }
      return new PyDict(out);
    }
    case "bin": return binop(n.op, evalNode(n.a), evalNode(n.b));
    case "un": return unary(n.op, evalNode(n.a));
    case "bool": {
      let v: Val = null;
      for (const item of n.items) {
        v = evalNode(item);
        if (n.op === "or" ? truthy(v) : !truthy(v)) return v;
      }
      return v;
    }
    case "cmp": {
      let left = evalNode(n.first);
      for (const [op, rn] of n.rest) {
        const right = evalNode(rn);
        if (!compare(op, left, right)) return false;
        left = right;
      }
      return true;
    }
    case "if": return truthy(evalNode(n.test)) ? evalNode(n.then) : evalNode(n.else);
    case "call": {
      const fn = evalNode(n.fn);
      const args = n.args.map(evalNode);
      if (!(fn instanceof PyFunc)) throw typeErr(`'${typeName(fn)}' object is not callable`);
      return fn.fn(args);
    }
    case "sub": {
      const obj = evalNode(n.obj);
      const idx = evalNode(n.idx);
      if (obj instanceof PyDict) {
        const hit = obj.entries.find(([k]) => eq(k, idx));
        if (!hit) throw err("KeyError", repr(idx));
        return hit[1];
      }
      const seq = typeof obj === "string" ? [...obj] : Array.isArray(obj) ? obj : obj instanceof TupleVal ? obj.items : null;
      if (seq === null) throw typeErr(`'${typeName(obj)}' object is not subscriptable`);
      if (typeof idx !== "bigint" && typeof idx !== "boolean") {
        if (typeof obj === "string") throw typeErr(`string indices must be integers, not '${typeName(idx)}'`);
        throw typeErr(`${typeName(obj)} indices must be integers or slices, not ${typeName(idx)}`);
      }
      let k = Number(asNum(idx));
      if (k < 0) k += seq.length;
      if (k < 0 || k >= seq.length) throw err("IndexError", `${typeof obj === "string" ? "string" : typeName(obj)} index out of range`);
      return seq[k];
    }
    case "attr": {
      const obj = evalNode(n.obj);
      throw err("AttributeError", `'${typeName(obj)}' object has no attribute '${n.name}'`);
    }
  }
}

/** The answer `eval(src)` would give, converted for the caller: JSON-ish values, tuples as PyTuple. */
function toPlain(v: Val): unknown {
  if (typeof v === "bigint") return Number(v);
  if (Array.isArray(v)) return v.map(toPlain);
  if (v instanceof TupleVal) return new PyTuple(v.items.map(toPlain));
  if (v instanceof PySetVal) return new Set(v.items.map(toPlain));
  if (v instanceof PyDict) return Object.fromEntries(v.entries.map(([k, x]) => [String(toPlain(k)), toPlain(x)]));
  if (v instanceof PyFunc) return v.name;
  return v;
}

export interface EvalOutcome {
  /** `bool(eval(src))` */
  truth: boolean;
  /** `repr(eval(src))`, for tests and diagnostics */
  repr: string;
  value: unknown;
}

/** Evaluate `src` like `eval(src, {"__builtins__": {}}, PREDICATE_NAMES)`. Throws PyEvalError with
 * CPython's exception name and message on failure. */
export function pyEval(src: string): EvalOutcome {
  const v = evalNode(new Parser(tokenize(src)).parseTop());
  return { truth: truthy(v), repr: repr(v), value: toPlain(v) };
}
