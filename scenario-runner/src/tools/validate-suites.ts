/**
 * Validate a game's suites WITHOUT a browser (port of tests/chaos/validate_suites.py).
 *
 *     npx tsx src/tools/validate-suites.ts --game chaos
 *
 * Checks, in order of how quietly they fail in production:
 *   1. every `{facts.X}` a suite references is exposed by that run's scenario — a typo'd fact
 *      resolves to the literal "null" and the predicate QUIETLY PASSES
 *   2. every `scenario:` resolves in the REGISTRY
 *   3. every named `stream:` invariant resolves in INVARIANTS
 *   4. every `teardown:` resolves in TEARDOWNS, and every `params:` set is accepted
 *   5. inline `stream:` predicates reference only bindable roots
 *   6. case ids are unique across the whole game and map to real matrix rows
 *
 * The exposed facts are read STATICALLY from src/scenarios/*.ts: every object-literal key inside a
 * scenario class, plus the keys of helpers spread into facts, plus per-outcome fan-outs
 * (`FACT_PREFIXES` x computed `${key}_suffix` keys). It is per-game because the profile is set first.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { parseArgs } from "node:util";
import ts from "typescript";
import { setProfile } from "../engine/game.js";
import { INVARIANTS } from "../engine/invariants.js";
import { TEARDOWNS } from "../engine/teardowns.js";
import { pyRepr } from "../py.js";
import { BadParams } from "../scenarios/base.js";
import { REGISTRY } from "../scenarios/index.js";
import { loadYaml, REPO_ROOT } from "../services/env.js";

const GAMES: Record<string, { matrix: string; prefix: string; skip: Set<string> }> = {
  chaos: { matrix: "test-case-chaos.csv", prefix: "CR_", skip: new Set(["test.yml", "temp.yml"]) },
  mystic: { matrix: "test-case-mahjong.csv", prefix: "Mah_", skip: new Set(["test.yml", "temp.yml", "raw.yml", "scenario-demo.yml"]) },
  zeus: { matrix: "test-case-zeus.xlsx", prefix: "ZZ-", skip: new Set(["test.yml", "temp.yml", "raw.yml", "vocab.yml", "freespin-runs.yml"]) },
  dragon: { matrix: "test-case-dragon.xlsx", prefix: "DR-", skip: new Set(["test.yml", "temp.yml"]) },
  "dragon-vip": { matrix: "test-case-dragon-vip.xlsx", prefix: "DV-", skip: new Set(["test.yml", "temp.yml"]) },
  joker: { matrix: "test-case-joker.xlsx", prefix: "JK-", skip: new Set(["test.yml", "temp.yml"]) },
  grandma: { matrix: "test-case-grandma.csv", prefix: "TC-", skip: new Set(["test.yml", "temp.yml"]) },
};

/** Helpers whose result is spread into `facts`; their keys cannot be read from the caller. */
const SPREAD_HELPERS: Record<string, string[]> = {
  confirmStopped: ["stopped", "extra_round_after_stop", "auto_flag_after", "auto_flag_cleared_s", "auto_spins_left_after", "stop_watch_s"],
  settleToBase: ["returned_to_base", "celebration_shown", "celebration_auto_closed", "popups_at_end", "base_ready_seconds",
    "bet_after_return", "base_watch_s", "base_hold_s"],
};

