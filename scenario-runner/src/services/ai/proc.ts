/**
 * `subprocess.run` / `shutil.which` / `time.sleep` for the AI helpers (ffmpeg, ffprobe, the
 * `claude` CLI). Kept in one module so the baseline test can stand in for the processes.
 *
 * Errors carry Python's type names and text, because the helpers fold `str(exc)` into a result's
 * `reason`: `TimeoutExpired` -> "Command '[...]' timed out after 600 seconds", a missing executable
 * -> FileNotFoundError "[WinError 2] The system cannot find the file specified".
 */
import { spawn } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { reprOf, strOf } from "./py-support.js";

const WIN = process.platform === "win32";

export interface CompletedProcess {
  returncode: number;
  stdout: string;
  stderr: string;
}

export interface RunOptions {
  cwd?: string;
  /** Written to stdin, then stdin is closed (`input=`). */
  input?: string;
  /** Seconds (`timeout=`). The child is killed and TimeoutExpired raised. */
  timeout?: number;
  env?: NodeJS.ProcessEnv;
}

/** `subprocess.TimeoutExpired`. */
export class TimeoutExpired extends Error {
  override name = "TimeoutExpired";
  constructor(readonly cmd: string[], readonly timeout: number) {
    super(`Command '${reprOf(cmd)}' timed out after ${strOf(timeout)} seconds`);
  }
}

/** FileNotFoundError from launching a program that does not exist. */
export class FileNotFoundError extends Error {
  override name = "FileNotFoundError";
  constructor(program: string) {
    super(WIN ? "[WinError 2] The system cannot find the file specified"
      : `[Errno 2] No such file or directory: ${reprOf(program)}`);
  }
}

/** `subprocess.list2cmdline` — the command line Python hands to CreateProcess. */
export function list2cmdline(seq: string[]): string {
  const result: string[] = [];
  for (const arg of seq) {
    let bsBuf: string[] = [];
    if (result.length) result.push(" ");
    const needquote = arg.includes(" ") || arg.includes("\t") || !arg;
    if (needquote) result.push('"');
    for (const c of arg) {
      if (c === "\\") {
        bsBuf.push(c);
      } else if (c === '"') {
        result.push("\\".repeat(bsBuf.length * 2));
        bsBuf = [];
        result.push('\\"');
      } else {
        if (bsBuf.length) {
          result.push(...bsBuf);
          bsBuf = [];
        }
        result.push(c);
      }
    }
    if (bsBuf.length) result.push(...bsBuf);
    if (needquote) {
      result.push(...bsBuf);
      result.push('"');
    }
  }
  return result.join("");
}

/** Text mode's universal newlines: `\r\n` and `\r` read as `\n`. */
function universalNewlines(s: string): string {
  return s.replace(/\r\n?/g, "\n");
}

/**
 * `subprocess.run(cmd, capture_output=True, text=True, ...)`. Resolves with the exit code and the
 * decoded output; rejects with FileNotFoundError / TimeoutExpired like Python.
 *
 * ⚠ A `.cmd` / `.bat` (the npm `claude.cmd` shim) cannot be spawned directly by Node (CVE-2024-27980
 * hardening); Python's CreateProcess ran it through cmd.exe implicitly. We do the same explicitly
 * with the command line Python would have built (`list2cmdline`).
 */
