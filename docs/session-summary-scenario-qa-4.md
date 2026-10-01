# Session Summary 4 — Zeus onboarded, and the first real live runs

Continues `session-summary-scenario-qa-3.md` (which split the engine into MECHANISM + per-game
DIALECT and is still the onboarding guide for a NEW GAME — **read that first**).

This session: added a second mechanism (Hold & Spin), rewrote Zeus's 52 cases onto the scenario
engine, and — for the first time since the suite was built — **ran it against a live game**. That
turned up one blocking bridge defect and several of my own wrong assumptions, all now fixed.

---

## 1. Where things stand

| | |
|---|---|
| Scenarios | **44** in `scripts/scenario_engine.py` |
| Invariants | **10** (4 general + 6 Hold & Spin) |
| Profiles | `mystic` (cascade) · `zeus` (hold-spin) · `chaos` |
| Suites | Mystic 12 · Zeus 4 · Chaos 5 — all parse, 0 broken fact references |
| Live-verified | Zeus: `base-spin` ✅ green · `base-win` ✅ green · `boot-base`, `spin-then-history`, `open-tutorial`, `browse-denominations` reach and record |
| Blocked | **Zeus bridge defect D1** — see `docs/zeus-bridge-defects.md` |

**The one thing to action:** `docs/zeus-bridge-defects.md` **D1**. One value (`autoSpinsRemaining`
is `-1` instead of `0` on a fresh session) hangs `startGame()` and makes every popup close itself
after ~0.4s. It blocks roughly half the Zeus suite. Everything else here is done.

---

## 2. What was built

### Hold & Spin as a first-class mechanism

`game_profile.py` now reads `SlotGame` → `CascadeGame` / `HoldSpinGame`. Zeus's whole feature rule
set lives in the BET response (`coinPositions`, `coinCount`, `freeSpinInfo`), so it grades **exactly**
— no AI. Six invariants, each tested against a deliberately broken stream:

`holdspin_award` · `holdspin_coins_held` · `holdspin_reset_on_new_coin` · `holdspin_countdown` ·
`holdspin_ends_correctly` · `holdspin_payout_matches_coins`

