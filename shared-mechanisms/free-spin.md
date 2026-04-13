# Free Spin — Shared Mechanism

> Canonical test patterns for the Free Spin bonus round mechanic.
> Adapted per game by the write-test skill — do not add game-specific method names here.

---

## 1. Rules

### What It Is
Free Spin is a bonus round in which the player is granted a fixed number of spins that do not deduct from the player's balance. Free spins are awarded by the server and tracked by a remaining-spin counter. The round ends when the counter reaches zero.

### Triggering
Free spins can be awarded through multiple paths:
- **Scatter trigger:** Landing a minimum number of scatter/bonus symbols during a base game spin.
- **Buy Bonus:** Player purchases a free spin package directly (see buy-bonus.md for purchase flow).
- **Promotional grant:** Server grants free spins via a promo event (e.g. a `startPromo` or equivalent signal).
- **Bonus-within-bonus:** Some games allow free spins to be re-triggered by landing scatter symbols during the free spin round itself.

### Spin Count and Configuration
- The number of free spins awarded is determined by the server's response or the purchased package.
- A "free spin count" display is shown prominently during the round.
- The counter decrements by exactly one after each free spin completes.

### Bet Locking
- During free spins, the player's bet level is locked at the value active at the time free spins were triggered or purchased.
- Bet change controls become non-interactable for the duration of the bonus.

### Balance Effect
- Free spins do not deduct from balance. Payouts from free spins are credited to balance normally.
- The server is authoritative on balance — the bridge must reflect server-reported balance, not a client-side calculation.

### Re-Trigger
- If re-trigger is supported, landing scatter symbols during a free spin adds additional spins to the remaining counter (rather than starting a new separate round).
- Re-trigger adds must not reset the accumulation counter — previously won amounts are retained.

### Round End
- When the remaining counter reaches zero and the last free spin completes, the game shows a total accumulated win from the free spin round.
- The game then returns to normal base game play.

---

## 2. Canonical Test Patterns

### Pattern A — Basic Trigger via Scatter
**Scenario:** Minimum scatter count lands, awarding free spins.

Setup:
- Start from idle base game state with a known balance.
- Inject a spin result that lands the minimum scatter count required to trigger free spins.

Steps:
1. Trigger a spin.
2. Wait for the game to enter free spin mode.
3. Observe the free spin counter.

Assertions:
- Free spin counter equals the expected award count.
- Balance deducted by exactly one bet (trigger spin cost only).
- Bet controls are non-interactable.
- Game state indicates free spin mode.

---

### Pattern B — Free Spin Does Not Deduct Balance
**Scenario:** Confirm each free spin executes without balance deduction.

Setup:
- Enter free spin mode with a known starting balance.
- Record balance before each spin.

Steps:
1. Execute one free spin.
2. Record balance after the spin resolves.

Assertions:
- Balance after spin equals: balance before spin + payout from that spin.
- Balance is never lower after a free spin than before it (unless the game uses a deduct-and-credit model — confirm with game docs).

---

### Pattern C — Counter Decrements Correctly
**Scenario:** Each free spin decrements the counter by exactly one.

Setup:
- Enter free spin mode with a known starting count (e.g. 10).

Steps:
1. Record counter value.
2. Execute one free spin.
3. Record counter again.

Assertions:
- Counter decreased by exactly one.
- Counter never jumps or skips values.
- Repeat for at least 3 consecutive spins to detect off-by-one defects.

---

### Pattern D — Round Ends at Counter Zero
**Scenario:** Free spin round ends correctly when the last spin completes.

Setup:
- Enter free spin mode with counter = 1.

Steps:
1. Execute the final free spin.
2. Wait for the round-end transition.

Assertions:
- Round-end UI appears (total accumulated win displayed).
- Displayed total equals the sum of all free spin payouts observed.
- Game returns to base game idle state after acknowledgment.
- Bet controls become interactable again.

---

### Pattern E — Re-Trigger Adds Spins (if applicable)
**Scenario:** Landing scatters during a free spin adds additional free spins.

Setup:
- Enter free spin mode with counter = N.
- Inject a free spin result that contains the minimum re-trigger scatter count.

Steps:
1. Execute the free spin.
2. Observe counter after the result.

