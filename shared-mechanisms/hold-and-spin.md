# Hold & Spin — Shared Mechanism

> Canonical test patterns for the Hold & Spin coin-collection bonus mechanic.
> Adapted per game by the write-test skill — do not add game-specific method names here.

---

## 1. Rules

### What It Is
Hold & Spin is a coin-collection bonus round triggered during the base game when a minimum number of coin symbols land simultaneously. The bonus suspends normal reel spinning and enters a re-spin phase where only coin positions and blank reel cells are active.

### Triggering
- A normal base game spin must land at least the minimum required coin symbols (commonly 3 or more).
- The triggering spin result itself counts as the first "reveal" — coins from the trigger spin remain locked in position.
- The bonus begins immediately after the trigger spin's win animations complete (no separate UI navigation required by the player).

### Re-Spin Countdown
- The player starts with a fixed re-spin count (canonically 3) displayed on screen.
- Each re-spin that lands **no new coins** decrements the counter by 1.
- Each re-spin that lands **one or more new coins** resets the counter back to its starting value (e.g. back to 3).
- The bonus ends when the counter reaches zero OR when all grid positions are occupied by coins (grid full).

### Coin Locking
- Coins that land during the bonus (including trigger-spin coins) are locked in their grid positions for all subsequent re-spins.
- Locked coins cannot be replaced, overlaid, or removed by subsequent re-spins.
- Each locked coin carries a value that contributes to the bonus payout.

### Jackpot Tiers
- Some coin positions carry special jackpot values (e.g. Minor, Major, Grand) rather than a numeric coin value.
- A jackpot is awarded when its corresponding coin lands. Jackpot values are fixed and do not accumulate per spin.
- Jackpot tiers are hierarchical — Minor < Major < Grand (< Mega/Ultra if present).

### Bonus Resolution
- When the bonus ends (counter hits zero or grid full), the game totals all locked coin values plus any jackpot awards.
- A bonus result popup/overlay displays the total accumulated win.
- The game returns to normal base game play after the player acknowledges the result (or after an auto-dismiss timeout).

### Grand/Full-Grid Jackpot
- If all grid positions are filled with coins before the counter reaches zero, an additional "Grand" or "full-grid" jackpot award may be granted on top of accumulated coin values (game-specific).

---

## 2. Canonical Test Patterns

### Pattern A — Minimum Trigger (3 Coins)
**Scenario:** Exactly the minimum number of coin symbols lands on the trigger spin.

Setup:
- Start from base game idle state with a known balance.
- Inject a spin result containing exactly the minimum coin count (e.g. 3 coins) in defined grid positions.

Steps:
1. Trigger a spin.
2. Wait for the game to enter the bonus state.
3. Verify the bonus has started: re-spin counter shows its initial value, triggering coins are visible and locked.
4. Verify balance has been deducted by exactly one bet amount (trigger spin cost).

Assertions:
- Re-spin counter equals the starting value (e.g. 3).
- Coin count on the grid equals the number of coins from the trigger spin.
- Locked coin positions match the injected result positions.
- Game state is "in bonus" (not idle).

---

### Pattern B — New Coin Resets Counter
**Scenario:** A re-spin lands a new coin, resetting the countdown to its starting value.

Setup:
- Enter the bonus with a known set of locked coins and a depleted counter (e.g. counter = 1).
- Inject a re-spin result that lands exactly one new coin.

Steps:
1. Execute one re-spin.
2. Observe the counter before and after.

Assertions:
- Counter has reset back to starting value (not decremented to zero).
- New coin is locked in its grid position.
- Total locked coin count has increased by one.
- No bonus-end event has fired.

---

### Pattern C — No New Coin Decrements Counter
**Scenario:** A re-spin lands no new coins.

Setup:
- Enter the bonus with at least one locked coin and counter at its starting value.
- Inject a re-spin result with no coin symbols.

Steps:
1. Execute one re-spin.

Assertions:
- Counter has decremented by exactly one.
- Locked coin positions are unchanged.
- Bonus has not ended (unless counter was 1).

---

### Pattern D — Counter Reaches Zero (Bonus End)
**Scenario:** Counter decrements to zero, ending the bonus.

Setup:
- Enter the bonus with counter = 1 and no new coins in the injected result.

Steps:
1. Execute one re-spin.
2. Wait for bonus-end transition to complete.

Assertions:
- Bonus result UI appears showing the accumulated total.
- Total displayed equals the sum of all locked coin values plus any jackpot awards.
- After acknowledging, game returns to idle/normal base game state.
- Balance has increased by the accumulated bonus payout.

---

### Pattern E — Grid Full Ends Bonus Early
**Scenario:** All grid positions are filled before the counter reaches zero.

Setup:
- Enter the bonus with enough coins already locked that only one position remains empty.
- Inject a re-spin result that fills the last position.