const SCENARIO_DIR = join(REPO_ROOT, "scenario-runner", "src", "scenarios");
const ENGINE_DIR = join(REPO_ROOT, "scenario-runner", "src", "engine");
const SHOT_CALL = /\b(ctx|this)\.screenshot\(/;
const TOKEN = /\{([a-zA-Z_][a-zA-Z_0-9]*)(?:\[(\d+)\])?\.([a-zA-Z_][a-zA-Z_0-9]*)\}/g;
const BINDABLE_ROOTS = new Set(["res", "trigger", "terminal", "rounds", "facts", "prev", "shot"]);
const BET_FIELDS = new Set(["seq", "game_state", "free_spins", "total_free_spin", "won_free_spins", "stake", "base_stake",
  "payout", "balance", "has_max_win", "cascades", "round_id", "raw", "phase", "max_win", "row"]);

interface ScannedClass {
  facts: Set<string>;
  captures: boolean;
  returns: boolean;
}

function sourceFiles(dir: string, skip: string[] = []): ts.SourceFile[] {
  return readdirSync(dir).filter((f) => f.endsWith(".ts") && !skip.includes(f))
    .map((f) => ts.createSourceFile(f, readFileSync(join(dir, f), "utf-8"), ts.ScriptTarget.ES2022, true));
}

/** Top-level functions (in the engine and the scenario files) that, transitively, take a screenshot. */
function shotProducingHelpers(files: ts.SourceFile[]): Set<string> {
  const bodies = new Map<string, string>();
  for (const sf of files) {
    ts.forEachChild(sf, (n) => {
      if (ts.isFunctionDeclaration(n) && n.name) bodies.set(n.name.text, n.getText(sf));
    });
  }
  const shooters = new Set([...bodies].filter(([, b]) => SHOT_CALL.test(b)).map(([n]) => n));
  for (let i = 0; i < 5; i++) {
    const before = shooters.size;
    for (const [n, b] of bodies) if ([...shooters].some((h) => b.includes(`${h}(`))) shooters.add(n);
    if (shooters.size === before) break;
  }
  shooters.add("tourUi");             // a DriveContext method
  return shooters;
}

/** name -> what each scenario class exposes and whether it can produce (and returns) shots. */
export function scanScenarios(): Map<string, ScannedClass> {
  const files = sourceFiles(SCENARIO_DIR, ["base.ts", "index.ts"]);
  const shooters = shotProducingHelpers([...files, ...sourceFiles(ENGINE_DIR)]);
  const out = new Map<string, ScannedClass>();
  for (const sf of files) {
    ts.forEachChild(sf, (node) => {
      if (!ts.isClassDeclaration(node)) return;
      let name: string | null = null;
      for (const m of node.members) {
        if (ts.isPropertyDeclaration(m) && m.name.getText(sf) === "name" && m.initializer && ts.isStringLiteralLike(m.initializer)) name = m.initializer.text;
      }
      if (!name) return;
      const facts = new Set<string>();
      const suffixes = new Set<string>();
      const walk = (n: ts.Node) => {
        if (ts.isObjectLiteralExpression(n)) {
          for (const p of n.properties) {
            if (!(ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p))) continue;
            if (ts.isIdentifier(p.name) || ts.isStringLiteralLike(p.name)) facts.add(p.name.text);
            else if (ts.isComputedPropertyName(p.name)) {
              const e = p.name.expression;
              const tail = ts.isTemplateExpression(e) ? e.templateSpans.map((s) => s.literal.text).join("") : "";
              if (tail.startsWith("_")) suffixes.add(tail);
            }
          }
        }
        ts.forEachChild(n, walk);
      };
      walk(node);
      const text = node.getText(sf);
      for (const [helper, keys] of Object.entries(SPREAD_HELPERS)) if (text.includes(`${helper}(`)) keys.forEach((k) => facts.add(k));
      const cls = REGISTRY.get(name) as unknown as { FACT_PREFIXES?: readonly string[] } | undefined;
      for (const prefix of cls?.FACT_PREFIXES ?? []) for (const suf of suffixes) facts.add(prefix + suf);
      out.set(name, {
        facts,
        captures: SHOT_CALL.test(text) || [...shooters].some((h) => text.includes(`${h}(`)),
        returns: /shots:\s*ctx\.shots/.test(text),
      });
    });
  }
  return out;
}

function readMatrix(name: string): [Set<string>, string] {
  const path = join(REPO_ROOT, name);
  if (!existsSync(path)) return [new Set(), `${name} not found`];
  if (!name.toLowerCase().endsWith(".csv")) {
    // Spreadsheet ingestion stays in Python (project decision 2026-11-10).
    return [new Set(), `${name} is a spreadsheet; coverage needs the Python validator (python tests/chaos/validate_suites.py)`];
  }
  const rows = parseCsv(readFileSync(path, "utf-8").replace(/^﻿/, ""));
  const header = rows.shift() ?? [];
  const col = header.indexOf("ID");
  return [new Set(rows.map((r) => (r[col] ?? "").trim()).filter(Boolean)), ""];
}

/** RFC-4180 CSV, as Python's csv module reads it (quoted fields, doubled quotes, embedded newlines). */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i++;
      } else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function* walkStrings(node: unknown): Generator<string> {
  if (typeof node === "string") yield node;
  else if (Array.isArray(node)) for (const v of node) yield* walkStrings(v);
  else if (node && typeof node === "object") for (const v of Object.values(node)) yield* walkStrings(v);
}