Assertions:
- Counter increased by the re-trigger award amount (counter = N - 1 + re-trigger grant).
- No round-end event occurred.
- Previously accumulated winnings are retained.

---

### Pattern F — Buy Bonus Entry into Free Spins
**Scenario:** Player purchases a free spin package to enter the bonus directly.

Setup:
- Known balance sufficient for the chosen package.
- Open buy bonus popup and select a free spin package.

Steps:
1. Confirm the purchase.
2. Wait for free spin mode to begin.

Assertions:
- Free spin counter equals the package's promised spin count.
- Balance deducted by the package price (not a regular bet).
- Bet is locked at the level active during purchase.

---

### Pattern G — Promo Free Spins (if applicable)
**Scenario:** Server-granted promotional free spins are activated correctly.

Setup:
- Simulate a game session where the server provides a promo free spin grant on `gameinfo` (or via a promo event).

Steps:
1. Load the game (or trigger promo event).
2. Accept the promo offer.

Assertions:
- Free spin counter shows the server-granted count.
- Bet amount is the promo-specified amount (player's bet setting is overridden).
- Bet controls remain non-interactable.
- After round ends, game returns to normal and restores the player's original bet setting.

---

## 3. Required Bridge Capabilities

| Capability | Description |
|-----------|-------------|
| Query free spins remaining | Read the current remaining free spin count |
| Query free spin mode active | Determine whether the game is currently in a free spin round |
| Query balance | Read current player balance |
| Query last payout | Read the payout from the most recently completed spin |
| Query accumulated round win | Read total winnings accumulated so far in the current free spin round |
| Query bet amount | Read the current locked bet amount during free spins |
| Query bet controls interactability | Check whether bet change buttons are interactable |
| Query spin state | Read the game's spin state (normal, in-bonus, promo, etc.) |
| Inject scatter-containing spin result | Force the next spin to return a result with the minimum scatter count |
| Inject re-trigger spin result | Force the next free spin to contain a re-trigger scatter pattern |
| Trigger spin action | Execute a spin through the normal UI code path |
| Wait for game idle | Wait until the game is ready for the next action |
| Query buy bonus controls interactability | Confirm buy bonus is disabled during free spin round |

---

## 4. Known Edge Cases

### Edge Case 1 — Reconnect Mid-Free-Spin Round
If the player disconnects during a free spin round, the server must restore the remaining spin count and accumulated winnings on reconnect. Test that: (a) the counter reflects the server-restored value (not the pre-disconnect value), (b) no extra balance deduction occurs on reconnect, (c) the accumulated win total is preserved.

### Edge Case 2 — Session Resume with Pending Free Spins
If the player closes the browser during a free spin round and reopens the game, the `gameinfo` pending data must restore the round state. Verify the game enters free spin mode (not base game) and the counter reflects remaining spins, not the original total.

### Edge Case 3 — Re-Trigger at Counter = 1
When a re-trigger lands on the last free spin, the counter should increase rather than reaching zero and ending the round. A common defect: the game triggers the round-end flow simultaneously with the re-trigger grant, resulting in a premature round end or a visual glitch where counter briefly shows 0.

### Edge Case 4 — Accumulated Win Display vs Actual Balance
The accumulated win counter displayed during the free spin round is a running visual total. The actual balance credit happens at round end. Test that the displayed total at round end matches the balance increase. Floating-point currency discrepancies between display (formatted string) and actual balance (number) are a common defect.

### Edge Case 5 — Auto-Spin Interaction with Free Spin Trigger
If auto-spin is active and a spin triggers free spins, the auto-spin mode must be suspended for the duration of the free spin round. After the round ends, the game should return to idle rather than automatically continuing the auto-spin sequence. Test that the auto-spin remaining count is zeroed out or suspended, not decremented during free spins.

### Edge Case 6 — Turbo Behavior During Free Spins
Some games disallow turbo mode during promotional or server-granted free spins. Test: (a) whether turbo can be toggled during standard triggered free spins, (b) whether turbo is forced off for promo-granted free spins, and (c) whether turbo state is restored correctly after the round ends.

### Edge Case 7 — Free Spin Payout Resulting in Zero
A free spin that results in a no-win outcome (payout = 0) should still decrement the counter correctly and not trigger any win animation. Test that zero-payout free spins are handled the same as zero-payout base game spins with respect to counter decrement and state transitions.
