---
game: zeus
generated: 2026-04-12
source_docs:
  - docs/raw/Doc Zappin zeus.md
  - bridge-api.md
bridge_api: bridge-api.md
shared_mechanisms:
  - hold-and-spin: uses (3+ coins trigger, 3 re-spins, jackpot tiers Grand/Major/Minor)
  - buy-bonus: uses (purchasable guaranteed Hold & Spin entry)
  - auto-spin: uses (packages 10/20/30/50/Infinity, stop-by-win)
  - reconnect: uses (server-authoritative restore, pending bonus state)
  - free-spin: not used (no scatter-triggered free spin rounds; game has no separate free spin mechanic)
---

# Zeus Slot Game — Game Knowledge

## 1. Overview

| Property | Value |
|----------|-------|
| Game name | Zappin Zeus |
| Theme | Greek mythology — Zeus, crow, cup, ring, time |
| Grid | 3 rows x 3 columns (9 cells) |
| Pay lines | 5 fixed paylines (left-to-right) |
| Per Line winnings | (bet amount / 5) * Multiplier |
| Engine | Cocos Creator 3.8.2 (WebGL canvas) |
| Backend | SignalR WebSocket + MessagePack |
| RTP | Not yet available |
| Decimal places | 2 |

---

## 2. Symbols

### Active Symbols

| Key | Doc ID | Bridge ID | Name | Type | Mapping (Doc) | Payout Notes |
|-----|--------|-----------|------|------|---------------|--------------|
| wild | 01 | 9 | Wild | Substitution | Zues (Golden Eagle) | Substitutes for all symbols except Coin |
| coin | 03 | 13 | Coin | Scatter (Exclude wild) | Coin | Hold&Spin trigger — 3+ coins start bonus. Carries random face value (Coin Multiplier x total bet) |
| p1 | 05 | 5 | P1 | Normal (Include wild) | Crow | Premium 1. See paytable |
| p2 | 06 | 6 | P2 | Normal (Include wild) | Cup | Premium 2. See paytable |
| p3 | 07 | 7 | P3 | Normal (Include wild) | Ring | Premium 3. See paytable |
| p4 | 08 | 8 | P4 | Normal (Include wild) | Time | Premium 4. See paytable |
| ace | 09 | 1 | A | Normal (Include wild) | Pentagon | Card symbol. See paytable |
| king | 10 | 4 | K | Normal (Include wild) | Diamon | Card symbol. See paytable |
| queen | 11 | 3 | Q | Normal (Include wild) | Triange | Card symbol. See paytable |

### Unused Symbols (flagged, not active in game)

| Key | Doc ID | Bridge ID | Name | Type | Reason |
|-----|--------|-----------|------|------|--------|
| freespin | 02 | — | FreeSpin | Scatter (Exclude wild) | Flagged as unused. Game has no separate free spin mechanic. |
| jackpot | 04 | — | Jackpot | Normal (Include wild) | Flagged as unused. Jackpots handled as coin types within Hold & Spin (MINOR/MAJOR/GRAND). |
| jack | 12 | 2 | J | Normal (Include wild) | Disabled in config, weight 0. Never appears in results. |

### Coin Types (Hold & Spin)

| Type | ID | Meaning | Multiplier |
|------|----|---------|------------|
| EMPTY | 0 | No coin | — |
| NORMAL | 1 | Regular coin | Variable (from Coin Value List) |
| MINOR | 2 | Minor jackpot coin | 50x total bet |
| MAJOR | 3 | Major jackpot coin | 100x total bet |
| GRAND | 4 | Grand jackpot coin | 500x total bet |

**Coin Value Notes:**
- Coin Value = Coin Multiplier x total bet amount.
- The Coin Value List differs between Hold & Spin feature and Main Game.
- The top 3 payout values (500x, 100x, 50x) are displayed on their respective Grand, Major, and Minor symbols.

### Win Type Thresholds

Win type is determined by `payout / betAmount` ratio:

