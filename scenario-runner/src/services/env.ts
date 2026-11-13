/**
 * Per-environment configuration (port of framework/env_files.py).
 *
 * Loads the shared base `.env`, then the `.env.{suffix}` overlay that overrides it, where the suffix
 * maps the runner's env name to the file name:
 *
 *     dev -> .env.dev    staging -> .env.stg    prod -> .env.prod    local -> .env.local
 *
 * Game config lives per environment in `games/{game}/env.config.{suffix}.yml`.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";

/** scenario-runner/src/services/env.ts -> the repo root. */
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

export const ENV_FILE_SUFFIX: Readonly<Record<string, string>> = {
  dev: "dev", staging: "stg", stg: "stg", prod: "prod", local: "local",
};

export function envFileSuffix(env: string): string {
  return ENV_FILE_SUFFIX[env] ?? env;
}

export function gameConfigPath(game: string, env: string, baseDir: string = REPO_ROOT): string {
  return join(resolve(baseDir), "games", game, `env.config.${envFileSuffix(env)}.yml`);
}

/** Parse YAML the way PyYAML's `safe_load` does (YAML 1.1: `yes`/`no`/`on`/`off` are booleans). */
export function loadYaml(text: string): any {
  return parseYaml(text, { version: "1.1" });
}

/** The per-environment game config. Throws when the file is missing or not a mapping. */
export function loadGameConfig(game: string, env: string, baseDir: string = REPO_ROOT): Record<string, any> {
  const path = gameConfigPath(game, env, baseDir);
  if (!existsSync(path)) {
    const e = new Error(`Game config not found: ${path}. Expected a per-environment file at `
      + `games/${game}/env.config.${envFileSuffix(env)}.yml`);
    e.name = "FileNotFoundError";
    throw e;
  }
  let config: unknown;
  try {
    config = loadYaml(readFileSync(path, "utf-8"));
  } catch (exc) {
    const e = new Error(`Failed to parse game config ${path}: ${(exc as Error).message}`);
    e.name = "ValueError";
    throw e;
  }
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    const e = new Error(`Invalid game config ${path}: top-level must be a YAML mapping`);
    e.name = "ValueError";
    throw e;
  }
  return config as Record<string, any>;
}

/**
 * Parse a dotenv file the way python-dotenv reads it: `KEY=VALUE` and `export KEY=VALUE`; single
 * quotes are literal; double quotes understand `\n`, `\t`, `\"` and `\\`; an unquoted value ends
 * at ` #`; `${VAR}` / `${VAR:-default}` expand (python-dotenv interpolates by default) from the
 * values read so far, then the process environment.
 */
export function parseDotenv(text: string, env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const out: Record<string, string> = {};
  const expand = (v: string): string => v.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g,
    (_m, name: string, dflt: string | undefined) => out[name] ?? env[name] ?? dflt ?? "");
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i].trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice(7).trim();
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(key)) continue;
    let raw = line.slice(eq + 1).trimStart();
    let value: string;
    if (raw.startsWith("'") || raw.startsWith('"')) {
      const q = raw[0];
      let body = raw.slice(1);
      // a quoted value may run over several lines
      while (!new RegExp(`(^|[^\\\\])${q}`).test(body) && i + 1 < lines.length) body += `\n${lines[++i]}`;
      let end = -1;
      for (let j = 0; j < body.length; j++) {
        if (body[j] === "\\" && q === '"') {
          j++;
          continue;
        }
        if (body[j] === q) {
          end = j;
          break;
        }
      }
      body = end >= 0 ? body.slice(0, end) : body;
      value = q === "'" ? body : expand(body.replace(/\\([nrt"\\])/g, (_m, c: string) => ({ n: "\n", r: "\r", t: "\t", '"': '"', "\\": "\\" }[c] as string)));
    } else {
      const hash = raw.search(/\s#/);
      if (hash >= 0) raw = raw.slice(0, hash);
      value = expand(raw.trim());
    }
    out[key] = value;
  }
  return out;
}

/** Load base `.env` (existing variables win) then the `.env.{suffix}` overlay (it wins). Idempotent.
 * Returns the file names loaded. */
export function loadEnvFiles(env: string, baseDir: string = REPO_ROOT): string[] {
  const root = resolve(baseDir);
  const loaded: string[] = [];
  const base = join(root, ".env");
  if (existsSync(base)) {
    for (const [k, v] of Object.entries(parseDotenv(readFileSync(base, "utf-8")))) {
      if (process.env[k] === undefined) process.env[k] = v;
    }
    loaded.push(".env");
  }
  const overlay = join(root, `.env.${envFileSuffix(env)}`);
  if (existsSync(overlay)) {
    for (const [k, v] of Object.entries(parseDotenv(readFileSync(overlay, "utf-8")))) process.env[k] = v;
    loaded.push(`.env.${envFileSuffix(env)}`);
  }
  return loaded;
}
