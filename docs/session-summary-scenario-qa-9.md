# Session Summary 9 — Dragon VIP onboarded, two paid features, and four live findings

Continues `session-summary-scenario-qa-8.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — read that first if you are adding one.

This session onboarded **Treasures of the Dragon VIP**: Dragon's 50 cases plus **Ante Bet** and
**Premium Reel**, the first game whose features live behind ONE panel instead of one button each.
That forced the first real engine work since Zeus (`switch_feature`, `premium-reel-spin`, two
invariants), and the first live runs produced four findings — three of them about the environment,
not the game. It also added back-office **denomination** support (`docs/bo-denominations.md`).

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game dragon-vip   # 63 cases, OK
python tests/chaos/validate_suites.py --game dragon       # 50 cases, OK
python tests/chaos/validate_suites.py --game zeus         # 50 cases, OK
python tests/chaos/validate_suites.py --game chaos        # 77 cases, OK
python tests/chaos/validate_suites.py --game mystic       # 3 known duplicate-id errors (unchanged, §11 of summary 8)
python tests/chaos/build_scenario_index.py                # 44 scenarios · 28 invariants, docs in sync
```

> ⚠ **The framework is still not in git.** `scripts/scenario_engine.py`, `scripts/game_profile.py`,
> `scripts/derive_paytable.py`, both `tests/chaos/*.py` checkers, all of `docs/` and the whole of
> `games/dragon-vip/` are UNTRACKED — 91 paths at last count, last commit `bb01fa6`. Summary 8 said
> the same thing and it is still true. **Commit before doing anything else.**

Three things block the next live run, in order:

1. **`BO_TOKEN` / `BO_REFRESH_TOKEN` in `.env.dev` are expired** (`Unauthorized`). The access token
   lives 20 minutes and refreshing needs one that has not lapsed, so they must be re-pasted from the
   browser. Nothing BO-driven runs without them — that includes every forced-paytable suite.
2. **Premium Reel is switched off for this session** (§4.2), so `dv-premium-reel.yml` reports
   unreached. BO has to enable it for the test player/currency.
3. **No BET response has been captured** from the VIP build, so the response field names are
   documented-but-unverified (§6).

---

## 1. Where things stand

| | |
|---|---|
| Dragon VIP | **63 cases** across 6 suites. Not automated: DV-10 (History on BO — manual), DV-23 (tab-switch sound), DV-56 (ante toast — §4.3) |
| Dragon · Zeus · Chaos | 50 · 50 · 77 — unchanged this session |
| Engine | **44 scenarios · 28 invariants** (was 43 · 26) |
| Profiles | `DragonVip(Dragon)` registered; `mystic`, `zeus`, `dragon`, `dragon-vip`, `chaos` |
| Config | `games/dragon-vip/env.config.dev.yml` now filled in: local build on `192.168.1.27:7456`, game code `SL_33_TREASURES_DRAGON_VIP`, BO game 283, currency TKN (31) |
| Git | nothing committed; last commit `bb01fa6` |

---

## 2. Dragon VIP — what was written

Suites in `games/dragon-vip/test-suites/` (`test.yml` is the user's scratch copy, skipped by the
validator):

| Suite | Cases | Notes |
|---|---|---|
| `dv-core.yml` | DV-01…12, 51, 52 | mirrors `dr-core.yml`; DV-52 also asks whether the help explains the two paid features |
| `dv-autoplay.yml` | DV-08, 13…21 | mirrors `dr-autoplay.yml`; DV-15 rewritten this session (§3.4) |
| `dv-holdspin.yml` | DV-30…43, 66 | Dragon's Hold & Spin + Buy Bonus; **DV-66 is new** — a bought bonus never carries a Premium Reel |
| `dv-input-sound-reconnect.yml` | DV-22, 24…29, 44…50 | mirrors Dragon's |
| `dv-ante.yml` | DV-53…55, 57, 58 | **new** — Ante Bet through the panel, and a Hold & Spin bet with ante |
| `dv-premium-reel.yml` | DV-59…65 | **new** — every package, and a Hold & Spin bet with one |

**Fixtures are re-derived, not copied** (`games/dragon-vip/paytables/`, README has the commands). The
VIP paytable is Dragon's plus `AnteBet*` / `PremiumReel_*` sections **and different BaseGame
weights**, so Dragon's fixtures would push the wrong base game and drop both features from BO. Six
files: the four Dragon paths plus `ante-trigger3-countdown` and `premium-trigger3-countdown`, which
force COIN only in the feature's OWN reel section — so a trigger proves the server really spun the
ante/premium reels.

