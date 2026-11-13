/**
 * Launch URL resolution (port of the URL block of run_scenario.py). The configured `url_script` is a
 * command line (today a Python tool, scripts/get_game_url_playsite.py) and is run as configured.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { pyRepr } from "../py.js";
import { loadEnvFiles, loadYaml, REPO_ROOT } from "../services/env.js";

export class ConfigExit extends Error {
  override name = "SystemExit";
}

export function gameConfig(game: string, env: string): Record<string, any> {
  const suffix = env === "staging" || env === "stg" ? "stg" : env;
  const path = join(REPO_ROOT, "games", game, `env.config.${suffix}.yml`);
  if (!existsSync(path)) throw new ConfigExit(`No game config at ${path}`);
  return loadYaml(readFileSync(path, "utf-8")) || {};
}

/** Append the config's browser_params and guarantee bridge=1 (nothing is testable without it). */
export function withParams(url: string, cfg: Record<string, any>): string {
  let out = url;
  for (const extra of [String(cfg.browser_params || "").replace(/^[?&]+/, ""), ""]) {
    if (extra && !out.includes(extra)) out += (out.includes("?") ? "&" : "?") + extra;
  }
  return out.includes("bridge=1") ? out : `${out}${out.includes("?") ? "&" : "?"}bridge=1`;
}

/** Run the config's `url_script` and return the launch URL it prints. Credentials come from the
 * .env overlay of `token_env` (default: the run env). */
export function runUrlScript(cfg: Record<string, any>, env: string, lang: string | null = null): string {
  const script = cfg.url_script;
  if (!script) throw new Error("game config has no `url_script:` -- needed to mint a launch token");
  try {
    loadEnvFiles(cfg.token_env || env);              // PLAYSITE_API_URL / USER / PASS
  } catch {
    // best-effort, as in Python
  }
  const subEnv: NodeJS.ProcessEnv = { ...process.env };
  for (const [key, v] of [["game_code", "PLAYSITE_GAME_CODE"], ["currency", "PLAYSITE_CURRENCY"]] as const) {
    const val = (cfg.playsite || {})[key];
    if (val !== null && val !== undefined && val !== "") subEnv[v] = String(val);
  }
  if (lang) subEnv.PLAYSITE_LANGUAGE = lang;
  const r = spawnSync(String(script), { shell: true, cwd: REPO_ROOT, env: subEnv, encoding: "utf-8" });
  const out = (r.stdout || "").trim();
  if (r.status !== 0 || !out) {
    throw new Error(`url_script failed${lang ? ` for language ${pyRepr(lang)}` : ""} (exit ${r.status}): ${(r.stderr || "").trim().slice(0, 300)}`);
  }
  return out;
}

/**
 * The one URL resolver, in priority order:
 *   1. `--url` override — used as-is;
 *   2. `base_url` (a LOCAL build): mint a token via `url_script` and hang its query string off base_url;
 *   3. `url` (a pasted launch URL) — but NOT for a language run (the token carries its language);
 *   4. `url_script` — resolve fresh from the backend.
 */
export function resolveUrl(game: string, env: string, override: string | null = null, lang: string | null = null): string {
  if (override) return withParams(override, {});
  const cfg = gameConfig(game, env);
  const base = String(cfg.base_url || "").trim();
  if (base) {
    const tokenUrl = runUrlScript(cfg, env, lang);
    // urllib's `urlparse(url).query`: after the first "?", before any "#".
    const q = tokenUrl.indexOf("?");
    const query = q < 0 ? "" : tokenUrl.slice(q + 1).split("#")[0];
    if (!query) throw new Error(`url_script returned no query string to graft onto base_url: ${tokenUrl.slice(0, 80)}`);
    return withParams(`${base}${base.includes("?") ? "&" : "?"}${query}`, cfg);
  }
  const staticUrl = String(cfg.url || "").trim();
  if (staticUrl && !lang) return withParams(staticUrl, cfg);
  if (cfg.url_script) return withParams(runUrlScript(cfg, env, lang), cfg);
  throw new ConfigExit(`env.config for '${game}'/'${env}' has none of base_url / url / url_script. Pass --url.`);
}

/** A launch URL in `lang` — the language lives in the LAUNCH TOKEN, so every change re-mints it. */
export function resolveUrlForLanguage(game: string, env: string, lang: string): string {
  return resolveUrl(game, env, null, lang);
}
