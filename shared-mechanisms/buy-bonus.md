# Buy Bonus — Shared Mechanism

> Canonical test patterns for the Buy Bonus direct-purchase mechanic.
> Adapted per game by the write-test skill — do not add game-specific method names here.

---

## 1. Rules

### What It Is
Buy Bonus allows a player to directly purchase entry into a bonus round (typically Hold & Spin or Free Spin) without waiting for a natural trigger during base game play. The player selects a package from a popup, pays a premium cost relative to their current bet, and the game immediately transitions into the purchased bonus mode.

### Triggering
- The player clicks the Buy Bonus button while in idle base game state.
- A popup presents one or more packages with descriptive names, spin counts or outcome types, and costs.
- The player selects a package and confirms the purchase via a confirmation step.
- The game sends the purchase request to the server.

### Package Structure
- Each package has a fixed cost expressed as a multiplier of the current bet level (e.g. 80x, 100x, 200x the bet amount).
- Packages may differ in guaranteed outcome type (e.g. guaranteed Hold & Spin entry with varying minimum coin counts).
- Package costs and contents are server-defined and should be read from game config, not hardcoded.

### Balance Requirements
- Buy Bonus is only available if the player's balance is sufficient to cover the most expensive package at the current bet level.
- Some games disable buy bonus if balance is below any package cost at the current bet (game-specific).
- The buy bonus button is non-interactable when any of the above conditions are not met.

### Restrictions
- Buy Bonus is disabled during: free spin rounds, auto-spin mode, promotional mode, and whenever the game is not in idle state.
- Buy Bonus is unavailable at bet levels where the package cost would exceed the available balance.

### Purchase Flow
1. Player opens the buy bonus popup.
2. Player selects a package (taps the package card or a Buy button).
3. A confirmation dialog appears requiring explicit confirm action.
4. On confirm, the game deducts the package cost from balance and sends the purchase request.
5. Server responds with a bonus result (guaranteed coin positions, free spins, etc.).
6. The game enters the bonus flow as if the bonus had been naturally triggered.

### Cancellation
- The player can close the popup or dismiss the confirmation step without completing the purchase.
- Dismissal must not deduct any balance.
- The game must return to idle state after dismissal.

---

## 2. Canonical Test Patterns

### Pattern A — Successful Purchase, Cheapest Package
**Scenario:** Player successfully buys the cheapest package with sufficient balance.

Setup:
- Idle state with a balance greater than the cheapest package cost at the current bet.

Steps:
1. Open the buy bonus popup.
2. Select the cheapest package.
3. Confirm the purchase.
4. Wait for the bonus to begin.

Assertions:
- Balance deducted by exactly the package cost (bet × package multiplier).
- Game enters bonus mode (Hold & Spin or Free Spin, per package type).
- Bonus state reflects the server-provided result (coin positions or spin count).
- Buy bonus button is non-interactable during the bonus round.

---

### Pattern B — Successful Purchase, Most Expensive Package
**Scenario:** Player buys the highest-cost package.

Setup:
- Balance sufficient for the most expensive package cost.

Steps:
1. Open the buy bonus popup.
2. Select the most expensive package.
3. Confirm the purchase.

Assertions:
- Balance deducted by the correct higher cost.
- Bonus entered with the outcome type corresponding to the most expensive package.
- No additional bet deduction occurs during the bonus itself.

---

### Pattern C — Cancellation at Popup Stage
**Scenario:** Player opens popup, then closes it without selecting or confirming.

Setup:
- Idle state with sufficient balance.

Steps:
1. Open the buy bonus popup.
2. Close the popup without selecting a package.

Assertions:
- Balance unchanged.
- Game returns to idle state.
- Buy bonus button becomes interactable again.

---

### Pattern D — Cancellation at Confirmation Stage
**Scenario:** Player selects a package, reaches the confirmation step, then cancels.

Setup:
- Idle state with sufficient balance.

Steps:
1. Open the buy bonus popup.
2. Select a package (triggering the confirmation dialog).
3. Cancel/dismiss the confirmation.

Assertions:
- Balance unchanged.
- Game returns to idle state (or back to the package selection step — confirm game-specific behavior).
- No server request has been sent.

---

### Pattern E — Insufficient Balance
**Scenario:** Player's balance is below the cost of the selected package.

Setup:
- Balance less than the cost of the cheapest package at the current bet level.

Steps:
1. Attempt to open the buy bonus popup or observe button state.

