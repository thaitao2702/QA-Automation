# Session Summary 12 — Grandma Pets onboarded end to end, and the framework bugs it exposed

Continues `session-summary-scenario-qa-11.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — read that first if you are adding one.

Summary 11 left Joker at 62 cases, none of them run. This session onboarded a **seventh game** from
nothing — no profile, no captures, no fixtures — to **103 cases across 6 suites that validate
clean**, and ran parts of it live. Every live run so far has found a **framework** bug and nothing
about the game, which is the normal shape of a first game but worth saying plainly: **no Grandma
case has produced a trustworthy verdict yet.**

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game grandma      # OK — 103 cases, 99 of 168 matrix rows
python tests/chaos/validate_suites.py --game chaos        # OK
python tests/chaos/validate_suites.py --game dragon       # OK
python tests/chaos/validate_suites.py --game dragon-vip   # OK
python tests/chaos/validate_suites.py --game zeus         # OK
python tests/chaos/validate_suites.py --game mystic       # 3 known duplicate ids (Mah_012/044/045)
python tests/chaos/validate_suites.py --game joker        # 2 known duplicate ids (JK-08/16, summary 11 §1)
python tests/chaos/build_scenario_index.py                # 50 scenarios · 33 invariants, docs in sync
```

> ⚠ **Still not in git. 113 paths**, last commit `bb01fa6` — the same warning summaries 8 … 11
> carried, now one session older and a whole game larger. **Commit before anything else.**

**The one thing to finish first** is in §5: the big-win skip is one console line away from being
solved, and that line is in your next run's output.

---

## 1. What Grandma Pets is, and where its rules live

A **1024-ways** game (4 rows × 5 reels) — the first ways game in the repo. Wild multiplier per
round, walking wilds in free spins, a 75× Buy Bonus, a Free Spin Extension sold at the end of every
bonus, a Cat vs Dog drama, a reel-glow anticipation, and Shake/Swirl.

| File | What it settles |
|---|---|
| `games/grandma/bet-response.md` | the payload, verified against 5 real captures. **Read this before writing any assert** |
| `games/grandma/bridge-api.md` | the bridge surface (the game team extended it mid-session with the drama) |
| `games/grandma/paytables/README.md` | every fixture, the command that made it, and why |
| `docs/grandma-coverage.md` | the 69 matrix rows NOT covered, and why — do not re-derive |
| `games/grandma/test-suites/gm-103-156.plan.md` | the plan, at v6 |

### The four things this payload spells differently, each of which fails SILENTLY

1. **`view`, not `views`** — a profile inheriting the plural reads nothing.
2. **No `gameState`.** It has `state` (where the round was played) and `nextState` (where it goes).
   The profile maps `F_GAME_STATE` to **`nextState`**: with `state`, the Buy Bonus purchase (played
   in base, `state: 1`) reads as terminal and the drain stops **before a single free spin**.
3. **`wonFreeSpins` is the award**; `freeSpins` is remaining and `totalFreeSpin` is the bonus total.
   The inherited `is_trigger` answered True for *every* free spin.
4. **`package` does not identify a purchase** — it is on the purchase AND every free spin of that
   bonus. `subType == 5` does.

⚠ **Two captured wins disagree with `docs/raw/paytable.json`**: id 18 (`P5`) pays P4's row, id 4
(`K`) pays A's row. Not blocking — fixtures only move reel WEIGHTS, and no assert compares a payout
to that file — but a live export would settle it, and every forced run carries a guard that asserts
what actually landed.

## 2. The suites — 103 cases, 6 files

| Suite | Runs | Cases | What |
|---|---|---|---|
| `gm-core.yml` | 10 | 16 | boot, denominations, bet, balance, out of money, history, paytable, tutorial, language |
| `gm-autoplay.yml` | 7 | 12 | every way an auto-play run starts and ends |
| `gm-input-sound-reconnect.yml` | 8 | 17 | spin gestures, turbo, sound, reconnect, second tab |
| `gm-lastbet.yml` | 4 | 4 | close and reopen — restore, and resume with spins owed |
| `gm-forced.yml` | 9 | 32 | free-spin awards, reel glow, Cat vs Dog drama (BO fixtures) |
| `gm-real-weights.yml` | 4 | 22 | Buy Bonus, walking wild — **no BO at all** |