These replace the `verifyHoldSpin*` analysers that bridge v2 deliberately dropped ("the framework
owns the logic").

### Zeus suites — 49 of 52 cases

`zz-core.yml` · `zz-autoplay.yml` · `zz-holdspin.yml` · `zz-input-sound-reconnect.yml`.
Coverage map: `docs/zeus-coverage.md`. Not automated: ZZ-10 (BO history endpoint unknown), ZZ-12
(needs a symbol-select affordance), ZZ-23 (tab-switch mute), ZZ-04 (see defect D3).

### Buttons are addressed by ROLE

Scenarios used to spell the reference build's literal names (`stakeIncrease`, `auto:count:4`) and
lean on a translation shim. **70 call sites** were converted:

```python
await ctx.click_button(GAME.button("stake_up"))            # was "stakeIncrease"
await ctx.click_button(GAME.button("auto_count", i=idx))   # was f"auto:count:{idx}"
```

Zero literals remain. Behaviour differences are handled too — `auto_stop_button()` picks Zeus's
`stopAuto` vs Mystic's `auto`, because on Zeus clicking `auto` *opens* a popup and the run would
have continued while the case reported a stop.

### `probe_bridge.py`

`python scripts/probe_bridge.py --game zeus --env dev` prints the required-API check, the snapshot,
whether `waitForSpinClickable` resolves, the buttons the build exposes, and profile-vs-reality.
**Reach for this first on any new game or any hang** — it turned "it hangs" into an exact answer in
one run.

---

## 3. What the live runs corrected

Every one of these was an assumption that looked fine on paper and was wrong in the game.

| Assumed | Reality | Fix |
|---|---|---|
| `await page.evaluate(startGame())` is safe | its promise never resolves on Zeus → the run hung forever with no output | `se.start_game()` races the promise against a readiness poll |
| Zeus has no `gameState`; derive it | `gameState` is present (`2` trigger, `1` terminal) | derivation removed |
| `totalFreespin`, no `baseStake` | both are the standard `totalFreeSpin` / `baseStake` | field map corrected |
| a trigger = 3+ coins on the grid | the **terminal** round also has 9 coins, with base state and no respins | `is_trigger()` checks state/counters first |
| ZZ-06 → `base-cluster-win` | that needs `cascades >= 2`, which a payline game never produces — it burned its whole budget | new mechanism-agnostic `base-win`; `base-cluster-win` now refuses a non-cascade profile |
| outcome hunt by "cluster wins" | always 0 on a payline game — 80 spins matched nothing | `win_steps()`: cascade steps on a cascade game, one-per-paying-round elsewhere |
| "nothing opens the bet popup" | `nextBet` **does** open it — the first probe polled 1.5s later, by which time defect D1 had closed it | superseded; see D1 |

### Two vacuous passes caught

Both would have reported green while testing nothing:

- **`browse-denominations`** passed its three `stream:` asserts *without ever opening a list* — the
  denominations come from config, so they were true regardless. It now verifies the list actually
  appeared (`open_bet_list()` checks `visiblePopups`) and reports **unreached** otherwise.
- **`spin-then-history`** would grade an empty History panel. It returns unreached when no row renders.

This is the recurring lesson from sessions 2–4: **every AI or config-derived assert needs a hard
companion proving the run reached what the case claims to judge.**

---

## 4. Working practices worth keeping

- **Probe, don't infer.** `probe_bridge.py` and one-off Playwright probes resolved in minutes what
  doc-reading got wrong repeatedly. The bet-popup question flipped twice before a 150ms poll settled it.
- **Verify a doc's claim before repeating it.** `bridge-api-v2.md` §10 says `nextBet` opens the bet
  popup — true, but only visibly for 0.3s. An earlier contract entry claimed `holdSpinStart/End`
  backed Mah_022; grep showed nothing calls it.
- **Test the failure direction.** Every invariant was driven with a broken stream to confirm it fires
  for its own defect *and nothing else*.
- **Dwells:** `ui_dwell_ms = 2000` after a settled UI change, `step_dwell_ms = 1000` for an
  intermediate step of a multi-step interaction, `0` only where back-to-back is the point (Mah_108).
- **Patch files by reading them back.** A string-replacement patch left a stray bare `return` in
  `BrowseDenominations` that returned `None`. There is now an AST check for that, and the runner
  names the scenario instead of raising `AttributeError`.

---

## 5. Picking this up

1. **Send `docs/zeus-bridge-defects.md` to the game team.** D1 is the blocker; D2/D3 are small.
2. **Once D1 lands**, re-run and expect ZZ-02/03 plus every popup case to start working:
   ```
   python scripts/run_scenario.py --game zeus --env dev --suite games/zeus/test-suites/zz-core.yml
   ```
   A client-side workaround exists (run and stop one autoplay after load, which sets
   `autoSpinsRemaining` to 0) but is **not wired in** — it places real bets every session, and the
   product fix is one line.
3. **Then run `zz-holdspin.yml`** — the Hold & Spin invariants have never executed against the live
   game. They need BO paytable fixtures (`fs-trigger3-countdown` / `-addcoin` / `fs-trigger9`); if BO
   is unconfigured the runs skip rather than run unforced.
4. **Confirm two Zeus unknowns** listed in `docs/zeus-coverage.md`: the autoplay package counts
   (`AUTO_COUNTS` still carries the default 10/20/30/50/∞) and the Buy Bonus price multiplier
   (ZZ-40 asserts only `price_ratio > 0`).
5. **Mystic is still bridge-less** — its dev build ships without `window.__gameTestBridge`, so its
   ~50 cases remain unverified live. Same first step: `probe_bridge.py --game mystic`.

### Reference map

| Need | File |
|---|---|
| Onboard a new game | `docs/session-summary-scenario-qa-3.md` |
| What a bridge must expose | `docs/game-bridge-contract.md` |
| Zeus response shape | `games/zeus/bet-response.md` (+ `fixtures/bet-responses/`) |
| Mystic response shape | `games/mystic/bet-response.md` |
| Case coverage | `docs/zeus-coverage.md` · `docs/mahjong-coverage.md` |
| Bridge defects to chase | `docs/zeus-bridge-defects.md` |