export function validate(game: string): { errors: string[]; warnings: string[]; lines: string[] } {
  const errors: string[] = [];
  const warnings: string[] = [];
  const lines: string[] = [];
  setProfile(game);
  const scanned = scanScenarios();
  const cfg = GAMES[game];
  const [matrixIds, matrixNote] = readMatrix(cfg.matrix);
  if (matrixNote) warnings.push(`coverage not reported: ${matrixNote}`);
  const dir = join(REPO_ROOT, "games", game, "test-suites");
  const suites = readdirSync(dir).filter((f) => f.endsWith(".yml") && !cfg.skip.has(f)).sort().map((f) => join(dir, f));
  if (!suites.length) throw new Error(`no ${game} suites found`);
  const seenIds = new Map<string, string>();
  const covered = new Set<string>();
  let totalCases = 0;
  for (const path of suites) {
    const short = basename(path);
    const doc = loadYaml(readFileSync(path, "utf-8"));
    if (!doc || !("runs" in doc)) {
      errors.push(`${short}: no \`runs:\` key`);
      continue;
    }
    const runNames = new Map<string, number>();
    (doc.runs as any[]).forEach((entry, i) => {
      const run = entry && "run" in entry ? entry.run : entry;
      const sname = run.scenario;
      const where = `${short} run[${i}] (${sname})`;
      const rname = run.name;
      if (rname !== undefined && rname !== null) {
        if (!(typeof rname === "string" && /^[a-z0-9][a-z0-9._-]*$/.test(rname))) {
          errors.push(`${where}: run name ${pyRepr(rname)} must be lower-case letters, digits, '-', '_' or '.' (it becomes a folder name)`);
        } else if (runNames.has(rname)) {
          errors.push(`${where}: run name '${rname}' is already used by run[${runNames.get(rname)}] -- names must be unique in a suite`);
        } else runNames.set(rname, i);
      }
      if (run.videos !== undefined && run.videos !== null && !Array.isArray(run.videos)) errors.push(`${where}: \`videos:\` must be a LIST of video blocks`);
      const blocks: Array<[string, any]> = [
        ...(run.video && typeof run.video === "object" && !Array.isArray(run.video) ? [["video", run.video] as [string, any]] : []),
        ...((run.videos || []) as any[]).map((b, k) => [`videos[${k}]`, b] as [string, any]).filter(([, b]) => b && typeof b === "object"),
      ];
      for (const [label, vid] of blocks) {
        if (vid.prompt && scanned.has(sname)) {
          const known = scanned.get(sname)!.facts;
          const toks = [...new Set([...String(vid.prompt).matchAll(/\{facts\.([a-z_0-9]+)\}/g)].map((m) => m[1]))].sort();
          for (const tok of toks) {
            if (!known.has(tok)) errors.push(`${where} ${label}: the prompt uses '{facts.${tok}}', which scenario '${sname}' does not expose -- it would render as the literal 'null'`);
          }
        }
        if (vid.expected_by_case) {
          const asked = new Set(Object.keys(vid.output_schema || {}).filter((k) => !["notes", "confidence", "reason"].includes(k)));
          const graded = new Set(Object.values(vid.expected_by_case as Record<string, any>).flatMap((exp) => Object.keys(exp || {})));
          const missing = [...asked].filter((k) => !graded.has(k)).sort();
          if (missing.length) warnings.push(`${where} ${label}: video asks for ${missing.join(", ")}, which no case grades -- grade it, or note why it is discovery-only`);
        }
        const at = vid.at;
        if (Array.isArray(at) && at.length !== 2) errors.push(`${where} ${label}: video.at is a list of ${at.length} -- a span must be exactly 2 targets (got ${pyRepr(at)})`);
        if (vid.each !== undefined && vid.each !== null) {
          if (typeof vid.each !== "string") errors.push(`${where} ${label}: video.each must be ONE mark name (got ${pyRepr(vid.each)})`);
          if (vid.at !== undefined && vid.at !== null) errors.push(`${where} ${label}: video has both \`each:\` and \`at:\` -- \`each\` takes its clips from the mark, so \`at\` would be ignored`);
        }
        const mr = vid.max_rounds;
        if (mr !== undefined && mr !== null) {
          if (vid.each === undefined || vid.each === null) errors.push(`${where} ${label}: \`max_rounds\` only applies to an \`each:\` block -- here it would be ignored`);
          if (!(typeof mr === "number" && Number.isInteger(mr) && mr > 0)) errors.push(`${where} ${label}: \`max_rounds\` must be a positive whole number (got ${pyRepr(mr)})`);
        }
      }
      const Cls = REGISTRY.get(sname);
      if (!Cls) {
        errors.push(`${where}: unknown scenario '${sname}'`);
        return;
      }
      const exposed = scanned.get(sname)?.facts ?? new Set<string>();
      const td = run.teardown;
      for (const t of typeof td === "string" ? [td] : (td || [])) if (t && !(t in TEARDOWNS)) errors.push(`${where}: unknown teardown '${t}'`);
      const params = run.params || {};
      if (Object.keys(params).length) {
        try {
          new Cls(params);
        } catch (e) {
          if (e instanceof BadParams) errors.push(`${where}: bad params ${pyRepr(Object.keys(params).sort())} -- ${e.message}`);
          else throw e;
        }
      }
      for (const kase of run.cases || []) {
        const cid = kase.id;
        totalCases += 1;
        if (seenIds.has(cid)) errors.push(`duplicate case id ${cid} (${seenIds.get(cid)} and ${short})`);
        seenIds.set(cid, short);
        const claims = [...String(cid).matchAll(/\d+/g)].map((m) => cfg.prefix + m[0]);
        claims.filter((c) => matrixIds.has(c)).forEach((c) => covered.add(c));
        if (!String(cid).startsWith(cfg.prefix)) warnings.push(`${short}: case id ${cid} is not a ${cfg.prefix} id`);
        else if (matrixIds.size && !claims.some((c) => matrixIds.has(c))) warnings.push(`${short}: case id ${cid} has no row in ${cfg.matrix}`);
        for (const a of kase.assert || []) {
          if (a && typeof a === "object" && "image" in a) {
            const s = scanned.get(sname);
            if (!s?.captures) errors.push(`${where} / ${cid}: \`image:\` assert but scenario '${sname}' captures NO screenshots — add ctx.screenshot(...) to it, or use \`video:\``);
            else if (!s.returns) errors.push(`${where} / ${cid}: scenario '${sname}' captures stills but never returns them (ScenarioResult(..., shots=ctx.shots)) — the image assert would grade nothing`);
          }
          if (a && typeof a === "object" && typeof a.stream === "string") {
            const s = a.stream.trim();
            if (/^[a-z_][a-z0-9_]*(\(.*\))?$/.test(s)) {
              const iname = s.split("(")[0];
              if (!(iname in INVARIANTS)) errors.push(`${where} / ${cid}: unknown invariant '${iname}'`);
            }
          }
          for (const text of walkStrings(a)) {
            for (const m of text.matchAll(TOKEN)) {
              const [, root, , field] = m;
              if (!BINDABLE_ROOTS.has(root)) errors.push(`${where} / ${cid}: '{${root}.${field}}' — '${root}' is not a bindable root ${pyRepr([...BINDABLE_ROOTS].sort())}`);
              else if (root === "facts") {
                if (!exposed.has(field)) {
                  errors.push(`${where} / ${cid}: '{facts.${field}}' is NOT exposed by scenario '${sname}' — it would resolve to the literal 'null' and the assert would quietly PASS. Exposed: ${pyRepr([...exposed].sort())}`);
                }
              } else if (["res", "trigger", "terminal", "rounds"].includes(root) && !BET_FIELDS.has(field)) {
                errors.push(`${where} / ${cid}: '{${root}.${field}}' is not a Bet field ${pyRepr([...BET_FIELDS].sort())}`);
              }
            }
          }
        }
      }
    });
  }
  lines.push(`game     : ${game}`, `suites   : ${suites.length}`, `cases    : ${totalCases}  (unique ids: ${seenIds.size})`,
    `matrix   : ${covered.size} rows covered of ${matrixIds.size} (${cfg.matrix})`);
  return { errors, warnings, lines };
}

function main(): number {
  const { values } = parseArgs({ options: { game: { type: "string", default: "chaos" } } });
  const game = values.game as string;
  if (!(game in GAMES)) {
    console.error(`--game must be one of ${Object.keys(GAMES).sort().join(", ")}`);
    return 2;
  }
  const { errors, warnings, lines } = validate(game);
  for (const l of lines) console.log(l);
  console.log();
  if (warnings.length) {
    console.log(`WARNINGS (${warnings.length}):`);
    for (const w of warnings) console.log(`  ~ ${w}`);
    console.log();
  }
  if (errors.length) {
    console.log(`ERRORS (${errors.length}):`);
    for (const e of errors) console.log(`  ! ${e}`);
    return 1;
  }
  console.log("OK — every scenario, invariant, teardown and {facts.*} reference resolves.");
  return 0;
}

if (process.argv[1] && process.argv[1].replace(/\\/g, "/").endsWith("tools/validate-suites.ts")) process.exit(main());
