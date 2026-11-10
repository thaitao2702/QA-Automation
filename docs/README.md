# Docs index

Read this page, then open only what you need. Every doc is listed once, under the job it serves.

---

## Start here

| Doc | Read it when |
|---|---|
| [qa-guide.md](qa-guide.md) · [🇻🇳](qa-guide.vi.md) | **First time on the platform.** What every piece is and why it exists, before any commands. |
| [qa-workflow.md](qa-workflow.md) · [🇻🇳](qa-workflow.vi.md) | You have a game and its docs, and want the path from there to passing tests. |
| [comprehensive-guide.md](comprehensive-guide.md) | Setup, configuration and usage end to end. The long reference — skim the two above first. |
| [qa-framework-guide.md](qa-framework-guide.md) | 🇻🇳 Vietnamese full guide to the framework. |

## Writing tests

| Doc | Read it when |
|---|---|
| [scenario-assert-authoring.md](scenario-assert-authoring.md) | **Writing test cases** — the YAML `scenario:` + `assert:` layer. §2 is the one to read before writing any `video:` prompt: a response value is not what the screen shows at that moment. |
| [assert-traps.md](assert-traps.md) | **Before writing any assert.** Real failures (balance, counting, inverse pair, per-round window) and the rules each one taught. Add new ones here. |
| [scenario-toolkit.md](scenario-toolkit.md) | **Writing scenario code — start here.** Every helper a scenario may call, grouped by job, with signatures, the facts each returns, a scenario template, and the ones never to call. Checked against the engine, so it is complete. |
| [scenario-authoring-rules.md](scenario-authoring-rules.md) | **Writing scenario code** (`scripts/scenario_engine.py`). An **index**: the toolkit, then all 21 rules with a summary and "read it when" each, linking to one file per rule in [scenario-rules/](scenario-rules/). Read the index, open only the rules you need. |
| [authoring-flow.md](authoring-flow.md) | Writing cases in the readable `.flow.yml` form. |

## The bridge (what the game must expose)

| Doc | Read it when |
|---|---|
| [game-bridge-contract.md](game-bridge-contract.md) | **The contract.** Required and optional functions, what each does and must provide, the boot/resume sequence, `getSnapshot` and `getGameInfo` shapes. Start at §1 (division of labour) and §3 (boot sequence). |
| [zeus-bridge-defects.md](zeus-bridge-defects.md) | Chasing a Zeus failure, or sending defects to the game team. D1 is the blocker. |

## Running and integrating

| Doc | Read it when |
|---|---|
| [run-lifecycle.md](run-lifecycle.md) | **What happens when you run a suite** — the ordered flow of a run, what the framework does for you vs what your scenario must do itself, and which failures skip a run vs abort the suite. Read this when a run fails somewhere that is not your scenario. |
| [framework-runner-guide.md](framework-runner-guide.md) | Function-by-function walkthrough of the runner. For changing how runs execute. |
| [bo-integration.md](bo-integration.md) | Driving the back office — forcing paytables for deterministic outcomes. |
| [bo-denominations.md](bo-denominations.md) | Back-office **denominations** — read a game's bet ladders, create or edit one, assign it to the master game or to one agent. Read §3 before any write: `denomination/update` replaces the whole list. |
| [env-config-example.yml](env-config-example.yml) · [symbols-example.yml](symbols-example.yml) | Config templates. |

## Per-game coverage

| Doc | Game |
|---|---|
| [mahjong-coverage.md](mahjong-coverage.md) | Mystic Wishes — Mah_001…Mah_108. The **verified reference suite**. |
| [zeus-coverage.md](zeus-coverage.md) | Zappin Zeus — ZZ-01…ZZ-52 (Hold & Spin). |
| [dragon-coverage.md](dragon-coverage.md) | Treasures of the Dragon — DR-01…DR-52, a Zeus fork: Zeus's cases on a `Dragon(Zeus)` profile. **Not yet run** — its env config is incomplete. |
| [dragon-vip-coverage.md](dragon-vip-coverage.md) | Treasures of the Dragon VIP — DV-01…DV-66: Dragon's cases plus Ante Bet and Premium Reel on a `DragonVip(Dragon)` profile, with fixtures re-derived from its own paytable. **Not yet run** — its env config is empty on purpose. |
| [chaos-coverage.md](chaos-coverage.md) | Chaos Runner — 111 cases (payline + ante). |

