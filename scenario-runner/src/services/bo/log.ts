/**
 * The `logging` the BO modules used, kept to what the runner actually saw.
 *
 * Nothing in the runner configures Python logging, so a BO `logger.info(...)` printed NOTHING and a
 * `logger.warning(...)` reached stderr as the bare message (Python's last-resort handler). That is
 * the default here too. `basicConfig(level)` reproduces `logging.basicConfig(level=...)`
 * (`LEVEL:logger.name:message` on stderr), and a sink sees every record (tests compare the text).
 */
import { pyPercent } from "./compat.js";

export const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 } as const;
export type LevelName = keyof typeof LEVELS;

export interface LogRecord {
  levelname: LevelName;
  name: string;
  message: string;
}

type Sink = (rec: LogRecord) => void;

const sinks: Sink[] = [];
let configuredLevel: number | null = null;

/** `logging.basicConfig(level=...)`. `null` goes back to the unconfigured default. */
export function basicConfig(level: LevelName | null): void {
  configuredLevel = level === null ? null : LEVELS[level];
}

/** Receive every record (any level). Returns the function that removes the sink. */
export function addLogSink(sink: Sink): () => void {
  sinks.push(sink);
  return () => {
    const i = sinks.indexOf(sink);
    if (i >= 0) sinks.splice(i, 1);
  };
}

export class Logger {
  constructor(readonly name: string) {}

  private log(levelname: LevelName, fmt: string, args: unknown[]): void {
    const levelno = LEVELS[levelname];
    const threshold = configuredLevel ?? LEVELS.WARNING;
    if (!sinks.length && levelno < threshold) return;
    const rec: LogRecord = { levelname, name: this.name, message: args.length ? pyPercent(fmt, args) : fmt };
    for (const s of sinks) s(rec);
    if (levelno < threshold) return;
    console.error(configuredLevel === null ? rec.message : `${levelname}:${this.name}:${rec.message}`);
  }

  info(fmt: string, ...args: unknown[]): void {
    this.log("INFO", fmt, args);
  }

  warning(fmt: string, ...args: unknown[]): void {
    this.log("WARNING", fmt, args);
  }
}

export function getLogger(name: string): Logger {
  return new Logger(name);
}
