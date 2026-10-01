# Treasures of the Dragon VIP — test-case coverage (DV-01 … DV-73)

Dragon VIP is **Dragon plus two paid spin features**, and that decided how it was onboarded:

| | |
|---|---|
| Base game, Hold & Spin, BET response | Dragon's (bridge doc §9 terminal rule, §11 non-goals). So **DV-01 … DV-52 are DR-NN** on the same scenarios and invariants |
| Server paytable | Dragon's **plus** `AnteBet*` and `PremiumReel_*` sections, and **different `BaseGame` weights**. Fixtures are re-derived, not copied |
| New features | Ante Bet (x1.5) and Premium Reel (reelId 2 x1.5, 3 x1.5, 23 x2.2), both behind the Buy Bonus panel |
| Bridge | `games/dragon-vip/game-bridge-api.md` (`dragon-vip-bridge-v3/2026-09-14`) |
| Premium Reel contract | `games/dragon-vip/docs/raw/client-integration.vi.md` (GDP-2770) |

- Profile: `scripts/game_profile.py` → `DragonVip(Dragon)`
- Suites: `games/dragon-vip/test-suites/dv-{core,autoplay,holdspin,input-sound-reconnect,ante,premium-reel,lastbet}.yml`
- Fixtures: `games/dragon-vip/paytables/` (derived; its README has the commands)
- Config: `games/dragon-vip/env.config.dev.yml` (**incomplete on purpose**, see below)

```
python tests/chaos/validate_suites.py --game dragon-vip      # no browser needed
python scripts/probe_bridge.py --game dragon-vip --env dev   # once the env config is filled in
python scripts/run_scenario.py --game dragon-vip --env dev --suite games/dragon-vip/test-suites/dv-ante.yml
```

---

## ⚠ Before the first live run

Nothing here has run against a VIP build yet. Three things stand in the way:

1. **Fill in `games/dragon-vip/env.config.dev.yml`.** The build path, Playsite game code and BO game
   id are unknown to this repo, so they are left empty. With Dragon's values the runner would
   launch plain Dragon. The ante and premium cases would go unreached, and the mirrored cases would
   grade the wrong game. The forced runs would also push VIP fixtures onto Dragon's BO record.
2. **Probe the build.** Check these resolve on screen: `stakeIncrease`, `auto` / `stopAuto`,
   `buyBonus`, and, with the panel open, `buyBonus:anteBet`, `buyBonus:premiumReels:2`, `buyBonus:package:0`.
   Check that `getPremiumReelOptions()` returns the three packages.
3. **Capture BET responses** into `games/dragon-vip/fixtures/bet-responses/`: one plain round, one
   ante round, one premium round, and one respin of a premium bonus. Then write
   `games/dragon-vip/bet-response.md`. The profile reads `anteMultiplier`, `premiumReelId`,
   `premiumReelMultiplier`, `baseStake` and `winlose` on the strength of the docs alone.

---

## What the VIP build changes, and where it reached the framework

| Difference | Change |
|---|---|
| No one-click ante. Ante and each Premium Reel: `buyBonus` → ACTIVATE row → `buyBonus:confirm`; off: one click on `anteBet:off` / `premiumReels:off` | new engine helper `switch_feature(ctx, "ante" \| "premium:<id>", on)`. `ante-toggle` uses it, so Chaos's one-click toggle and VIP's panel share one scenario |
| Ante / Premium Reel exist on a Hold & Spin game | `base_stake` / `ante_multiplier` / `is_ante_round` moved onto `SlotGame`; new `premium_reel_id` / `premium_reel_multiplier` |
| Premium Reel is a new feature | new scenario `premium-reel-spin`; new invariants `premium_reel_stake`, `premium_reel_frozen_in_freespins` |
| A Hold & Spin can be bet with a feature | `hold-spin-complete` gained `feature:`. It samples the off control through the whole drain and restores the feature afterwards |
| A Buy Bonus package row raises the confirm sheet itself (no `buyBonus:buy`) | `buy-bonus` treats `buy_purchase` as optional and scrolls the row into view first |
| Stake controls are canonically `stakeIncrease` / `stakeDecrease`; no `autoOrFreeSpin` | profile dialect only |
| `anteNotify` toast | `POPUPS["ante_notify"]` |
| The feature panel carries VIP-only text | added to `UI_SURFACES`, so `language-tour` shoots it |
| Fixtures | `scripts/derive_paytable.py` now takes list-valued and comma-separated sections, plus `--coin-weight` |

The Chaos behaviour of `ante-toggle` and `buy-bonus` is unchanged: the toggle branch of
`switch_feature` clicks `ante` exactly as before, and Chaos has no Buy Bonus. Dragon and Zeus keep
`buyBonus:buy`.

---

## Automated — 69 of 73

