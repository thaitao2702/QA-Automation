/**
 * The scenario runner (port of scripts/run_scenario.py `main`).
 *
 *     npx tsx src/runner/cli.ts --game mystic --env dev --suite games/mystic/test-suites/x.yml
 *
 * Per run: SETUP (language / paytable, before the game loads) -> PREPARE (one load, or ensure the
 * base in place) -> RUN the scenario -> grade stream asserts -> paytable reset -> run teardowns ->
 * CLEANUP (drain a bonus; assert the base when asked). After the session: the AI pass, result.json,
 * summary.json, replays and report.html. Exit 1 when any case is red or any run errored.
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { chromium } from "playwright";
import type { Bet } from "../engine/bet.js";
import { missingApi, startGame } from "../engine/bridge.js";
import { DriveContext } from "../engine/drive.js";
import { GAME, setProfile } from "../engine/game.js";
import { now, waitForFunction } from "../engine/page.js";
import { ensureBase, TEARDOWNS } from "../engine/teardowns.js";
import { pyJson, pyRepr, pyRound, pyStr } from "../py.js";
import { REGISTRY } from "../scenarios/index.js";
import { BadParams } from "../scenarios/base.js";
import { BoHelper } from "../services/bo/helper.js";
import { BO_SETTLE_S, boReset, boSetup, maxWinNeedsBet, readBaseBet } from "../services/bo/setup.js";
import { loadYaml, REPO_ROOT } from "../services/env.js";
import { WalletHelper } from "../services/wallet.js";
import { aiCalls, buildCases } from "./cases.js";
import { jsonable, type Processed, runVerdict, type Section, writeReplay, writeReport, writeSessionReplay } from "./report.js";
import { ConfigExit, resolveUrl, resolveUrlForLanguage } from "./urls.js";

/** Normalise a `teardown:` declaration to a list of registry names ([] = disabled). */
export function normTd(v: unknown): string[] {
  if (v === null || v === undefined) return [];
  if (typeof v === "string") return ["", "none"].includes(v.trim().toLowerCase()) ? [] : [v.trim()];
  if (Array.isArray(v)) return v.flatMap(normTd);
  return [];
}

/** Run the named teardowns in order. An unknown name is recorded and skipped; a teardown that
 * raises is recorded and, for a run-level teardown, ABORTS the suite (returns true). */
export async function runTeardowns(names: string[], ctx: DriveContext, label: string, processed: Processed[], suite = false): Promise<boolean> {
  for (const td of names) {
    const fn = TEARDOWNS[td];
    if (!fn) {
      console.log(`  ! unknown teardown '${td}' -- skipping`);
      processed.push({ name: `${label}:${td}`, error: `unknown teardown '${td}'` });
      continue;
    }
    try {
      console.log(`  teardown[${td}] ${suite ? "after all runs" : `after ${label}`}...`);
      await fn(ctx);
    } catch (e) {
      const scope = suite ? "suite teardown" : "run teardown";
      console.log(`  !! ${scope} '${td}' failed (${label}): ${(e as Error).message}${suite ? "" : " -- aborting run"}`);
      processed.push({ name: `${label}:${td}`, error: `teardown failed: ${(e as Error).message}` });
      if (!suite) return true;
    }
  }
  return false;
}