Steps:
1. Execute the final re-spin.

Assertions:
- Bonus ends immediately after the grid fills (does not wait for counter).
- All grid positions show locked coins.
- Grand/full-grid award (if applicable) is included in the total payout.
- Balance increases by total accumulated value.

---

### Pattern F — Jackpot Coin Award
**Scenario:** A jackpot-tier coin (e.g. Major) lands during a re-spin.

Setup:
- Start a bonus with standard locked coins.
- Inject a re-spin result that includes a Major jackpot coin in a specific position.

Steps:
1. Execute the re-spin.
2. Observe jackpot animation/notification.

Assertions:
- Jackpot coin is locked in its grid position.
- Jackpot value is reflected in the running total (if a running total is displayed).
- Upon bonus end, the final payout includes the jackpot fixed value.

---

### Pattern G — Buy Bonus Entry (if applicable)
**Scenario:** Hold & Spin is entered via Buy Bonus rather than natural trigger.

Setup:
- Sufficient balance to cover the buy bonus package cost.
- Open buy bonus popup and select a package.

Steps:
1. Confirm the buy bonus purchase.
2. Wait for the bonus to start.

Assertions:
- Game enters the bonus state.
- Initial coins are present on the grid (guaranteed by the purchase).
- Re-spin counter shows starting value.
- Balance has been deducted by the package cost.

---

## 3. Required Bridge Capabilities

The game's test bridge must expose these abstract capabilities. Exact method names vary per game.

| Capability | Description |
|-----------|-------------|
| Query bonus active state | Determine whether the game is currently in a Hold & Spin bonus round |
| Query re-spin counter | Read the current remaining re-spin count |
| Query locked coin positions | Read all currently locked coin positions (row, col, value, type) |
| Query accumulated bonus payout | Read the running total of coin values collected so far |
| Query grid occupancy | Determine how many grid cells are occupied by locked coins |
| Inject coin-containing spin result | Force the next spin to return a result with specified coin positions and types |
| Inject no-coin spin result | Force the next spin to return a result with no coins |
| Query bonus state (spin state enum) | Read whether the game is in START_BONUS, IN_BONUS, or END_BONUS |
| Query balance | Read current player balance |
| Trigger spin action | Execute a spin through the normal UI code path |
| Wait for game idle | Wait until the game is ready for the next action |
| Query last payout | Read the payout from the most recently completed spin |

---

## 4. Known Edge Cases

### Edge Case 1 — Interrupted Mid-Bonus (Reconnect During Re-Spin)
If the player loses connectivity while a re-spin is in progress, the game must recover to the correct bonus state on reconnect. The server must restore: the current locked coin positions, the remaining re-spin count, and the accumulated payout so far. A re-spin that was in-flight must be completed, not re-submitted. Tests should verify the post-reconnect state matches what would have been the case had no disconnect occurred.

### Edge Case 2 — Session Restore from Pending State
If the player closes the browser mid-bonus and reopens the game, the server's `gameinfo` response may contain a `pending` object with the bonus state. The game must resume the bonus (not restart it) using the pending data. Verify that locked coin positions, counter value, and accumulated payout are restored correctly and that the game does not charge another bet before resuming.

### Edge Case 3 — Counter Reset on Trigger Spin Itself
When the trigger spin lands coins, the counter immediately shows its starting value (e.g. 3). Test that this initial display is correct even before any re-spins occur. A common defect is the counter showing 0 briefly during the trigger animation before being set.

### Edge Case 4 — Simultaneous Jackpot and Regular Coins in One Re-Spin
A single re-spin may land both a jackpot-tier coin and regular coins in different positions. Both must be locked, and both values must be included in the accumulated total. Test that neither value is dropped or double-counted.

### Edge Case 5 — Maximum Coin Count (Grid Full with Varying Jackpot Distribution)
When the grid fills with a mix of regular and jackpot coins, verify the final payout is the arithmetically correct sum of all individual values. Floating-point rounding on currency display is a common defect at this boundary.

### Edge Case 6 — Turbo Mode During Bonus
Some games force turbo mode on or off during the Hold & Spin bonus. Test: (a) entering the bonus with turbo off — is turbo state preserved or forced? (b) manually toggling turbo during re-spins — is the setting respected or ignored during bonus?

### Edge Case 7 — Auto-Spin Interaction
If auto-spin is active when the Hold & Spin trigger occurs, the bonus must pause or terminate auto-spin mode. Verify the auto-spin counter is set to zero or auto-spin is suspended once the bonus begins. After the bonus ends, verify the game returns to the expected state (idle, not automatically continuing auto-spin with the pre-bonus count).

### Edge Case 8 — Buy Bonus Disabled During Bonus
The buy bonus button should become non-interactable once a Hold & Spin bonus is active. Verify interactability changes at bonus start and restores at bonus end.