Coverage doc: `docs/dragon-vip-coverage.md`.

---

## 3. Engine changes

### 3.1 Paid features: one panel, not one button

The VIP build has no `ante` toggle. Ante Bet, Premium Reel and Buy Bonus are rows in one panel behind
the feature button, and that same button turns the active feature off. Three clicks on, one click off.

- **`switch_feature(ctx, "ante" | "premium:<reelId>", on)`** — one helper, both UI systems: a
  one-click toggle (Chaos) or the panel flow (VIP). It SWITCHES rather than blind-toggles, and
  samples `visiblePopups` for ~2.5 s after the last click so a feature toast is caught.
- `ante-toggle` now goes through it, so Chaos and VIP share the scenario. Added `spin_after_off`
  (a plain spin after the feature goes off) and the exclusion probes.
- **`hold-spin-complete` gained `feature:`** — plays the whole Hold & Spin as a bet WITH ante or a
  package: switches on, refuses to hunt if a spin does not carry it, samples the off control across
  the entire drain (`_watch_controls`), switches back off after the evidence is captured.
- **`premium-reel-spin`** (new scenario) — plain spin, then each configured package: on, probe what
  it excludes, spin, off; then a plain spin again.
- **`premium_reel_stake`** and **`premium_reel_frozen_in_freespins`** (new invariants).
- `buy-bonus` now treats `buy_purchase` as optional (VIP's package row raises the confirm sheet
  itself) and scrolls a row into view first.
- Profile: `base_stake` / `ante_multiplier` / `is_ante_round` lifted to `SlotGame`; new
  `premium_reel_id` / `premium_reel_multiplier`, `HAS_PREMIUM_REEL`, roles `ante_on` / `ante_off` /
  `premium_reel:<reelId>` / `premium_off` / `buy_next` / `buy_prev`, popup `ante_notify`.

### 3.2 `conservation` and the `-1` balance

VIP's Hold & Spin respins report `balance: -1` and only the terminal round carries the real figure.
Read literally that reported two violations on a feature whose money was exactly right. `conservation`
now treats a **non-positive balance as "not reported"**, carries that round's stake and payout
forward, and makes the next reported round account for them — so the check is no weaker. Written up
as **Rule 19** in `docs/scenario-authoring-rules.md`.

### 3.3 `derive_paytable.py`

Now handles **list-valued sections** (`PremiumReel_BaseGame` holds one per package), comma-separated
`--section`, and `--coin-weight N --coin-section S` for Hold & Spin respin weights.

### 3.4 `auto-stop-at-win` — the clip started after the win

DV-15's clip opened *after* the winning spin had settled. Two causes: the win landed on the first auto
spin, and the framework's 2 s dwell on `auto:confirm` swallowed that spin (the client fires it on the
click, so the response was collected from the buffer and the round's window opened late). Fixes:
confirm no longer dwells; a new `lead_in` mark opens the clip up to `lead_in_rounds` (3) spins before
the win; `min_spins_before_win` restarts auto when the win comes too early to show anything.
`resolve_window` now accepts a mark that is a MOMENT (`{"t": seconds}`) rather than a round.

---

## 4. Live findings

### 4.1 The dev-play build has no bridge

`https://dev-play.royaledge.io/sl_33_treasures_dragon_vip/index.html` loads and plays, `bridge=1` is
in the URL, but `window.__gameTestBridge` never appears and `__gameTestBridgeError` is not set —
and none of the 18 loaded JS files contains `__gameTestBridge`. The bridge is simply not in that
build. The env config now points `base_url` at the local build instead.

### 4.2 Premium Reel is not offered on this session

`getGameInfo().settings.premiumReel` is `null` while `anteMultiplier` is `1.5`, so
`premiumReelAvailable` is false, `getPremiumReelOptions()` is `[]`, and every
`canDo('activatePremiumReels:*')` answers `feature_unavailable:premiumReels`. The paytable file
enables the feature (`PremiumReel_Setting.Enable: true`), so the **BO record for this player/currency
disagrees with it** — a question for BO/product. Until then `dv-premium-reel.yml` is unreached
(inconclusive, not red), and DV-55 asserts only the panel half.

### 4.3 No ante notification

