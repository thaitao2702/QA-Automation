# Auto Spin — Shared Mechanism

> Canonical test patterns for the Auto Spin mechanic — automated spin sequences with configurable counts and stop conditions.
> Adapted per game by the write-test skill — do not add game-specific method names here.

---

## 1. Rules

### What It Is
Auto Spin allows a player to configure the game to execute a predetermined number of spins automatically without requiring manual input per spin. The feature is controlled through a configuration popup. Auto-spin runs until the configured count is exhausted, a stop condition is met, or the player manually cancels.

### Configuration
The player configures auto-spin through a popup before starting:
- **Spin count:** A fixed number from a predefined set (e.g. 10, 20, 30, 50) or an "infinite/unlimited" option.
- **Stop-on-win:** An optional flag that halts auto-spin immediately when any spin produces a payout greater than zero. Some games implement a "stop on win greater than X" threshold variant.

### Start Behavior
- Auto-spin begins immediately after the player confirms the configuration — the first spin fires without additional interaction.
- The player's current bet level is locked for the duration of the auto-spin sequence.
- Bet change controls become non-interactable while auto-spin is running.

### Counter Behavior
- An auto-spin remaining counter is displayed while running.
- Each completed spin (including those that trigger bonuses — behavior is game-specific) decrements the counter by one.
- When the counter reaches zero, auto-spin ends silently (no modal required).

### Stop Conditions
- **Manual stop:** Player taps the stop auto-spin button. The currently executing spin completes before auto-spin ends.
- **Stop-on-win:** If enabled, auto-spin halts after any spin with a payout > 0.
- **Bonus triggered:** If a bonus round (Hold & Spin, Free Spin) is triggered during auto-spin, auto-spin must be suspended for the duration of the bonus.
- **Counter exhausted:** Auto-spin ends naturally when the count reaches zero.

### Post-Auto-Spin State
- After auto-spin ends (any reason), the game returns to idle with all controls interactable.
- The player's bet level remains at whatever it was during auto-spin.
- The auto-spin configuration is not retained (next auto-spin requires re-configuration).

---

## 2. Canonical Test Patterns

### Pattern A — Basic Auto-Spin (Fixed Count)
**Scenario:** Player configures a fixed count and runs auto-spin to completion.

Setup:
- Idle state with sufficient balance to cover the chosen count × bet amount.
- Inject non-triggering spin results to avoid bonus interruption.

Steps:
1. Open auto-spin popup.
2. Select a fixed count (e.g. 10).
3. Confirm and start auto-spin.
4. Observe counter as spins execute.
5. Wait for auto-spin to naturally complete.

Assertions:
- Auto-spin executes exactly the configured number of spins.
- Counter decrements by one per spin.
- Counter reaches zero at the end.
- Game returns to idle after completion.
- Balance reflects: starting balance − (count × bet) + sum of all payouts.

---

### Pattern B — Manual Stop During Auto-Spin
**Scenario:** Player stops auto-spin mid-sequence using the stop button.

Setup:
- Start auto-spin with a count large enough to allow intervention (e.g. 50).
- Record counter value while running.

Steps:
1. Start auto-spin.
2. Wait for at least 3 spins to complete.
3. Press the manual stop button.
4. Wait for the in-progress spin to complete.

Assertions:
- Auto-spin stops after the current spin completes (not mid-spin).
- Game returns to idle state.
- All controls become interactable again.
- Counter shows the remaining (unused) spins at the time of stop, then clears.
- Balance correctly reflects only the spins that were actually executed.

---

### Pattern C — Stop-on-Win Condition
**Scenario:** Stop-on-win is enabled; auto-spin halts on the first winning spin.

Setup:
- Start auto-spin with stop-on-win enabled.
- Inject a sequence where the first spin is a no-win, followed by a winning spin.

Steps:
1. Open auto-spin popup.
2. Enable stop-on-win option.
3. Set a spin count of at least 20.
4. Confirm and start.
5. Observe behavior when the first winning spin occurs.

Assertions:
- Auto-spin executes through no-win spins without stopping.
- When a winning spin occurs, auto-spin stops after that spin completes.
- Game returns to idle.
- Remaining auto-spin count is not zero (some spins were unused).
- The winning spin's payout is credited to balance normally.

---

### Pattern D — Stop-on-Win Does Not Stop on Zero Payout
**Scenario:** Stop-on-win must not trigger when a spin pays out zero.

Setup:
- Start auto-spin with stop-on-win enabled.
- Inject multiple consecutive no-win spin results.

Steps:
1. Start auto-spin with stop-on-win enabled.
2. Execute at least 5 consecutive no-win spins.

Assertions:
- Auto-spin continues executing through all no-win spins.
- Stop-on-win has not triggered.
- Counter has decremented correctly for each no-win spin.

---

### Pattern E — Bonus Trigger Suspends Auto-Spin
**Scenario:** A Hold & Spin or Free Spin bonus is triggered during auto-spin.

Setup:
- Start auto-spin with a count large enough to include a bonus trigger.
- Inject a spin result that triggers the bonus.

Steps:
1. Start auto-spin.
2. Trigger the bonus via an injected spin result.
3. Observe auto-spin behavior during the bonus.
4. Complete the bonus round.
5. Observe auto-spin behavior after the bonus ends.