Seven of those 69 (DV-59 … DV-65, Premium Reel) are written but cannot run yet: this session does not
offer the feature. See the findings above.

DV-67 … DV-71 and DV-73 are the **Last Bet** cases. Last Bet is what the game shows when the player
reopens it, not a panel. DV-67 … DV-70 play a round, reopen, and check the session comes back on that
round (`last-bet-restore`). DV-71/DV-73 reopen while a bonus is still OWED, after the denomination was
changed on BO, so the reopen RESUMES the bonus (`bonus-resume-denomination-swap`). ⚠ Every one of
them gates on `info_captured` — `applyLastBet()` nulls `gameInfo.lastBet` at Start, so a live read
reports nothing on a session that restored perfectly, and an ungated case would green on a broken
build.

| Case | Scenario | Suite |
|---|---|---|
| DV-01 … DV-52, less DV-10 / DV-23 | as DR-NN (`docs/dragon-coverage.md`) | dv-core, dv-autoplay, dv-holdspin, dv-input-sound-reconnect |
| DV-53 Ante on from the panel charges base x multiplier · DV-54 one tap off, next spin plain · DV-55 while Ante is on the feature panel cannot open | ante-toggle (`spin_after_off`, one run) | dv-ante |
| DV-57 Hold & Spin on an ante spin plays normally · DV-58 Ante locked during it | hold-spin-complete (`feature: ante`, `ante-trigger3-countdown`) | dv-ante |
| DV-59 every package switches on · DV-60 charged base x multiplier, base unchanged, echoed · DV-61 one tap off · DV-62 Premium Reel excludes Ante / Buy Bonus · DV-63 package changes between spins | premium-reel-spin (one run) | dv-premium-reel |
| DV-64 package locked for every respin · DV-65 package cannot be switched off mid-bonus | hold-spin-complete (`feature: "premium:2"`, `premium-trigger3-countdown`) | dv-premium-reel |
| DV-66 a bought bonus never carries a package | buy-bonus | dv-holdspin |
| DV-67 reopening after a LOSING round restores its stake and no win | last-bet-restore (`outcome: lose`) | dv-lastbet |
| DV-68 reopening after a WINNING round restores its stake, win and winning line | last-bet-restore (`outcome: win`) | dv-lastbet |
| DV-69 reopening after a bonus TRIGGERED from the base game restores that round, not a plain spin | last-bet-restore (`outcome: freespin_trigger`, `fs-trigger3-countdown`) | dv-lastbet |
| DV-70 play at bet X, change the bet to Y, reopen → the game shows X | last-bet-restore (`change_bet_after: true`) | dv-lastbet |
| DV-71 denomination changed on BO while a bonus is owed → the resumed bonus plays at the old bet · DV-73 after that bonus, the bet is the new denomination's default | bonus-resume-denomination-swap (one run, `fs-trigger3-countdown`; teardown `reset-denomination`) | dv-lastbet |

Every case has a `stream:` half and an AI half (Rule 10), except DV-04, which is arithmetic over the
launch payload, as DR-04.

The Last Bet cases grade their AI half with `video: at: [played, restored]` (DV-71/73: `[trigger,
restored]`) — one clip running from the last round's response, through the reload, to the restored
game sitting ready. A still
cannot hold a win presentation (the counter runs up, the payline is drawn and fades), and the
question is not *what* the screen reads — the bridge owns that exactly — but whether it MATCHES what
the player left. Each prompt gates on `reload_visible`: no reload in the clip means no two phases to
compare, and every other answer is meaningless.

## Not automated

| Case / rule | Why |
|---|---|
| DV-10 History on BO | manual, as DR-10 |
| DV-23 tab-switch mutes sound | not written yet, as DR-23 |
| DV-56 switching Ante Bet on raises its notification | the build raises no toast (measured 2026-09-16), while the bridge doc lists `anteNotify`. Dropped from dv-ante.yml until the game team says which is right; `{facts.ante_toast_on_enable}` still records it |
| DV-59 … DV-65 Premium Reel | written, but **unreached** on this session: `settings.premiumReel` is null, so the feature is not offered. They run as soon as BO enables it |
| Server rejections -111 … -116 (client doc §5) | need a hand-built bet request; the client never sends one. An API-level test, not a UI one |
| Reload during a Premium Reel bonus resumes with `pending.premiumReel` (§2.2, §6 rule 6) | no scenario reloads mid-Hold & Spin yet |
| `lastBet.premiumReelId` replays the last spin's look on rejoin (§2.3) | needs a reload after a premium spin plus a visual read; small, not written |
| DV-72 reopening after a PROMO round | needs a **seeded promo account** — promo exists only when the server sends `gameInfo.promo`. Chaos parked CR_081-083 and CR_097 on the same thing. When written: `processEndPromo()` puts the bet back to `defaultBetLevel - 1` when a promo ends, which is the half to assert; ⚠ VIP promo popups are Promise-driven — click through, never dismiss |
| CR_094 proper — reopening after a **bought** bonus restores at the PURCHASE PRICE, not the base stake | DV-69 covers the bonus *triggered* from the base game. The two routes into the same feature charge different amounts (a purchase is `price_ratio > 1` of the base stake), so they are separate cases and neither is called merely "bonus". The bought variant needs `buy-bonus`'s panel flow then a reload; `last-bet-restore` deliberately does not reimplement that panel. The shape is a third outcome, `buy_bonus`, delegating to the helper `buy-bonus` already uses — small, worth doing once DV-69 runs green |

