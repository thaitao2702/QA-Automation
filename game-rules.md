# Zeus Slot Game — Game Rules

> Source document for game-knowledge.md generation.
> Derived from bridge-api.md, Constants.ts, Interface.ts, and project-overview.md.

---

## 1. Grid & Paylines

| Property | Value |
|----------|-------|
| Grid size | 3 rows × 3 columns (9 cells total) |
| Pay lines | 5 fixed paylines |
| Decimal places | 2 |
| Currency | Server-configured per session (e.g., USD, EUR, THB) |

### Payline Definitions

The 5 paylines are fixed positions on the 3×3 grid. Wins are paid left-to-right. The exact coordinate mapping is defined in `PayLinesPosition` (Constants.ts). Typical 3×3 configurations cover: top row, middle row, bottom row, diagonal top-left to bottom-right, and diagonal bottom-left to top-right.

---

## 2. Symbols

| Key | ID | Name | Type | Notes |
|-----|----|------|------|-------|
| A | 1 | Ace | Low | Card symbol |
| J | 2 | Jack | Low | Disabled in server config (weight = 0); never appears in results |
| Q | 3 | Queen | Low | Card symbol |
| K | 4 | King | Low | Card symbol |
| P1 | 5 | Athena | High | Premium 1 — highest-paying after Wild |
| P2 | 6 | Helmet | High | Premium 2 — Greek helmet |
| P3 | 7 | Zeus | High | Premium 3 — Zeus portrait |
| P4 | 8 | Shield | High | Premium 4 — Greek shield |
| Wild | 9 | Golden Eagle | Wild | Substitutes for all symbols except Coin |
| Coin | 13 | Coin | Bonus | Landing 3+ triggers Hold & Spin bonus |
| COIN_EMPTY | -1 | Empty Coin Slot | Internal | Placeholder for empty bonus grid positions |
| ERROR | -1 | Error | Internal | Invalid/error state symbol |

### Win Type Thresholds (multiplier of bet amount)

| Win Type | Multiplier Range | Visual |
|----------|-----------------|--------|
| LOSE | payout = 0 | No animation |
| SLIM_WIN | 0 < x < 1 | Minimal highlight |
| SMALL_WIN | 1 ≤ x < 2.5 | Win line animation |
| MEDIUM_WIN | 2.5 ≤ x < 10 | Enhanced win animation |
| BIG_WIN | 10 ≤ x < 25 | Big win popup |
| MEGA_WIN | 25 ≤ x < 50 | Mega win popup + trophy button |
| ULTRA_WIN | x ≥ 50 | Ultra win popup + trophy button |

---

## 3. Hold & Spin Bonus

### Trigger

- During a normal base-game spin, landing **3 or more COIN symbols** (ID=13) anywhere on the 3×3 grid triggers the Hold & Spin bonus.
- The trigger spin itself is counted as the first reveal — coins from the trigger spin are immediately locked.
- SpinState transitions to `START_BONUS` on the triggering spin.

### Re-Spin Phase

- Player begins with **3 re-spins** (`freeSpins = 3`).
- Each re-spin that lands **no new coins** decrements the counter by 1.
- Each re-spin that lands **one or more new coins** resets the counter back to **3**.
- The bonus ends when the counter reaches 0 **or** all 9 grid positions are filled with coins (grid full).
- Re-spin SpinState = `IN_BONUS`.
- Final re-spin (counter → 0 or grid full) = `END_BONUS`.

### Coin Locking

- Coins that land (including trigger-spin coins) are locked in their grid positions for all subsequent re-spins.
- Locked coins carry a value contributing to the bonus payout.
- Coin types: NORMAL (1), MINOR (2), MAJOR (3), GRAND (4).

### Jackpot Tiers

| Type | ID | Description |
|------|----|-------------|
| NORMAL | 1 | Regular coin — carries a numeric value |
| MINOR | 2 | Minor jackpot coin — fixed jackpot award |
| MAJOR | 3 | Major jackpot coin — fixed jackpot award (higher than Minor) |
| GRAND | 4 | Grand jackpot coin — highest fixed award |

### Bonus Resolution