| Win Type | Multiplier Range | Celebration |
|----------|-----------------|-------------|
| LOSE | payout = 0 | None |
| Slim Win | 0 < x < 1 | Minimal highlight |
| Small Win | 1 <= x < 2.5 | Win line animation |
| Medium Win | 2.5 <= x < 10 | Enhanced animation |
| Big Win | 10 <= x < 25 | Full-screen popup |
| Mega Win | 25 <= x < 50 | Popup + trophy button |
| Ultra Win | x >= 50 | Popup + trophy button |

---

## 3. Paylines

5 fixed paylines on a 3x3 grid. All symbols pay from left to right on adjacent reels starting from the leftmost reel, except Coin which pays on any reel. Payline coordinates defined in `Constants.ts:PayLinesPosition`. Standard 3x3 configuration:
- Payline 1: Middle row (cells [1,0], [1,1], [1,2])
- Payline 2: Top row (cells [0,0], [0,1], [0,2])
- Payline 3: Bottom row (cells [2,0], [2,1], [2,2])
- Payline 4: Diagonal top-left to bottom-right
- Payline 5: Diagonal bottom-left to top-right

---

## 4. Mechanics

### 4.1 Hold & Spin Bonus

See also: `shared-mechanisms/hold-and-spin.md`

**Trigger:** 3 or more COIN symbols (ID=13) anywhere on the 3x3 grid during a normal base-game spin.

**Zeus-specific implementation:**
- The triggering spin counts as the first reveal — trigger-spin coins are immediately locked.
- SpinState at trigger = `START_BONUS`; re-spins = `IN_BONUS`; final = `END_BONUS`.
- Re-spin count starts at **3**, resets to 3 on any re-spin that lands >=1 new coin, decrements on no-coin re-spins.
- The standard 3x3 play window (9 positions) transforms into 9 independent 1x1 play windows. Each position results in either a Coin (win) or Blank (no coin).
- Coins are locked for all subsequent re-spins (cannot be replaced).
- Only Coin or Blank symbols appear during Hold & Spin mode.
- Bonus ends when counter = 0 OR all 9 cells are filled.
- If 9 coins land in the Main Game, Hold & Spin starts but spinning is skipped — coin values are immediately added to the Coin Win Meter and awarded.
- Hold & Spin has **no retrigger function** — it simply resets the spin counter when new coins land.

**Coin types:**
| Type | ID | Description |
|------|----|-------------|
| NORMAL | 1 | Standard coin with numeric value (variable multiplier) |
| MINOR | 2 | Fixed Minor jackpot award (50x total bet) |
| MAJOR | 3 | Fixed Major jackpot award (100x total bet) |
| GRAND | 4 | Highest fixed jackpot award (500x total bet) |

**Resolution:** Total of all locked coin values + jackpot awards credited to balance. Coins collected are added to the Coin Win Meter and awarded to the player. Bonus result popup shown. SpinState returns to `NORMAL`.

**Bridge methods for Hold & Spin:**
- `getFreeSpinsRemaining()` — current re-spin counter (0-3)
- `getCoinPositions()` — array of `{row, col, coinValue, type}` for all locked coins
- `getSpinState()` — `START_BONUS | IN_BONUS | END_BONUS | NORMAL`
- `setDevModeResult(grid)` — inject a 3x3 result to control coin positions

---

### 4.2 Buy Bonus

See also: `shared-mechanisms/buy-bonus.md`

Player purchases a guaranteed Hold & Spin bonus entry ("Mua 1 lan hold and Spin"). Packages defined in `GameInfo.freeSpinPackages` — each has `{name, freeSpins, price}`. The package cost is `bet x package.price`. Entering via buy bonus is functionally identical to a natural trigger once the bonus starts.

**State flow:** `idle -> betting -> result -> START_BONUS -> IN_BONUS (re-spins) -> END_BONUS -> idle`