`visibleNotifications` stays empty across an ante switch. The bridge doc only *reserves* the
`anteNotify` label (§5, §6.8) and never says switching ante raises it — unlike its `turbo` row, which
says so outright. So this is a **design question, not a doc-vs-build conflict**: should VIP confirm
ante with a toast the way Chaos does, given it already shows a confirm sheet? DV-56 was dropped; the
facts are still exposed.

### 4.4 What did run

`hold-spin-complete` with `feature: ante` reached a full feature: trigger charged 10.50 = 7 × 1.5,
three respins, `holdspin_*` invariants green, and the AI confirmed the coins held and the feature
control hidden during the bonus. That run is what surfaced both the `-1` balance and the
inverse-pair prompt bug (§5).

---

## 5. Prompt bug worth remembering

The DV-57/58 and DV-64/65 prompts asked "is the feature still shown as ON?" **and** "is its off
control locked?" — one physical property asked twice in opposite directions (the inverse-pair trap,
`docs/scenario-assert-authoring.md` §2). The model answered it plainly: *"the Ante Bet control (the
'DISABLE' button) is hidden while the bonus is active, so it is both not shown and effectively
locked."* The first field was also never graded by any case. Both prompts now ask two distinct
properties — the price on screen at the trigger, and the off control's absence during the respins —
and describe the one-panel UI so the model is not looking for a button this build does not have.

---

## 6. Open questions for the game / server team

Full list in `docs/dragon-vip-coverage.md`. The ones that block or skew a case:

| Question | Effect |
|---|---|
| Does a Premium Reel bonus round echo `stake = baseStake × multiplier` (client doc §4) or `0` (§3.3)? | If a non-zero stake is echoed but not debited, DV-64's `conservation` goes red. The contract contradicts itself |
| Does `balance: -1` on a bonus round mean "unchanged / not sent"? | Assumed from arithmetic; not in any doc. Capture it in `games/dragon-vip/bet-response.md` |
| Is `anteMultiplier` the BET response's spelling? (`pending` uses `anteMulti`) | `ante_stake_multiplier` and `{facts.ante_engaged}` depend on it |
| Does a feature stay ON after its bonus ends? | Exposed as `{facts.feature_on_after_bonus}`, not asserted |
| Why is `settings.premiumReel` null when the paytable enables it? | Blocks 7 cases (§4.2) |

---

## 7. Back-office denominations (new)

`framework/bo/denominations.py` + `docs/bo-denominations.md`: read a pair's bet ladders, create one,
edit one, and assign it to the master game or to one agent's game row —
`ensure_agent_automation_denomination()` does the whole setup in one call and returns the previous id
for teardown.

Three traps are encoded there: `denomination/update` is a **full replace** (every existing entry must
be resubmitted), a **new entry is the one with no `id`**, and the list/update endpoints spell the
sub-category differently (`gameSubCategoryId` vs `subCategoryId`). `defaultBetLevel` is a 1-based
index into `betLevels`.

**Tested offline against captured payloads; never run live** — the BO token was expired all session.

---

## 8. Docs written or changed

| Doc | What |
|---|---|
| `docs/dragon-vip-coverage.md` | **new** — the game, its 63 cases, what the first live runs found, open items |
| `docs/bo-denominations.md` | **new** — the denomination API, functions and traps |
| `docs/scenario-authoring-rules.md` | **Rule 19** — a field that says NOTHING is not a value (the `-1` balance) |
| `docs/game-bridge-contract.md` | §8 Features rewritten as **two UI systems**; §4 paid-feature snapshot fields; §6 sentinel rule + feature echo; §7 panel helpers |
| `docs/scenario-index.md` · `docs/scenario-toolkit.md` | the new scenario, invariants and `switch_feature`; counts refreshed |
| `docs/bo-integration.md` | §7 denominations |
| `games/dragon-vip/paytables/README.md` | **new** — why fixtures are derived, with the commands |

---

## 9. What to do next

1. **Commit.** 91 untracked paths, including the whole engine.
2. **Refresh the BO tokens**, then re-run the forced suites (`dv-holdspin.yml`, `dv-ante.yml`).
3. **Capture BET responses** into `games/dragon-vip/fixtures/bet-responses/` — one plain, one ante,
   one premium, one respin — and write `games/dragon-vip/bet-response.md`. That settles three of the
   five open questions in §6 at once.
4. **Ask BO to enable Premium Reel** for the test player/currency, then run `dv-premium-reel.yml`.
5. **Run the denomination flow live** once a token is in place: read the list first, then
   `ensure_agent_automation_denomination` and its teardown.
6. **Re-run DV-15** to confirm the lead-in clip shows auto spinning before the win.