⚠ **Why two suites and not one.** Once a suite selects the automation paytable, a run WITHOUT a
fixture inherits whatever the last fixture left in it (summary 11 §3). `gm-real-weights.yml` pushes
no paytable anywhere, which is the only way "the game's own weights" is true.

**Every AI assert is VIDEO, never a still**, and each run names a clip window covering only the
rounds its cases are about. Where a verdict needs a value, the AI **reads** it and a function
compares — it is never shown the expected answer.

## 3. What was built in the engine

New, in `scripts/`:

| | |
|---|---|
| `WaysGame` + `Grandma` profile | subclasses `LineGame` for the per-reel census; clears the line divisor, 1024 ways, `steps()` returns the round |
| `ways_payout_matches` invariant | `symbolMultiplier × totalWildMultiplier × wayCount × baseStake` — exact on all 8 captured entries, catches 5 mutations, fails rather than passing vacuously |
| `freespin-reload`, `walking-wild`, `drama` | the three new scenarios (50 in the registry now) |
| `purchase_bonus()` | extracted from `buy-bonus` so the new scenarios reach a bought bonus the same way |
| `sample_purchase_reveal()` | the Buy Bonus teaser exists ONLY on the client (the server sends `-1`), so it is sampled from `getDisplayedGrid()` while it plays |
| `answer_extension_offer()` / `decline_extension_offer()` | see §4 |
| `skip_big_win()` / `big_win_threshold()` | see §5 |
| `watch_presentation()` / `take_presentation_trace()` | 50 ms in-page sampling — a wild mid-walk or a control that flickers enabled cannot be seen from Python, because every `await` is a gap |
| `derive_paytable.py --zero` | sets a symbol to 0 on the TARGET reels, the inverse of `--zero-elsewhere`. Four fixtures need it |

Extended: `fs-complete` gained `arrive_when` (hunt a trigger of a particular SHAPE and play out the
ones that do not match) and the spin-button counter check; `reels-glow` exposes what landed;
`auto-play` watches the controls that must stay locked; `buy-bonus` drains by mechanism.

### Four validator checks added, each for a bug this session actually hit

1. **`video.at` arity** — a 3-element span raises `TypeError` at run time, *after* the bonus played.
2. **Ungraded AI fields** — a field the prompt asks for that no case grades (warning: discovery
   fields are legitimate). Found the same pattern in 4 other games.
3. **`{facts.x}` in a PROMPT that the scenario does not expose** — renders the literal `"null"` into
   the text the model reads, and looks perfectly fine in the file.
4. **`ui_read` with parameterised JS and no `arg`** — now raises. See §5.

## 4. The Free Spin Extension — three attempts, and why

Every bonus ends on an offer panel that holds the spin button. It took three goes:

1. A single read the moment the drain returned. **The panel fades in AFTER that**, so it always said
   "not open" and a teardown swept it up.
2. Poll, bailing out when `canDo('spin').ok`. **Spin is briefly clickable in the gap** before the
   popup appears, so the poll exited on its first pass.