**Bridge methods:**
- `isBuyBonusEnabled()` — false during promo, auto-spin, and bonus
- `openBuyBonusPopup()` — opens BuyBousPopup
- `confirmBuyBonus(packageIndex)` — sends `buyfreespin` event; resolves when bonus completes
- `getFreeSpinPackages()` — array of available packages

---

### 4.3 Auto-Spin

See also: `shared-mechanisms/auto-spin.md`

**Packages:** 10, 20, 30, 50, Infinity (continuous).
**Stop conditions:** (a) manual stop, (b) stop-by-win when `payout > 0`, (c) bonus trigger.
**Auto-spin is suspended** when Hold & Spin bonus begins; does not auto-resume after bonus.

**Bridge methods:**
- `startAutoSpin(count, stopByWin)` — begins session; count in {10, 20, 30, 50, Infinity}
- `stopAutoSpin()` — cancels current session
- `getAutoSpinsRemaining()` — remaining spins (Infinity for continuous)
- `isStopAutoVisible()` — true when an auto session is active
- `isAutoEnabled()` — false during promo and during spinning

---

### 4.4 Reconnect

See also: `shared-mechanisms/reconnect.md`

- Network loss -> AppState -> `reconnecting` or `error`.
- Restore -> SignalR reconnects -> server sends fresh `gameinfo` with authoritative balance.
- If bonus was active mid-disconnect: `GameInfo.pending` = `{coinPositions, freeSpins, stake}`.
- Game resumes bonus from pending state — no re-bet charged.
- Session timeout configured via `GameInfo.settings.sessionTimeOut`.

**Bridge methods:**
- `getAppState()` — use to detect reconnecting/error states
- `getSnapshot()` — full state dump for pre/post comparison

---

### 4.5 Turbo Mode

- Shortens reel animation duration. Toggle via `setTurbo(enabled)`.
- Toggle button names in code are **inverted**: `offTurboButton.isChecked = true` = turbo ON.
- Turbo is temporarily forced ON during hold-spin-start (long press), then restored.
- Turbo may be forced OFF and locked during promo mode.

---

### 4.6 Promotions

- Server-initiated: `gameinfo.promo` or `startPromo` SignalR event.
- During promo: bet locked, buy bonus disabled, turbo restricted, SpinState = `PROMO`.
- `isPromoMode()` -> `GameLogic.isPromoMode`.
- Not in scope for initial test suites (requires server-side promo configuration).

---

### 4.7 Custom Mechanics (Game-Specific)

**Reel Glow**

Visual anticipation effect that highlights reels when a bonus trigger is close.

Trigger conditions:
1. If 2+ Coin symbols appear in Reel 1 -> Reel Glow appears on Reel 2 and Reel 3.
2. If 2+ Coin symbols appear in Reel 1 and Reel 2 -> Reel Glow appears on Reel 3.
3. If Wild icons on Reel 1 and Reel 2 form a payline -> Reel Glow appears on Reel 3.
4. When Reel Glow appears, main icons with potential to trigger the effect are highlighted; non-triggering icons are dimmed.

> Note: No bridge API method to query or verify glow state. May require visual/screenshot-based testing.

**Multiple Distribution List Rotation**

Multiple symbol distribution tables used for gameplay variation. Each table defines a unique symbol probability layout per reel.

Base Game rotation triggers:
- Game launch (first-time entry)
- Buy Bonus
- Game mode changes (entering/exiting Buy Bonus or Free Spin mode from Main Game)
- Spin counter reaching predefined threshold X (e.g. X=10)

Free Game/Buy Bonus rotation triggers:
- Entering Buy Bonus
- Entering Free Spins

Counter reset rules:
- On game start: all counters reset.
- From Bonus/Free Game back to Base Game: counter X is reset.
- From Base Game to Bonus/Free Game and return: Base Game counter retains value.
- Game restart: all counters reset.
- Bonus/Free Game modes do not use a spin counter for rotation.
- If game is restarted while in Free Game or Bonus mode, distribution list remains unchanged.