export function runProcess(cmd: string[], opts: RunOptions = {}): Promise<CompletedProcess> {
  return new Promise((resolve, reject) => {
    const [file, ...args] = cmd;
    const viaShell = WIN && /\.(cmd|bat)$/i.test(file);
    let child;
    try {
      child = viaShell
        ? spawn(list2cmdline(cmd), { cwd: opts.cwd, env: opts.env, shell: true, windowsHide: true })
        : spawn(file, args, { cwd: opts.cwd, env: opts.env, windowsHide: true });
    } catch (e) {
      reject((e as NodeJS.ErrnoException).code === "ENOENT" ? new FileNotFoundError(file) : e);
      return;
    }
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let timedOut = false;
    let settled = false;
    const timer = opts.timeout !== undefined
      ? setTimeout(() => {
        timedOut = true;
        child.kill();
      }, opts.timeout * 1000)
      : null;
    child.stdout?.on("data", (b: Buffer) => out.push(b));
    child.stderr?.on("data", (b: Buffer) => err.push(b));
    child.on("error", (e: NodeJS.ErrnoException) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      reject(e.code === "ENOENT" ? new FileNotFoundError(file) : e);
    });
    child.on("close", (code: number | null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (timedOut) {
        reject(new TimeoutExpired(cmd, opts.timeout!));
        return;
      }
      resolve({
        returncode: code ?? -1,
        stdout: universalNewlines(Buffer.concat(out).toString("utf-8")),
        stderr: universalNewlines(Buffer.concat(err).toString("utf-8")),
      });
    });
    child.stdin?.on("error", () => {});       // the child may exit before reading its input
    if (opts.input !== undefined) child.stdin?.end(opts.input, "utf-8");
    else child.stdin?.end();
  });
}

/** `time.sleep(seconds)`. */
export function sleep(seconds: number): Promise<void> {
  return new Promise((r) => setTimeout(r, seconds * 1000));
}

// ── shutil.which (CPython 3.13) ──────────────────────────────────────────────────────

const WIN_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC";

/** `ntpath.join(a, b)` / `posixpath.join(a, b)` without normalising (what `which` returns). */
function osJoin(dir: string, name: string): string {
  if (!WIN) return dir === "" || dir.endsWith("/") ? dir + name : `${dir}/${name}`;
  if (dir === "" || /[\\/]$/.test(dir) || /^[a-zA-Z]:$/.test(dir)) return dir + name;
  return `${dir}\\${name}`;
}

function accessCheck(fn: string): boolean {
  try {
    return existsSync(fn) && !statSync(fn).isDirectory();
  } catch {
    return false;
  }
}

/** `shutil.which(cmd)` — PATH (cwd first on Windows), PATHEXT, first match as Python spells it. */
export function which(cmd: string, env: NodeJS.ProcessEnv = process.env): string | null {
  // os.path.split: the head keeps its root ("C:\\", "/"), loses trailing separators otherwise
  const sep = WIN ? /[\\/]/ : /\//;
  const drive = WIN && /^[a-zA-Z]:/.test(cmd) ? cmd.slice(0, 2) : "";
  const rest = cmd.slice(drive.length);
  let lastSep = -1;
  [...rest].forEach((c, i) => {
    if (sep.test(c)) lastSep = i;
  });
  const base = rest.slice(lastSep + 1);
  let head = rest.slice(0, lastSep + 1);
  const root = sep.test(head[0] ?? "") ? head[0] : "";
  head = head.slice(root.length).replace(WIN ? /[\\/]+$/ : /\/+$/, "");
  const dirname = drive + root + head;
  let path: string[];
  if (dirname) {
    path = [dirname];
  } else {
    const raw = env.PATH ?? (WIN ? undefined : "/bin:/usr/bin");
    if (!raw) return null;
    path = raw.split(WIN ? ";" : ":");
    if (WIN && !env.NoDefaultCurrentDirectoryInExePath) path.unshift(".");
  }
  let files: string[];
  if (WIN) {
    const pathext = (env.PATHEXT || WIN_DEFAULT_PATHEXT).split(";").filter((e) => e).map((e) => e.replace(/\.+$/, ""));
    files = pathext.map((ext) => base + ext);
    const norm = base.toUpperCase();
    if (pathext.some((ext) => norm.endsWith(ext.toUpperCase()))) files.unshift(base);
  } else {
    files = [base];
  }
  const seen = new Set<string>();
  for (const dir of path) {
    const normdir = WIN ? dir.replace(/\//g, "\\").toLowerCase() : dir;
    if (seen.has(normdir)) continue;
    seen.add(normdir);
    for (const f of files) {
      const name = osJoin(dir, f);
      if (accessCheck(name)) return name;
    }
  }
  return null;
}