3. What works: the **round** says whether to expect an offer (`freeSpins == 0` with `nextState == 2`),
   two independent signals are watched (the state getter AND the panel's name in `visiblePopups`),
   it waits for the panel to actually go (it holds up to 5 s), collects the reply, and skips the
   result popup — which `dismissPopups()` never force-closes.

⚠ It lives in **`drain_to_base`** as well as `play_out_freespins`: a resumed bonus plays out inside
`start_game`, which calls `assert_idle` itself, so a scenario answering afterwards is already late.

## 5. ⚠ THE OPEN THREAD — the big-win skip

Celebrations cost seconds per round and minutes per run, for a presentation nothing grades.
`skip_big_win()` reads the threshold from `gameInfo.settings.winFormula` (**6× the bet** — bands are
multiples of the bet, not money), asks the RESPONSE whether the round qualifies, then waits for the
banner and taps twice.

Fixed along the way: a 4 s wait that missed a banner arriving at 6.5 s (a ways game presents
`winWays` → `wildMultiplier` → count-up FIRST — it now waits until the banner appears **or the round
finishes presenting**); `winFormula` read from `settings.general` as well as the alias; a `1e-9`
tolerance because `0.6 / 0.1` is `5.999999999999999` and a 6× win read as not-big.

> ## ✅ ANSWERED 2026-09-24 — `offscreen:bigWin:skip`. It is a bridge defect, not ours.
>
> The control **resolves** (it is in `getAllButtonRects()` keys) but the bridge reports its centre
> **outside the canvas**, so it is permanently unclickable — not a race. Filed as **G1** in
> `docs/grandma-bridge-defects.md`, with the hypothesis: it is a full-screen tap layer like
> `freeSpinResult:skip`, which the bridge already special-cases to return an on-canvas edge point,
> and `bigWin:skip` was not given the same treatment. **Nothing to fix in this framework** — the
> name, the timing and the call were all correct. Celebrations will keep playing in full until the
> bridge fixes it. The diagnostic now also prints the rect and the canvas size, so the next run
> carries the numbers the game team asks for first.

**Where it stood:** the threshold resolves, the banner is detected, and then

```
big win : `bigWin:skip` did not become clickable -- reason: <THIS LINE IS THE ANSWER>
```

`bigWin:skip` is in **no §7 table** — the name came from the game team. `waitForButtonClickable`
rejects with one of six reasons and the next run prints it:

| reason | means | fix |
|---|---|---|
| `unknown_control:…` | the build has no such control | get the real name (the console now lists every control resolving at that moment) |
| `blocked_by:<node>` | **the banner covers its own skip button** — my best guess | click the coordinates without the hit-test, or ask for the layer to accept the tap |
| `not_interactable` / `offscreen` | it exists but cannot be used | game-side |
| `no_geometry` / `nothing_hit` | the engine could not place it | bridge defect, per the doc |

`scripts/probe_big_win.py` answers all of it in one run without a suite. ⚠ It bets.

> **The lesson worth carrying.** A `canDo` I added reported `not_available:null` — because
> `ctx.ui_read()` passes **no argument** to the page, so the JS ran with `undefined` and answered
> confidently about a control named null. The same shape had already cost a run through the
> extension poll's `(opts)`. `ui_read` now **raises** on parameterised JS with no `arg`.

## 6. What is left

1. **Commit.** 113 paths.
2. **Finish the big-win skip** (§5) — one console line.
3. **Run the suites** and read the verdicts with suspicion. Order: `gm-core` → `gm-real-weights` →
   `gm-forced`. Nothing has produced a trustworthy verdict yet.
4. **Verify `bo.paytable_code` and `bo.game_id: 280`** on BO. `paytable_code` is unset, so the runner
   falls back to `AutomationTest` — Zeus's spelling. Summary 11 §9: this is the failure that still
   reports success.
5. **The grid read + compare** (`plan` §0 P7): the AI returns one 4×5 grid per named round and the
   framework compares it to `view`. TC-149 and TC-125 wait on it; a video `read` compares one value
   today.
6. **Ask for three bridge additions** (`docs/grandma-coverage.md` §5), in value order:
   `getReelGlowState()` (makes 8 existing cases exact instead of AI-only), Shake/Swirl state
   (unblocks 12 rows), Energy Trail state (4 rows).
7. **File the four bridge/doc defects found:** the `Promo` popup at boot is in no §7 list; §8.3 says
   the Buy Bonus purchase carries the server's real grid and it is five reels of `-1`; `bigWin:skip`
   is undocumented; `BigWinPopup` has no named skip control in §7.
8. **Two spec conflicts to settle with the game team**, recorded in the suites rather than guessed:
   TC-122 (the matrix says every symbol of the loser's type vanishes; the client removes only the
   fakes) and TC-126/127 (the matrix needs a real adjacent Cat+Dog; the client fabricates the loser).
   TC-104, TC-114, TC-126 and TC-139 were **removed** in review — see the plan.

## 7. Where the time went, and what it says

Six live runs; six framework bugs; zero game findings so far:

| The run said | It was |
|---|---|
| the extension popup hung | a single read taken before the panel existed |
| the clip showed no award | a round's window ends when the NEXT round starts, and a free spin's start is recorded when we begin WAITING (now **Rule 21**) |
| `0 == 8` on the FS counter | reading the button when the response landed, before the client redrew it |
| `0 >= 1` on walking-wild transitions | asserting a server-decided event off ONE bonus |
| `abs is not defined` | inline predicates run with no builtins |
| big wins played in full | four separate causes, §5 |

Every one produced a plausible-looking red on a game that was behaving correctly, which is the
failure mode this framework exists to avoid. **The validator checks in §3 exist so the next six are
caught before a run, not after one.**