> Note: Server-side mechanic. No bridge method to query current distribution table. Testable only through statistical analysis or server-side verification.

**Info Bar Text**

Context-aware messages displayed in the info bar with Max Win Cap behavior.

| Status | Text | Display Behavior |
|--------|------|-----------------|
| A. WIN | Win amount (with Max Win Cap: shows capped amount + "Max Win reached") | Stays till player/Auto spin again |
| B. LOSE | Randomized encouraging text (e.g. "Relax and enjoy the spin!") | Stays till player/Auto spin again |
| C. SPIN | Randomized spin text (e.g. "Best of luck!") | Only appears when player/Auto spins |
| D. IDLE | Randomized idle text (e.g. "Hold For Quick Spin!") | Appears at game start or after X minutes idle |
| E. TOTAL WIN RESULT | Total win amount (with Max Win Cap: shows capped total + "Max Win reached") | Stays till player/Auto spin again |
| F. WINNING IN A ROW | Consecutive win streak text (3 wins, 4 wins, 5+ wins) | During consecutive win sequences |

Scenarios:
- Normal spin: A, B, C, D, F (with 3-second rotation for consecutive wins)
- Freespin scenario: A(case1), B, C, E, F
- Turbo (normal): A, B, E
- Turbo (Freespin): A(case1), B, E

**Max Win Cap**
- Game Limit Set By Operator = Maximum Win Per Bet TKN = Max Win Cap.
- Affects both individual spin wins (status A) and total bonus round wins (status E).
- When cap is reached, displayed amount is capped and "Max Win reached" is shown.

---

## 5. Bridge API Summary

Full reference: `bridge-api.md`

### Actions (21 total)
`spin`, `stopSpin`, `nextBet`, `previousBet`, `selectBet`, `openAutoPopup`, `startAutoSpin`, `stopAutoSpin`, `openBuyBonusPopup`, `confirmBuyBonus`, `setTurbo`, `setSound`, `openSettings`, `openInfo`, `openHistory`, `holdSpinStart`, `holdSpinRelease`, `skipBonusTransition`, `skipBigWin`, `openDevMode`, `openTrophy`

### Queries (grouped)
**Game state:** `getAppState`, `getSpinState`, `getBalance`, `getBetAmount`, `getBetIndex`, `getDenominations`, `getFreeSpinsRemaining`, `getAutoSpinsRemaining`, `getLastPayout`, `getLastWinType`, `isTurboActive`, `isSoundEnabled`, `isMusicEnabled`, `isPromoMode`

**Reel state:** `getReelResult`, `getCoinPositions`, `getPaylines`

**Button interactability:** `isSpinEnabled`, `isBuyBonusEnabled`, `isAutoEnabled`, `isNextBetEnabled`, `isPreviousBetEnabled`, `isStopAutoVisible`

**Game info:** `getGameInfo`, `getCurrency`, `getPaytable`, `getFreeSpinPackages`

**Display values:** `getDisplayBalance`, `getDisplayBet`, `getDisplayWin`

**Snapshot:** `getSnapshot`

### Event Collection
`startEventCollection`, `stopEventCollection`, `drainEvents` — see bridge-api.md section 5 for event types.

### Wait Helpers
`waitForState(state, timeoutMs)`, `waitForIdle`, `waitForSpinComplete`

### Activation
URL param `?bridge=1`. Registered at `window.__gameTestBridge`. Zero overhead in production.

---

## 6. Test Coverage Map