- When the bonus ends, the game totals all locked coin values plus any jackpot awards.
- A bonus result popup displays the total accumulated win.
- Balance is credited with the total payout.
- SpinState returns to `NORMAL` and game returns to base game (AppState = `idle`).

### Buy Bonus Entry

- Players can purchase a guaranteed Hold & Spin bonus entry via the Buy Bonus popup.
- SignalR event: `buyfreespin` with `{stake, packageName}`.
- State flow: `idle` → `betting` → `result` → `START_BONUS` → `IN_BONUS` (re-spins) → `END_BONUS` → `idle`.
- `GameLogic.bonusGameType = BONUS_TOTAL_WIN` is set on buy-bonus confirmation.

---

## 4. Persistence Levels

The game features an 8-tier visual progression system (`PersistenceView`). Persistence levels affect the visual appearance of the reels (glow effects) and are tracked via `PersistenceInfo { nextLevel: 1-8, type: UP|LOOP|DOWN|TRIGGER }`. Persistence changes are driven by cumulative play and triggered by `TransitionSpinStateHandler`.

---

## 5. Auto-Spin

- Available packages: 10, 20, 30, 50, Infinity (continuous).
- Configured via `AutoPopup` (package + stop-by-win toggle).
- Stop-by-win: if enabled and a spin has `payout > 0`, auto-spin stops after that spin.
- Manual stop: `stopAutoSpin()` → `GameLogic.numberAutoSpin = 0` → `updateSpinButton()`.
- Auto-spin is suspended/ended when a Hold & Spin bonus begins.

---

## 6. Bet Denominations

- Available denominations come from `GameLogic.denominations` (server-provided).
- Default bet level is `GameInfo.settings.defaultBetLevel`.
- Min/max bet enforced: `selectBet(index)` validates denomination ≤ balance.
- `nextBet()` / `previousBet()` increment/decrement the denomination index.
- All bet changes sync the UI: bet label, denomination popup, buy bonus popup.

---

## 7. Turbo Mode

- When active: reel animation duration is shortened for faster play.
- Toggle: `setTurbo(enabled)` emulates clicking `offTurboButton` (on) or `onTurboButton` (off).
- Note: toggle button names are inverted — `offTurboButton.isChecked = true` means turbo IS active.
- During Hold & Spin (hold-spin-start hold), turbo is forced on, then restored on release.
- During promo mode, turbo may be forced off and the toggle becomes non-interactable.

---

## 8. Sound

- `setSound(enabled)` controls BOTH sound effects and music simultaneously.
- Bridge methods: `isSoundEnabled()` → `SoundManager.isSoundEnabled()`, `isMusicEnabled()` → `SoundManager.isMusicEnabled()`.
- Sound state persists across spins within a session.

---

## 9. Promotions (Free Spins / Countdown)

- Server sends promo data in `gameinfo` response or via `startPromo` event.
- Player accepts promo → bet is locked to promo bet amount → promo spins run.
- SpinState = `PROMO` during promo spins.
- During promo: buy bonus disabled, turbo restricted, bet change disabled.
- Promo ends when spin count or timer expires → `PromoResultPopup` shown.

---

## 10. Reconnect / Session Restore

- If the connection is lost, AppState transitions to `reconnecting` or `error`.
- SignalR auto-reconnect logic handles re-establishing the WebSocket.
- On reconnect, server restores authoritative balance and any pending state.
- If the player was mid-bonus, `GameInfo.pending` contains: `{ coinPositions, freeSpins, stake }`.
- The game resumes the bonus from the pending state — it does not restart or charge another bet.

---

## 11. Application State Machine

```
INITIAL → CONNECTING → LOADING → READY → IDLE ⇄ BETTING ⇄ RESULT
                                            ↕                   ↕
                                          PROMO               ERROR
                                            ↕                   ↕
                                          IDLE          RECONNECTING → IDLE
```

Valid transitions defined in `ApplicationStateManager.ts`.

---

## 12. Bridge Activation

- URL parameter: `?bridge=1`
- Registered at: `window.__gameTestBridge`
- Activated during: `SlotGameApplication.initialize()`
- Production safe: zero overhead when parameter absent.
- Debug logging: additionally pass `?debug=1` for verbose game logs (dev only).