Assertions:
- Buy bonus button is non-interactable (disabled) before opening the popup.
- OR: if popup can be opened, confirm-purchase control is disabled or shows an error when balance is insufficient.
- Balance unchanged if any purchase attempt is made.

---

### Pattern F — Buy Bonus Disabled During Auto-Spin
**Scenario:** Buy bonus button is unavailable while auto-spin is running.

Setup:
- Start auto-spin with a valid package count.

Steps:
1. Observe buy bonus button state during auto-spin.

Assertions:
- Buy bonus button is non-interactable while auto-spin is active.
- Button becomes interactable again after auto-spin ends or is manually stopped.

---

### Pattern G — Bonus Outcome Matches Package
**Scenario:** Verify the bonus type delivered matches the package selected.

Setup:
- Two packages available: one entering Hold & Spin, one entering Free Spins.

Steps:
1. Purchase the Hold & Spin package.
2. Verify the bonus type entered.
3. (In a separate test) Purchase the Free Spin package.
4. Verify free spin counter appears.

Assertions:
- For Hold & Spin package: game enters coin-collection bonus, re-spin counter visible.
- For Free Spin package: game enters free spin mode, free spin counter visible.
- Each package's guaranteed outcome matches what was advertised in the popup.

---

## 3. Required Bridge Capabilities

| Capability | Description |
|-----------|-------------|
| Query buy bonus button interactability | Check whether the buy bonus button is currently interactable |
| Query available packages | Read the list of available buy bonus packages with costs and types |
| Open buy bonus popup | Trigger the buy bonus popup through the UI code path |
| Select package by index | Select a specific package card by its position in the package list |
| Confirm purchase | Confirm the purchase through the confirmation dialog |
| Cancel/dismiss popup | Dismiss the popup or confirmation without confirming |
| Query balance | Read current player balance |
| Query current bet amount | Read the current bet level for cost calculation |
| Query game state | Determine whether the game is in idle, bonus, free spin, etc. |
| Query spin state | Read whether the game has transitioned into bonus (START_BONUS, IN_BONUS) or free spin |
| Wait for game idle | Wait until the game is ready for the next action |
| Query bonus active state | Confirm whether a bonus round is currently in progress |

---

## 4. Known Edge Cases

### Edge Case 1 — Bet Level Change Between Popup Open and Confirm
If a player opens the buy bonus popup, another process (e.g. auto-bet or a rapidly tapped bet change) changes the bet level before confirmation, the package cost in the popup may be stale. Test: (a) whether the popup re-calculates package costs on bet change while open, (b) whether the server validates the confirmed cost against current bet, (c) whether a mismatch is handled gracefully (error shown, no double-deduction).

### Edge Case 2 — Purchase During Transitional App State
If the player rapidly taps confirm while the game is transitioning out of idle (e.g. a background event or reconnect is in progress), the purchase request may be sent from a non-idle state. Test that the server rejects or queues the request correctly and that the balance is not double-deducted.

### Edge Case 3 — Network Failure After Confirm but Before Server Response
If the network drops immediately after the purchase confirmation is sent but before the server responds, the client is in an ambiguous state. On reconnect, the server must inform the client whether the purchase succeeded (via pending state in `gameinfo`). Test that the client does not re-submit the purchase and correctly resumes the purchased bonus if it was acknowledged by the server.

### Edge Case 4 — Balance Exactly Equal to Package Cost
When the player's balance is precisely equal to a package cost, the purchase should succeed and leave the balance at zero. Test that: (a) the buy button is enabled (not just "strictly greater than"), (b) after purchase the balance is zero (not negative), (c) subsequent spin controls correctly reflect the zero-balance condition.

### Edge Case 5 — Buy Bonus Button Visibility at Different Bet Levels
At the minimum bet level, the cheapest package may be affordable but the most expensive may not. Verify the popup correctly enables/disables individual packages based on affordability, not just the button's master enable state.

### Edge Case 6 — Concurrent Promo and Buy Bonus
If a promotional free spin award is active or pending, buy bonus should be disabled. Test that the buy bonus button is non-interactable immediately when a promo activates and remains so for the duration of the promo round.

### Edge Case 7 — Package List Rendered from Server Data
Package labels, costs, and spin counts are server-defined. If the server returns a different package count than expected (e.g. A/B test, regional rules), the UI should render dynamically without hardcoded assumptions. Test with game config scenarios where the package count is at its minimum (1 package) and maximum (e.g. 4 packages).
