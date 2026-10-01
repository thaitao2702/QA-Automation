# Grandma Pets — what the suites cover, and what they do not

`test-case-grandma.csv` has **168 rows**. The suites cover **96**, as **101 cases** (a few rows need
more than one play-through, so they carry a letter suffix — TC-103a/b/c, TC-085a/b).

```bash
python tests/chaos/validate_suites.py --game grandma      # OK, and prints the coverage count
```

| Suite | Cases | What |
|---|---|---|
| `gm-core.yml` | 17 | boot, denominations, bet, balance, out of money, history, paytable, tutorial, language |
| `gm-autoplay.yml` | 12 | every way an auto-play run starts and ends |
| `gm-input-sound-reconnect.yml` | 12 | spin gestures, turbo, reconnect, second tab |
| `gm-lastbet.yml` | 5 | close and reopen — restore, a bet or bet-list change before reopening, and resume with spins owed |
| `gm-forced.yml` | 32 | free-spin awards, reel glow, Cat vs Dog drama (BO fixtures) |
| `gm-real-weights.yml` | 22 | Buy Bonus and walking wild (no BO — the game's own weights) |

The other **72 rows are not covered**, and this file is why. Nobody should re-derive it.

---

## 1. No bridge surface — the feature cannot be seen from a test (28 rows)

These are real features with real cases; what is missing is a way to observe them. Each would take
a small bridge addition, and each addition is worth more than the cases it unblocks, because it
turns an AI guess into an exact check.

| Rows | Feature | What the bridge would need |
|---|---|---|
| TC-157 … TC-168 (12) | **Shake / Swirl** — symbols rearrange visually on a shake or swirl gesture | ⚠ Nothing at all exists: no busy label, no state, no event. It is also **gesture-driven** (device shake, a swirl on the canvas), so a test must be able to raise the gesture as well as observe it. TC-164/165/166 are the interesting ones — "it is visual only, changes no symbol count, no RNG, no payout" — and those become exact the moment a `getDisplayedGrid()` comparison is possible before and after |
| TC-017 … TC-020 (4) | **Energy Trail** — tapping a wild runs a trail and opens the paytable popup | No busy label and no state. `persistenceTrails` exists in `busyOps` but belongs to the walking wild, and using it here would be grading the wrong animation. TC-018/019 (several trails at once, no two overlapping) need the trail's own path, which nothing reports |
| TC-059 … TC-071 (13) | **Replay** | The bridge installs in replay mode (`?mode=replay`, §1) and the game auto-starts — but nothing exposes the replay's own controls (Watch Again, Copy Link), its balance sequence, or the "Replay" marker. The balance rows (TC-059 … TC-064) are arithmetic the response could settle if a replay session reported rounds at all |

## 2. Server- or back-office-side, not client (19 rows)

| Rows | Why |
|---|---|
| TC-088 … TC-102 (15) | **Dynamic Game Help** — RTP values, volatility, game limits, version, and the five `*Enabled` switches. Every one is "the client shows what the BO setting says", so a real test writes the setting in BO, reloads, and reads the guide. The reading half is doable today (the guide is a WebView the AI can read); the writing half needs BO endpoints the framework does not have. ⚠ Half a test is worse than none: reading the guide without having set the value grades the guide against itself |
| TC-074 … TC-081 (8, minus overlap) | **Compliance** — auto-play off, turbo off, `minSpinDurationSec`, one-click-per-spin, reality check, max session, bonus-buy off. Same shape: each is a BO/operator switch, and the claim is that the client hides or blocks something. `bonusBuyAllowed` and `Auto Play = false` would be easy once the switch can be written, since the profile already has `HAS_BUY_BONUS` / `HAS_AUTOPLAY` to compare against |
| TC-012 | **History on the back office** — a BO screen, not the game |

## 3. Needs a harness the framework does not have (6 rows)

| Rows | Why |
|---|---|
| TC-001, TC-002 | **Loading at 30%** — the bar must hold at 30% until `gameInfo` arrives, tested by delaying that response past 10 s. Needs request interception (Playwright can do it; no scenario does yet) |
| TC-050 | **Response delay** — the reels must keep spinning until the response lands. Same interception, on the bet response |
| TC-037 | **Hide / switch tab mutes everything** — needs a real visibility change plus an audio-level read; the video has no audio track and no getter reports volume |
| TC-045 | **Tab hidden for 30–45 min, then reopened** — a browser throttles a hidden tab (timers, socket keep-alives), which a network cut does not reproduce, so the 25 s cut used for TC-041 is not evidence for it. Needs a real visibility change held for the full period |
| TC-076 | `minSpinDurationSec` — timing a spin against a configured floor, on top of the BO write above |

## 4. Deliberately not written (19 rows)

| Rows | Why |
|---|---|
| TC-015, TC-016 | Paytable popup **animation loop** and **per-symbol sound**. The animation is a "runs continuously" claim with no defined end state, and the sound has no getter — both are eyes-and-ears checks, and pretending otherwise would produce a green that means nothing |
| TC-038, TC-039 | **BGM off / Music off → volume 0** — removed 2026-10-01 by decision: the claim is about what the player HEARS, and the video has no audio track, so no AI assert can answer it. (`isSoundEnabled()` / `isMusicEnabled()` only report the switch, not the volume) |
| TC-057, TC-058 | **Error codes** — needs the server to return specific error codes on demand |
| TC-072, TC-073 | **Promo** count and countdown — no promo is configured on dev, and `gameMode: PROMO` has never been seen. ⚠ The profile maps promo state 4 but nothing exercises it |
| TC-084 | **Last Bet after Buy Bonus** — the matrix says the client stores the bought round's FAKE screen locally. The purchase carries no grid at all from the server (`bet-response.md` §9), so the claim is entirely about client storage, and `getDisplayedGrid()` after a reopen would be the way in. Worth revisiting — it is the closest of these to being writable |
| TC-087 | **Last Bet during Promo** — blocked by the promo gap above |
| TC-104, TC-114, TC-126, TC-139 | **Removed during review** (2026-09-22/24): TC-104's expected result was a broken spreadsheet cell; TC-114's stated 50% rate contradicts the client's configured 70/70/4; TC-126 assumes a drama needs a real adjacent pair, which the client does not; TC-139's "correct per Design" has no written design rule |

---

## 5. The three bridge additions worth asking for

In the order that buys the most:

1. **`getReelGlowState()`** → `{revealedReels, spinningReel, dimmedCells}` plus a `reelGlow` busy
   label. **Unblocks nothing new but makes 8 existing cases exact** (TC-131 … TC-138), which today
   rest entirely on an AI judging dimming and highlighting on video. The most fragile block in the
   whole suite.
2. **Shake / Swirl state** — a busy label, and the cells it rearranged. Unblocks 12 rows, and
   TC-164/165/166 ("visual only, no symbol lost, no effect on the result") become exact comparisons
   against the server grid rather than opinions.
3. **Energy Trail state** — a busy label and the trail's path. Unblocks 4 rows.

Two smaller ones already filed from live runs: the **`Promo` popup at boot** is in no §7 list
(`bridge-api.md` documents every `visiblePopups` name, and this one is missing), and **§8.3's claim
that the Buy Bonus purchase carries the server's real grid is wrong** — it is five reels of `-1`.