Assertions:
- Auto-spin is suspended (not executing spins) for the duration of the bonus.
- Bonus round completes normally.
- After the bonus ends, the game returns to idle (auto-spin does not automatically resume).
- Auto-spin counter may reflect a deduction for the triggering spin, but no further deductions occurred during the bonus.

---

### Pattern F — Infinite Auto-Spin
**Scenario:** Player selects the "infinite" / unlimited spin count option.

Setup:
- Balance sufficient for many spins.
- Inject non-triggering spin results.

Steps:
1. Open auto-spin popup.
2. Select infinite/unlimited option.
3. Confirm and start.
4. Let at least 10 spins execute.
5. Press the manual stop button.

Assertions:
- Auto-spin runs without a visible countdown (or displays an infinity symbol).
- Stop button is visible and functional.
- Game stops after the current spin completes on manual stop.
- Returns to idle state.

---

### Pattern G — Bet Lock During Auto-Spin
**Scenario:** Bet change controls are non-interactable while auto-spin runs.

Setup:
- Start auto-spin.

Steps:
1. While auto-spin is running, observe bet control states.
2. Attempt to interact with bet controls (if the bridge supports simulating a tap on a disabled button).

Assertions:
- Next bet button is non-interactable.
- Previous bet button is non-interactable.
- Denomination popup cannot be opened.
- Bet amount does not change during auto-spin.

---

### Pattern H — Auto-Spin Interaction with Buy Bonus
**Scenario:** Buy bonus button is disabled during auto-spin.

Setup:
- Start auto-spin.

Steps:
1. Observe buy bonus button state while auto-spin runs.

Assertions:
- Buy bonus button is non-interactable during auto-spin.
- Button returns to interactable after auto-spin ends.

---

## 3. Required Bridge Capabilities

| Capability | Description |
|-----------|-------------|
| Open auto-spin popup | Open the auto-spin configuration popup through the UI code path |
| Configure and start auto-spin | Select spin count and stop-on-win setting, then confirm and start |
| Stop auto-spin | Trigger the manual stop through the UI code path |
| Query auto-spin remaining count | Read the current remaining auto-spin count |
| Query auto-spin active state | Determine whether auto-spin is currently running |
| Query stop-on-win setting | Read whether stop-on-win is currently enabled |
| Query balance | Read current player balance |
| Query bet amount | Read the current locked bet amount during auto-spin |
| Query bet controls interactability | Check whether bet change buttons are interactable |
| Query buy bonus button interactability | Confirm buy bonus is disabled during auto-spin |
| Query last payout | Read the payout from the most recently completed spin |
| Query game state | Read the application state (idle, betting, result, etc.) |
| Inject spin result | Force the next spin to return a specific result (win or no-win) |
| Wait for game idle | Wait until the game is ready for the next action |

---

## 4. Known Edge Cases

### Edge Case 1 — Auto-Spin Count Boundary (Last Spin)
When the auto-spin counter reaches 1 (final spin), the game must execute exactly one more spin and return to idle. A common defect is executing zero additional spins (stopping at 1 without completing) or executing two additional spins (off-by-one). Test the transition from counter = 1 to counter = 0 explicitly.

### Edge Case 2 — Manual Stop Pressed Exactly as Counter Hits Zero
If the player presses the manual stop button at the same moment the last auto-spin completes (race condition), the game must not enter an undefined state. Test rapid stop-button press at the boundary of the auto-spin sequence completing naturally.

### Edge Case 3 — Stop-on-Win with Big Win Animation
When stop-on-win triggers on a big win (with its dedicated celebration animation), auto-spin must wait for the animation to complete before returning to idle. The stop-on-win halts future spins but does not interrupt the current spin's animation sequence. Test that the full big win animation plays out even when stop-on-win would stop the auto-spin.

### Edge Case 4 — Balance Drops Below Bet During Auto-Spin
If the player's balance falls below the bet amount mid-sequence (possible if bet is high and losses accumulate), auto-spin should stop gracefully rather than attempting a spin that would fail. Test the behavior when balance < bet occurs at spin N mid-sequence: does the game stop cleanly, show an error, or attempt the spin and fail?

### Edge Case 5 — Reconnect During Auto-Spin
If the network drops while auto-spin is running, auto-spin must not automatically resume on reconnect. The player must manually restart auto-spin. Test that the auto-spin count is cleared (or set to zero) after reconnect, not retained from before the disconnect.

### Edge Case 6 — Auto-Spin Configured with Count = 0 or Invalid Value
Some implementations have UI edge cases where the spin count could be left at zero or an invalid state. Test the popup's validation: the confirm/start button should be disabled or an error shown if no valid spin count is selected.

### Edge Case 7 — Turbo During Auto-Spin
Turbo mode can typically be toggled during auto-spin. Test: (a) starting auto-spin with turbo on, (b) enabling turbo after auto-spin starts, (c) disabling turbo mid-auto-spin. Verify that turbo state changes take effect for subsequent spins without interrupting the auto-spin sequence.

### Edge Case 8 — Auto-Spin Stop-on-Win with Promo-Granted Free Spins
If stop-on-win is active and a free spin round (with winnings) is triggered or granted during auto-spin, the auto-spin halt behavior depends on game design. Clarify with docs: does the free spin round's payout count as a "win" for stop-on-win purposes, or does stop-on-win only apply to base game spins? This edge case requires explicit confirmation from game documentation.