## History

Session summaries, newest last. Read the latest for current state; the earlier ones explain how the
design got here.

| Doc | Covers |
|---|---|
| [session-summary-scenario-qa.md](session-summary-scenario-qa.md) | 1 — the response-driven model |
| [session-summary-scenario-qa-2.md](session-summary-scenario-qa-2.md) | 2 — coverage build-out, shared video, BO paytables |
| [session-summary-scenario-qa-3.md](session-summary-scenario-qa-3.md) | 3 — MECHANISM + per-game DIALECT split; **the onboarding guide for a new game** |
| [session-summary-scenario-qa-4.md](session-summary-scenario-qa-4.md) | 4 — Zeus onboarded, first live runs |
| [session-summary-scenario-qa-5.md](session-summary-scenario-qa-5.md) | 5 — Chaos Runner onboarded, and a naming question left open |
| [session-summary-scenario-qa-6.md](session-summary-scenario-qa-6.md) | 6 — §6 resolved, a real blocker found, and four rules written down |
| [session-summary-scenario-qa-7.md](session-summary-scenario-qa-7.md) | 7 — four silent-pass bugs, deterministic paytables, and the AI results nobody could see |
| [session-summary-scenario-qa-8.md](session-summary-scenario-qa-8.md) | 8 — the Zeus rewrite, bridge v3, and a bought bonus driven end to end |
| [session-summary-scenario-qa-9.md](session-summary-scenario-qa-9.md) | 9 — Dragon VIP onboarded, Ante Bet + Premium Reel behind one panel, BO denominations · **latest — start here** |

---

## "I need to…" → go here

| Question | Doc |
|---|---|
| What is this platform? | [qa-guide.md](qa-guide.md) |
| How do I add tests for a mechanic? | [scenario-assert-authoring.md](scenario-assert-authoring.md) |
| Why did my `video:` assert fail when the game looked fine? | [assert-traps.md](assert-traps.md) |
| My scenario hung on `waitForSpinClickable` | [Rule 1](scenario-rules/rule-01-freespin-trigger.md) |
| What must the game team implement? | [game-bridge-contract.md](game-bridge-contract.md) §2, §9 |
| What breaks if the bridge omits function X? | [game-bridge-contract.md](game-bridge-contract.md) §7 |
| How do I onboard a new game? | [session-summary-scenario-qa-3.md](session-summary-scenario-qa-3.md), then [game-bridge-contract.md](game-bridge-contract.md) §12 (profile) and §8 (where system buttons live) |
| How do I force a specific outcome? | [bo-integration.md](bo-integration.md) |
| Which cases are automated for game X? | the coverage doc for that game |
| Why is a run slow / how many AI calls does it cost? | [scenario-assert-authoring.md](scenario-assert-authoring.md) — share one clip across cases with `expected_by_case` |
| What happens between two runs? | [run-lifecycle.md](run-lifecycle.md) |
| A run failed in "prepare" / "cleanup" — what is that? | [run-lifecycle.md](run-lifecycle.md) §3, §7 |
| Does the framework play out free spins for me? | [run-lifecycle.md](run-lifecycle.md) §3 — between runs yes, inside a run no |
| Is there already a helper for this? | [scenario-toolkit.md](scenario-toolkit.md) |
| How do I prove something STOPPED? | [Rule 4](scenario-rules/rule-04-prove-stopped.md) — `confirm_stopped()` |
| How do I prove a bonus returned to the base game? | [scenario-toolkit.md](scenario-toolkit.md) §3 — `settle_to_base()` |
| How should a new scenario be laid out? | [scenario-toolkit.md](scenario-toolkit.md) §0 — the template |
