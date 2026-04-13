# Reconnect — Shared Mechanism

> Canonical test patterns for network interruption and recovery during various game states.
> Adapted per game by the write-test skill — do not add game-specific method names here.

---

## 1. Rules

### What It Is
Reconnect covers the game's behavior when network connectivity is lost during active play. Because slot games involve real-money transactions, the server is the authoritative source of truth — all state must be restored from the server on reconnect, not reconstructed client-side.

### When Interruption Can Occur
Network loss can occur at any point:
- **Idle state:** No active transaction, lowest risk. Game should simply wait to reconnect and resume.
- **Mid-spin (request sent, result not received):** The server may or may not have processed the bet. Critical state.
- **During result animation:** Bet was processed, result received, animation in progress.
- **During bonus round (Hold & Spin or Free Spin):** Multiple server round-trips may be in progress.
- **During buy bonus flow:** Purchase may or may not have been processed by the server.
- **During promo round:** Similar to free spin, server tracks remaining spins.

### Recovery Mechanism
On reconnect, the game must:
1. Re-establish the network connection.
2. Re-request game info (equivalent of re-sending a `gameinfo` request or equivalent handshake).
3. Server responds with current authoritative state, including any pending/in-progress rounds.
4. Client restores all visible state to match server: balance, bonus state, counters, locked positions.
5. If a round was in-flight (sent but no response received), server provides the result in the pending state.

### Pending State
The server's game info response may include a `pending` object containing:
- Coin positions from an in-progress Hold & Spin bonus.
- Remaining free spin count.
- Accumulated bonus winnings so far.
- Stake amount of the in-flight transaction.

The client must use this pending data to resume (not restart) the round.

### Error Escalation
If reconnection fails after a timeout, the game must transition to a permanent error state that informs the player and provides a way to retry or reload. Silent failures (game appears to work but is not connected) are unacceptable.

### State Machine Transitions
A well-implemented reconnect flow passes through these application states:
`error` → `reconnecting` → `idle` (success)
OR
`error` → `reconnecting` → `error` (failed, show error UI)

---

## 2. Canonical Test Patterns

### Pattern A — Disconnect and Reconnect from Idle
**Scenario:** Network is severed while the game is idle (no spin in progress).

Setup:
- Game loaded and in idle state.
- No spins in progress.

Steps:
1. Go offline (network offline simulation).
2. Wait for the game to detect the disconnection (observe state transition to error/reconnecting).
3. Go online.
4. Wait for the game to complete reconnection.

Assertions:
- Game transitions through error/reconnecting states (not silently stuck in idle).
- After reconnect, game returns to idle state.
- Balance matches the server-authoritative value.
- All interactive controls (spin, bet change, etc.) become available again.

---

### Pattern B — Disconnect During Spin (Request Sent, No Response)
**Scenario:** Network drops after the bet request is sent but before the server response arrives.

Setup:
- Game in idle state.
- Inject network drop at the moment of spin request dispatch (or immediately after).

Steps:
1. Trigger a spin.
2. Immediately go offline (before response arrives).
3. Wait for disconnect detection.
4. Go online.
5. Wait for reconnect to complete.

Assertions:
- Server's response (pending state or completed spin result) is used to restore state.
- Balance reflects the completed spin's payout or the original balance if the server did not process the bet.
- If the server processed the spin and has a pending result, the client displays the result after reconnect.
- No duplicate bet deduction occurs.

---

### Pattern C — Disconnect During Result Animation
**Scenario:** Network drops after the spin result is received but while animations are playing.

Setup:
- Trigger a spin normally.
- Wait for the result to arrive (observe state change to result animation).
- Go offline during animation.

Steps:
1. Go offline after result arrives.
2. Wait for disconnect detection.
3. Go online.

Assertions:
- Game completes or resumes the result animation.
- Balance correctly reflects the already-received payout.
- No duplicate bet or payout.

---

### Pattern D — Disconnect During Hold & Spin Bonus
**Scenario:** Network drops mid-bonus while re-spins are in progress.

Setup:
- Enter Hold & Spin bonus with a known set of locked coins.
- Go offline after one or more re-spins have completed.

Steps:
1. Go offline during a re-spin.
2. Wait for disconnect detection.
3. Go online.
4. Wait for reconnect.

Assertions:
- After reconnect, locked coin positions match the server-authoritative state.
- Re-spin counter matches the server-authoritative remaining count.
- Accumulated payout reflects all completed re-spins (no re-spin results are lost).
- Bonus is resumed, not restarted.
- No duplicate coin positions or payout.

---

### Pattern E — Disconnect During Free Spin Round
**Scenario:** Network drops mid-free-spin round.