| Mechanism | Suites | Bridge Methods Used |
|-----------|--------|---------------------|
| Core UI & Load | suite-01 | getAppState, isSpinEnabled, getBetAmount, getBalance, getBetIndex, selectBet, openInfo |
| Balance math | suite-02 | getBalance, getBetAmount, getLastPayout, setDevModeResult |
| Auto-spin | suite-03 | startAutoSpin, stopAutoSpin, getAutoSpinsRemaining, isStopAutoVisible, isAutoEnabled |
| Turbo / Sound | suite-04 | setTurbo, isTurboActive, setSound, isSoundEnabled, isMusicEnabled |
| Reconnect | suite-05 | getAppState, getBalance, isSpinEnabled, getSpinState, isStopAutoVisible |
| Hold & Spin | suite-06 | spin, getSpinState, getFreeSpinsRemaining, getCoinPositions, setDevModeResult, resetDevMode |
| Buy Bonus | suite-07 | isBuyBonusEnabled, openBuyBonusPopup, confirmBuyBonus, getSpinState, startAutoSpin, stopAutoSpin |
| Spin mechanics | suite-08 | spin, stopSpin, getReelResult, getPaylines, getLastPayout, getLastWinType, setDevModeResult |
| Language/Tutorial | suite-09 | getDisplayBalance, getDisplayBet, openInfo |

---

## 7. Known Limitations & Out-of-Scope Items

| Item | Reason |
|------|--------|
| Free Spin mechanic | Game does not have a separate free spin round. FreeSpin symbol (doc ID 02) flagged as unused. |
| Jackpot symbol (doc ID 04) | Flagged as unused. Jackpots are coin types (MINOR/MAJOR/GRAND) within Hold & Spin. |
| J symbol (doc ID 12, bridge ID 2) | Disabled in config, weight 0. Cannot be tested via real spin. |
| Perceived Persistence (Feature 6) | Not implemented. Doc placeholder only ("Mo ta Perceived Persistence ?"). |
| RTP | Not yet available. |
| Reel Glow visual verification | No bridge API method to query glow state. Requires visual/screenshot-based testing. |
| Multiple Distribution Rotation | Server-side mechanic. No bridge method to query current distribution table. |
| Promo mode tests | Require server-side promo configuration; not in current test suites. |
| Persistence level visual tests | No bridge method exposes current persistence level; would need bridge extension. |
| ZZ-07, ZZ-08, ZZ-16 | Deferred — require specific test environment configuration not yet available. |
| ZZ-10 | Out of scope — excluded from automation. |
| Cross-game language sync | Out of scope. |
| Network latency/stability | "Stable" is subjective; automation covers no-crash only. |
| Multiplier/cascade mechanics | Not present in this game (5-payline 3x3 traditional slot, no cascade). |

---

## 8. DevMode Usage Notes

`setDevModeResult(grid)` accepts a 3x3 array of symbol IDs. The injected result is consumed on the next `spin()` call; subsequent spins use server responses. Always call `resetDevMode()` in teardown.

**Example grids:**
```
All-losing (no paylines):  [[1,3,4],[3,4,1],[4,1,3]]
3x King middle row (win):  [[1,3,1],[4,4,4],[3,1,3]]
3x Zeus middle row (BIG):  [[1,3,1],[7,7,7],[3,1,3]]
3 coins bottom row:        [[1,3,4],[3,4,1],[13,13,13]]
Full grid coins (9):       [[13,13,13],[13,13,13],[13,13,13]]
```

---

## 9. Source Documentation Index

| File | Type | Key Information |
|------|------|-----------------|
| Doc Zappin zeus.md | MD | Main game doc: symbol mapping (12 symbols, 3 unused), base game rules (left-to-right pay, 5 paylines, coin-any-reel), features (Coin Value with Grand/Major/Minor multipliers, Hold & Spin with 3x3->9x1x1 transformation, Free Spin [not implemented], Buy Bonus [one H&S session], Reel Glow anticipation, Perceived Persistence [not implemented], Win Tier Multiplier, Info Bar Text with Max Win Cap, Multiple Distribution List Rotation). Contains English and Vietnamese text. |
| bridge-api.md | MD | Full test bridge API: 21 actions, 30+ queries, wait helpers, event collection, state machine reference, symbol IDs (Constants.ts), win type thresholds (Interface.ts), game config, key TypeScript interfaces. |