/** `time.strftime("%d-%m-%Y-%H%M%S")` in local time. */
function stamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const { values: args } = parseArgs({
    args: argv,
    options: {
      game: { type: "string", default: "mystic" },
      env: { type: "string", default: "dev" },
      url: { type: "string" },
      suite: { type: "string" },
      scenario: { type: "string" },
      headless: { type: "boolean", default: false },
      "slow-mo": { type: "string", default: "0" },
      "no-ai": { type: "boolean", default: false },
      only: { type: "string" },
      teardown: { type: "string" },
    },
  });
  const game = args.game as string;
  const env = args.env as string;

  // Build the list of runs (from --suite, or a one-off from --scenario) + the suite-end teardown.
  let runs: Array<Record<string, any>>;
  let suiteTeardown: string[] = [];
  let suiteAssertBase = false;
  let doc: Record<string, any> = {};
  if (args.suite) {
    doc = loadYaml(readFileSync(args.suite, "utf-8"));
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
      throw new ConfigExit(`ERROR: ${args.suite} is not a YAML mapping (expected a top-level \`runs:\` key).`);
    }
    const rawRuns: unknown[] = doc.runs || [];
    const empty = rawRuns.flatMap((r, i) => (r === null ? [i + 1] : []));
    if (empty.length) {
      throw new ConfigExit(`ERROR: ${args.suite} has an EMPTY entry under \`runs:\` (item ${empty.join(", ")} of ${rawRuns.length}) `
        + "-- usually a stray `-` with nothing under it. Delete the bare dash or give it a `run:` block.");
    }
    const bad = rawRuns.flatMap((r, i) => (!r || typeof r !== "object" || Array.isArray(r) ? [i + 1] : []));
    if (bad.length) {
      throw new ConfigExit(`ERROR: ${args.suite}: entry ${bad.join(", ")} under \`runs:\` is not a mapping -- each item must be a \`- run:\` block.`);
    }
    runs = (rawRuns as Array<Record<string, any>>).map((r) => ("run" in r ? r.run : r));
    suiteTeardown = normTd(doc.teardown);
    suiteAssertBase = !!doc.assert_base;
  } else if (args.scenario) {
    runs = [{
      scenario: args.scenario, teardown: args.teardown ?? null, cases: [{
        id: `${args.scenario}-default`, title: "default invariants",
        assert: [{ stream: "conservation" }, { stream: "stake_zero_in_freespins" }, { stream: "terminal_reached" }],
      }],
    }];
  } else {
    throw new ConfigExit("pass --suite <yaml> or --scenario <name>");
  }

  if (args.only) {
    const want = new Set(args.only.split(",").map((s) => s.trim()).filter(Boolean));
    runs = runs.filter((r) => want.has(r.name) || want.has(r.scenario));
    if (!runs.length) {
      const names = (args.suite ? (doc.runs || []).map((x: any) => (x && x.run) || x) : []).map((r: any) => r?.name || r?.scenario);
      throw new ConfigExit(`--only ${args.only}: no run with that name (or scenario) in the suite. Run names: ${names.filter(Boolean).join(", ")}`);
    }
  }

  setProfile(game);
  const url = resolveUrl(game, env, args.url ?? null);
  const root = join(REPO_ROOT, "test-results", game, env, `scenario-${stamp()}`);
  mkdirSync(root, { recursive: true });
  console.log(`Scenario runner | ${runs.length} run(s) | ${url.slice(0, 70)}...`);
  const summary: Record<string, any> = { suite: args.suite || `ad-hoc:${args.scenario}`, runs: [] };

  let wallet: WalletHelper | null = null;
  try {
    wallet = new WalletHelper(game, env);
  } catch (e) {
    console.log(`(wallet unavailable: ${(e as Error).message})`);
  }
  let bo: BoHelper | null = null;
  try {
    bo = new BoHelper(game, env);
  } catch (e) {
    console.log(`(bo unavailable: ${(e as Error).message})`);
  }
  // ⚠ START FROM A KNOWN PAYTABLE: the agent's paytable is SHARED state, and a leftover forced
  // fixture silently rigs every run that has no `paytable:` of its own.
  if (bo !== null && (await boReset(bo, "whatever was left selected (suite-start baseline)"))) {
    await new Promise((r) => setTimeout(r, BO_SETTLE_S * 1000));
  }

  const browser = await chromium.launch({ headless: !!args.headless, slowMo: Number(args["slow-mo"] || 0) });
  const videoDir = join(root, "_video");
  const context = await browser.newContext({ viewport: GAME.VIEWPORT, recordVideo: { dir: videoDir, size: GAME.VIEWPORT } });
  const page = await context.newPage();
  const videoStart = now();
  let vpath: string | null = null;
  const processed: Processed[] = [];
  const sections: Section[] = [];
  const openSection = (name: string): Section => {
    const t = pyRound(now() - videoStart, 2);
    if (sections.length && sections[sections.length - 1].end_s === null) sections[sections.length - 1].end_s = t;
    const sec: Section = { name, start_s: t, end_s: null, verdict: "error" };
    sections.push(sec);
    return sec;
  };
  const closeSections = () => {
    if (sections.length && sections[sections.length - 1].end_s === null) sections[sections.length - 1].end_s = pyRound(now() - videoStart, 2);
  };

  try {
    let lastCtx: DriveContext | null = null;
    let prevLastBet: Bet | null = null;
    let aborted = false;
    let gameLoaded = false;
    let prevPt = false;
    let curUrl = url;
    let curLang: string | null = null;

    const relaunch = async (lang: string | null): Promise<string> => {
      const newUrl = resolveUrlForLanguage(game, env, lang as string);
      await page.goto(newUrl, { waitUntil: "networkidle" });
      await waitForFunction(page, "() => !!window.__gameTestBridge", 30000);
      await startGame(page);
      curUrl = newUrl;
      curLang = lang;
      gameLoaded = true;
      return newUrl;
    };

    const usedNames = new Set<string>();
    for (const runDoc of runs) {
      const scenName = runDoc.scenario;
      let name = String(runDoc.name || scenName);
      if (usedNames.has(name)) {
        let k = 2;
        while (usedNames.has(`${name}-${k}`)) k += 1;
        name = `${name}-${k}`;
      }
      usedNames.add(name);
      const section = openSection(name);
      const Cls = REGISTRY.get(scenName);
      if (!Cls) {
        console.log(`  ! unknown scenario '${scenName}' -- skipping`);
        processed.push({ name, error: "unknown scenario" });
        continue;
      }
      const params = runDoc.params || {};
      let scenario;
      try {
        scenario = new Cls(params);
      } catch (e) {
        if (!(e instanceof BadParams)) throw e;
        console.log(`  ! bad params for '${name}': ${e.message} -- skipping`);
        processed.push({ name, error: `bad params: ${e.message}` });
        continue;
      }
      console.log(`\n=== run: ${name} (scenario: ${scenName}) ===`
        + (Object.keys(params).length ? `\n  params: ${pyRepr(params)}` : "")
        + `\n  ${scenario.description}`);
      const runDir = join(root, name);
      mkdirSync(join(runDir, "_shots"), { recursive: true });
      const ctx = new DriveContext(page, videoStart, {
        wallet, browser, url: curUrl, bo, relaunch, language: curLang, shotsDir: join(runDir, "_shots"),
      });
      const hasPt = !!runDoc.paytable;
      let ptActive = false;
      const wantAssertBase = !!(runDoc.assert_base ?? suiteAssertBase);

      // ── SETUP: what must be true BEFORE the game loads (language token, paytable) ──────
      const lang: string | null = runDoc.language ?? null;
      const needsLang = !!(lang && lang !== curLang);
      let targetUrl = curUrl;
      if (needsLang) {
        try {
          console.log(`  language[${lang}] minting a launch URL...`);
          targetUrl = resolveUrlForLanguage(game, env, lang as string);
        } catch (e) {
          console.log(`  !! language setup failed for '${name}': ${(e as Error).message} -- skipping run`);
          processed.push({ name, error: `language setup failed: ${(e as Error).message}` });
          continue;
        }
      }
      if (hasPt) {
        // A BET-DERIVED CAP HAS TO SEE THE BET: `max_win_x` is a multiple of the stake, so open the
        // game, read the stake it really sits at, and resolve the cap against that.
        let liveBet: number | null = null;
        if (maxWinNeedsBet(runDoc.paytable)) {
          if (!gameLoaded) {
            try {
              await page.goto(targetUrl, { waitUntil: "networkidle" });
              await waitForFunction(page, "() => !!window.__gameTestBridge", 30000);
              await startGame(page);
            } catch (e) {
              console.log(`  ! bet probe could not open the game: ${(e as Error).message}`);
            }
          }
          liveBet = await readBaseBet(page);
          console.log(liveBet !== null ? `  bet[live] ${liveBet}` : "  ! bet[live] unreadable -- falling back to bo.reference_bet");
        }
        try {
          await boSetup(bo, game, runDoc.paytable, liveBet);
          ptActive = true;
        } catch (e) {
          console.log(`  !! paytable setup failed for '${name}': ${(e as Error).message} -- skipping run`);
          processed.push({ name, error: `paytable setup failed: ${(e as Error).message}` });
          if (bo) await boReset(bo);
          prevPt = false;
          continue;
        }
      }

      // ── PREPARE: start from a clean, spinnable base — ONE load covers every reason ─────
      const needsLoad = !gameLoaded || needsLang || hasPt || prevPt;
      if (needsLoad) {
        try {
          await page.goto(targetUrl, { waitUntil: "networkidle" });
          await waitForFunction(page, "() => !!window.__gameTestBridge", 30000);
          if (!gameLoaded) {
            const miss = await missingApi(page);
            if (miss.length) {
              console.log(`ERROR: bridge missing ${pyRepr(miss)} -- no runs executed`);
              if (ptActive && bo) await boReset(bo, name);
              break;
            }
          }
          await ctx.captureGameInfo("prepare load");
          await startGame(page, 60000, async (raw) => {
            ctx.logApi("freespin_response", { phase: "resumed at load", response: raw });
          });
          curUrl = targetUrl;
          gameLoaded = true;
          if (needsLang) curLang = lang;
          ctx.language = curLang;
          ctx.url = curUrl;
        } catch (e) {
          console.log(`  !! prepare[${name}] load failed: ${(e as Error).message} -- skipping run`);
          processed.push({ name, error: `prepare failed (load): ${(e as Error).message}` });
          if (ptActive && bo) await boReset(bo, name);
          prevPt = false;
          continue;
        }
      } else {
        try {
          const state = await ensureBase(ctx, prevLastBet, `prepare[${name}]`, wantAssertBase);
          if (state !== "clean") console.log(`  prepare[${name}]: ${state}`);
        } catch (e) {
          console.log(`  !! prepare failed for '${name}': ${(e as Error).message} -- skipping run`);
          processed.push({ name, error: `prepare failed: ${(e as Error).message}` });
          continue;
        }
      }

      try {
        const t0 = now();
        const result = await scenario.run(ctx);
        if (!result) throw new Error(`scenario '${name}' returned None -- its run() fell through without a ScenarioResult`);
        const durationMs = Math.trunc((now() - t0) * 1000);
        console.log(`  reached=${result.reached ? "True" : "False"}` + (result.reason ? ` (${result.reason})` : "") + ` | rounds=${result.rounds.length}`);
        const cases = buildCases(runDoc, result);
        writeFileSync(join(runDir, "events.jsonl"), result.events.map((e) => `${JSON.stringify(jsonable(e))}\n`).join(""), "utf-8");
        writeFileSync(join(runDir, "timeline.json"), JSON.stringify(jsonable(result.timeline), null, 2), "utf-8");
        processed.push({ name, run_dir: runDir, result, cases, description: scenario.description, duration_ms: durationMs, section, api_events: ctx.apiEvents });
      } catch (e) {
        console.log(`  ! run '${name}' errored: ${(e as Error).name}: ${(e as Error).message}`);
        processed.push({ name, error: `${(e as Error).name}: ${(e as Error).message}` });
      }
      if (ptActive && bo) await boReset(bo, name);
      prevPt = hasPt;
      lastCtx = ctx;
      // Run-level teardown FIRST (a teardown that reloads leaves the game clean for the check).
      aborted = await runTeardowns(normTd(runDoc.teardown), ctx, name, processed);
      if (aborted) break;
      // CLEANUP: always repairs; asserts the base only when the suite asked for it.
      try {
        const state = await ensureBase(ctx, null, `cleanup[${name}]`, wantAssertBase);
        if (state !== "clean") console.log(`  cleanup[${name}]: ${state}`);
      } catch (e) {
        console.log(`  !! cleanup failed for '${name}': ${(e as Error).message} -- aborting suite`);
        processed.push({ name, error: `cleanup failed: ${(e as Error).message}` });
        break;
      }
      prevLastBet = ctx.lastBet;
    }
    closeSections();
    if (!aborted && lastCtx !== null) await runTeardowns(suiteTeardown, lastCtx, "suite", processed, true);
    try {
      vpath = (await page.video()?.path()) ?? null;
    } catch {
      vpath = null;
    }
  } catch (e) {
    console.log(`\nsession error: ${(e as Error).name}: ${(e as Error).message}`);
    closeSections();
  } finally {
    await context.close();
    await browser.close();
  }

  // ONE session video at the run root; every scenario references it.
  const sessionPath = join(root, "session.webm");
  if (vpath && existsSync(vpath)) {
    copyFileSync(vpath, sessionPath);
    rmSync(dirname(vpath), { recursive: true, force: true });
  }

  // AI pass: trim each assert's sub-clip, analyse, grade -> pending becomes green/orange/red.
  // Loaded only when AI grading is on, so a `--no-ai` run needs none of the provider SDKs.
  const ai = args["no-ai"] ? null : await import("../services/ai/ai-video.js");
  const provider = ai === null ? null : ai.makeVideoProvider(env);
  const imgProvider = ai === null ? null : ai.makeImageProvider(env);
  const graded = processed.filter((p) => p.run_dir);
  const nVideo = graded.reduce((n, p) => n + aiCalls(p.cases!), 0);
  if (args["no-ai"]) console.log(`\nAI: disabled (--no-ai) -- ${nVideo} video assert(s) stay pending`);
  else if (provider === null) console.log(`\nAI: no provider available -- ${nVideo} video assert(s) stay pending (check GEMINI_API_KEY / AI_PROVIDER in .env)`);
  else console.log(`\nAI: ${(provider as any).model ?? provider.constructor.name} -- grading ${nVideo} AI call(s)`);
  for (const p of graded) {
    const cases = p.cases!;
    const nV = cases.reduce((n, c) => n + c.video.filter((v) => v.status === "pending").length, 0);
    const shotKeys = new Set<string>();
    cases.forEach((c) => c.image.forEach((sp, i) => (sp.shots || []).forEach((sh: any) => {
      shotKeys.add(`${sp.group || `${c.id}-img${i}`}|${sp.combine ? "__combined__" : sh.file}`);
    })));
    const nImg = cases.reduce((n, c) => n + c.image.length, 0);
    if (provider !== null && nV) console.log(`  ${p.name}: ${nV} video assert(s)`);
    if (imgProvider !== null && nImg) {
      if (shotKeys.size) console.log(`  ${p.name}: ${shotKeys.size} screenshot(s)`);
      else console.log(`  ${p.name}: ${nImg} image assert(s) but the scenario captured NO screenshots -- add ctx.screenshot(...) to it, or use a \`video:\` assert`);
    }
  }
  if (provider !== null || imgProvider !== null) {
    const tAi = now();
    await ai!.runAi(provider, imgProvider, sessionPath, graded.map((p) => [p.run_dir!, p.cases!] as [string, any[]]), { game });
    console.log(`  AI pass took ${(now() - tAi).toFixed(0)}s`);
  }

  for (const p of processed) {
    if (!p.run_dir) {
      summary.runs.push({ scenario: p.name, error: p.error ?? null, cases: [] });
      console.log(`${p.name.padEnd(20)} ERROR: ${p.error}`);
      continue;
    }
    const cases = p.cases!;
    const result = p.result!;
    writeFileSync(join(p.run_dir, "result.json"), JSON.stringify(jsonable({
      kind: "run", scenario: p.name, description: p.description, reached: result.reached, reason: result.reason,
      video_window: result.video_window, shots: result.shots, facts: result.facts || {}, marks: result.marks || {}, cases,
    }), null, 2), "utf-8");
    const verdict = runVerdict(result.reached, cases);
    p.verdict = verdict;
    p.section!.verdict = verdict;
    summary.runs.push({ scenario: p.name, reached: result.reached, cases: cases.map((c) => ({ id: c.id, verdict: c.verdict })) });
    console.log(`${p.name.padEnd(20)} ${verdict}`);
    for (const c of cases) {
      const vids = c.video;
      const imgs = c.image ?? [];
      const vstat = vids.length ? ` video=${vids.filter((v) => v.status === "pass").length}/${vids.length}` : "";
      const istat = imgs.length ? ` image=${imgs.filter((v) => v.status === "pass").length}/${imgs.length}` : "";
      console.log(`    ${String(c.id).padEnd(16)} ${String(c.verdict).padEnd(12)} stream=${c.stream.filter((s) => s.result.valid).length}/${c.stream.length}${vstat}${istat}`);
      for (const v of vids) {
        const answer = v.kind === "read" ? v.read : v.data;
        const detail = v.note || (answer !== null && answer !== undefined ? pyJson(answer) : "");
        const where = "each" in v ? `each: ${v.each}` : v.at || "all";
        console.log(`        video[${where}] ${String(v.status ?? "?").padEnd(8)} ${detail}`);
      }
      for (const v of imgs) {
        const shotResults: any[] = v.shot_results || [];
        const bad = shotResults.filter((r) => r.status !== "pass");
        console.log(`        image[${shotResults.length} shot(s)] ${String(v.status ?? "?").padEnd(8)} ${v.note || ""}`);
        for (const r of bad.slice(0, 8)) console.log(`            ${r.shot ?? "?"}: ${r.note || pyStr(r.data ?? null)}`);
        if (bad.length > 8) console.log(`            ... and ${bad.length - 8} more failing shot(s)`);
      }
    }
  }

  // Replays last: every one shows ALL runs as chapters, so every run must be graded first.
  const sessionInfo = processed.flatMap((p, k) => (p.run_dir ? (p.api_events || []).filter((e) => e.type === "game_info")
    .map((e) => [k, { ...e, data: { ...e.data, run: p.name } }] as const) : []));
  processed.forEach((p, k) => {
    if (!p.run_dir) return;
    const others = sessionInfo.filter(([j]) => j !== k).map(([, e]) => e);
    writeReplay(p.run_dir, p.name, `${p.name} scenario`, p.verdict!, p.result!, p.duration_ms!, sections, p.section!, [...(p.api_events || []), ...others]);
  });
  writeSessionReplay(root, summary.suite, processed, sections);
  if (provider !== null && typeof (provider as any).costSummary === "function") summary.ai_cost = (provider as any).costSummary();
  writeFileSync(join(root, "summary.json"), JSON.stringify(jsonable(summary), null, 2), "utf-8");
  const report = writeReport(root, summary.suite, processed, summary.ai_cost || {});
  console.log(`\nreport  -> ${report}`);
  console.log(`summary -> ${join(root, "summary.json")}`);
  const reds = summary.runs.reduce((n: number, r: any) => n + (r.cases || []).filter((c: any) => c.verdict === "red").length, 0);
  const errs = summary.runs.filter((r: any) => r.error).length;
  return reds || errs ? 1 : 0;
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  main().then((code) => process.exit(code), (e) => {
    if (e instanceof ConfigExit) {
      console.error(e.message);
      process.exit(1);
    }
    console.error(e);
    process.exit(1);
  });
}