Setup:
- Enter free spin mode with a known starting count.
- Complete at least one free spin, go offline before the next.

Steps:
1. Go offline during a free spin.
2. Reconnect.

Assertions:
- Free spin counter reflects server-authoritative remaining count.
- Accumulated winnings from completed free spins are preserved.
- Free spin round resumes (not restarts from the full initial count).
- Balance matches server-authoritative value.

---

### Pattern F — Multiple Rapid Reconnect Attempts
**Scenario:** Network flickers — disconnect and reconnect repeatedly in quick succession.

Setup:
- Idle state.

Steps:
1. Cycle network offline → online multiple times with minimal delay between cycles.

Assertions:
- Game does not enter an inconsistent state after multiple reconnect cycles.
- No duplicate event listeners or message handlers accumulate.
- Game returns to a stable idle state after the network stabilizes.
- Balance is correct and consistent.

---

### Pattern G — Reconnect Timeout (Server Unreachable)
**Scenario:** Network stays down long enough that the reconnect timeout is reached.

Setup:
- Idle or mid-spin state.
- Go offline and keep network offline beyond the reconnect timeout threshold.

Steps:
1. Go offline.
2. Wait for the timeout to elapse.

Assertions:
- Game transitions to a permanent error state (not an infinite reconnecting loop).
- An error message or retry prompt is displayed to the player.
- No background reconnect attempts continue silently after timeout.

---

## 3. Required Bridge Capabilities

| Capability | Description |
|-----------|-------------|
| Query app state | Read the current application state (idle, error, reconnecting, etc.) |
| Query balance | Read current player balance (server-authoritative) |
| Wait for state | Wait until the application reaches a specified state |
| Go offline | Instruct the test framework to sever network connectivity (CDP or equivalent) |
| Go online | Restore network connectivity |
| Query pending state | Read whether the server returned any pending bonus or spin data |
| Query locked coin positions | Read locked coin positions after reconnect (Hold & Spin context) |
| Query re-spin counter | Read remaining re-spins after reconnect |
| Query free spins remaining | Read remaining free spins after reconnect |
| Query accumulated bonus payout | Read accumulated winnings after reconnect |
| Query error state | Determine whether the game is showing an error/disconnect UI |
| Trigger spin action | Execute a spin to confirm normal operation post-reconnect |

---

## 4. Known Edge Cases

### Edge Case 1 — Reconnect During Bonus Transition Animation
The Hold & Spin bonus and free spin rounds have entry/exit animations that play between server round-trips. If the network drops during one of these transition animations, the client may be in a state where it has received partial data. Test that the game can recover from a disconnect during: (a) the entry transition animation, (b) the bonus-result/exit animation. The client should not assume animation completion implies server acknowledgment.

### Edge Case 2 — Stale UI State After Reconnect
After reconnecting, the game must fully refresh all displayed values from the server response. Common defects include: balance label showing the pre-disconnect value, free spin counter showing the original (pre-bonus) value, or locked coin positions not rendering on the grid. Test each displayed value individually.

### Edge Case 3 — Offline During Buy Bonus Confirmation
If the network drops between the player confirming a buy bonus purchase and receiving the server's response, the client must not re-submit the purchase on reconnect. The server's pending state will indicate whether the purchase was processed. Test that no double-deduction occurs and that the bonus state (if purchased) is correctly restored.

### Edge Case 4 — Auto-Spin Active During Disconnect
If auto-spin is running when the network drops, auto-spin mode should be paused or terminated. On reconnect, verify that: (a) auto-spin does not automatically resume, (b) the auto-spin counter is cleared or reflects a safe state, (c) the player must manually re-start auto-spin if desired.

### Edge Case 5 — Session Timeout During Extended Disconnect
Many game servers enforce a session timeout if the connection is inactive for too long (e.g. 30 seconds to several minutes). If the player reconnects after the server-side session has expired, the game must handle the expired session error gracefully — showing a session-expired message rather than entering an undefined state. Test the boundary between successful reconnect (within timeout) and session-expired reconnect (beyond timeout).

### Edge Case 6 — Disconnect Immediately After Balance Update
After a spin completes, the balance is updated on the client. If the network then drops before the next spin, the balance shown should still be the last server-confirmed value. On reconnect, verify the server's balance matches the locally displayed balance (they should be the same if no in-flight requests were lost).

### Edge Case 7 — Reconnect Restores Button Interactability
After a successful reconnect, all button states (spin, bet change, buy bonus, auto-spin) must be restored to match the current game state. A common defect is buttons remaining permanently disabled after reconnect. Test each interactive element's interactability state explicitly after reconnect.