---

## What the first live runs found (2026-09-16)

Measured on the dev build (`dragon-vip-bridge-v3/2026-09-14`, currency TKN, 9 denominations 1.99 … 35,
bet 7, `defaultBetLevel: 8`):

| Finding | Evidence | Effect |
|---|---|---|
| **Premium Reel is not offered on this session**, though the paytable file enables it | `getGameInfo().settings.premiumReel` is `null` while `anteMultiplier` is `1.5`; `premiumReelAvailable` false; `getPremiumReelOptions()` `[]`; `canDo('activatePremiumReels:2')` → `feature_unavailable:premiumReels` | dv-premium-reel.yml (DV-59 … DV-65) reports **unreached** until BO enables the feature for the test player / currency. DV-55 dropped its premium half (see below). **Ask BO/product why**: the paytable ships `PremiumReel_Setting.Enable: true`, so the agent or currency record disagrees with it |
| **Switching Ante Bet on raises no notification** | `visibleNotifications` stayed empty across the switch; `{facts.ante_toast_on_enable}` False | DV-56 is not automated. The bridge doc lists `anteNotify` as a toast (§5, §6.8), so either the doc is ahead of the build or the toast was dropped — **a question for the game team**, not a suite fix |
| **Hold & Spin respins report `balance: -1`**, not the running balance | trigger 14283.04 → respin −1 → respin −1 → terminal 14339.04, with 14283.04 + 56.00 = 14339.04. No doc mentions it (Zeus documents a `-1` sentinel only for the autoplay counter) | `conservation` now treats a non-positive balance as "not reported" and makes the terminal round account for every carried stake and payout. Before that it reported two violations on a feature whose money was exactly right. **Worth confirming with the server team** that −1 means "unchanged/not sent", and capturing it in `games/dragon-vip/bet-response.md` |
| Ante Bet itself works through the panel | `canDo('activateAnteBet')` ok; `anteMultiplier: 1.5`; `anteBet:off` resolves only while it is on | DV-53 / DV-54 / DV-55 / DV-57 / DV-58 stand as written |
| The bridge is missing from the dev-play build | none of the 18 loaded scripts contains `__gameTestBridge` | runs need a build with the bridge (`base_url` pointing at one), not the plain dev-play URL |

## Open items

1. **Field names are documented, not observed.** See "Before the first live run" §3. A red on
   `ante_stake_multiplier`, `premium_reel_stake` or `{facts.ante_engaged}` before a payload is
   captured may be a naming gap, not a defect. The `pending` block spells ante `anteMulti`, and the
   bet response may do the same.
2. **What does a Premium Reel respin echo as `stake`?** Client doc §4's phase table says
   `baseStake x multiplier`, while §3.3 says the client sends 0. DV-64 asserts `conservation`, so a
   non-zero stake that is not debited goes red there. That is a question for the server team, not a
   reason to loosen the assert. `{facts.respin_stakes}` shows the echo. Ante respins have the same
   question and are not asserted `stake_zero_in_freespins` either.
3. **Does a feature stay on after its bonus?** Nothing documents it.
   `{facts.feature_on_after_bonus}` is exposed on DV-57/64 and not asserted.
4. **Does switching Ante off raise a toast?** `{facts.ante_toast_on_disable}` is exposed, not asserted.
5. **Stake rounding.** `ante_stake_multiplier` compares to 1e-6 (Chaos's tolerance), while
   `premium_reel_stake` compares to the cent. If a denomination x 1.5 lands on a half-cent and the
   server rounds, DV-53 goes red on arithmetic, not behaviour. Check the denominations once the
   build is probed.
6. **DV-52 expects the help to explain Ante Bet and Premium Reel** (`paid_features_present`). That is
   an assumption about the help content; drop the field if the VIP help does not document them.
7. **`defaultBetLevel` is 1-based in this client too** (bridge §3). DV-04 keeps the contract's reading,
   as DR-04 does.
8. **There is no Dragon VIP test matrix**, so the validator reports coverage as unknown, and the
   numbers DV-53 … DV-66 were assigned here. Re-map them if QA issues one.
