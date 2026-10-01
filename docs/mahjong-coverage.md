# Mahjong (Mystic) test-case coverage — Mah_001 … Mah_108

Source: `test-case-mahjong.csv`. Maps every case to how it's automated (or why it isn't yet).
The scenario/assert framework is `scripts/scenario_engine.py` + `scripts/run_scenario.py`
(spec: `docs/scenario-assert-authoring.md`). These cases are portable to any cluster-pays
cascade slot that implements `docs/game-bridge-contract.md` — see `scripts/cascade_game.py`.

**Status legend**
- ✅ **Automated** — a case exists in a suite and runs today.
- 🟡 **Feasible** — the bridge already exposes what's needed; only a small new scenario/case is missing.
- 🔧 **Needs bridge/infra** — requires a new bridge affordance, forced outcomes (BO paytable), network
  control, locale/BO provisioning, or special input the current API doesn't expose.
- 👤 **Manual / external** — not a client-driveable check (BO system, real load pipeline, etc.).

**Suites**: `mah-001-020.yml` · `mah-basegame-freespin.yml` · `mah-autoplay.yml` · `mah-tutorial.yml` ·
`mah-spin-turbo.yml` · `mah-reconnect.yml` · `mah-freespin-balance.yml` · `mah-reels-glow.yml` · `mah-popups.yml` ·
`mah-language.yml` · `mah-history-lastbet.yml`.

---

## Automated today (✅)

| Case | Scenario | Suite |
|---|---|---|
| Mah_001 UI at load | boot-base | mah-001-020 |
| Mah_004 denominations = config | browse-denominations | mah-001-020 |
| Mah_005 bet +/- | change-bet | mah-001-020 |
| Mah_006 default bet | boot-base | mah-001-020 |
| Mah_007 balance debit | base-spin | mah-001-020 |
| Mah_008 balance credit | base-cluster-win | mah-001-020 |
| Mah_009 insufficient balance | insufficient-balance | mah-001-020 |
| Mah_010 + Mah_016 auto stops on empty | auto-until-empty | mah-autoplay |
| Mah_012 in-game History | spin-then-history (**exact**: row matched by round id) | mah-history-lastbet |
| Mah_014 + Mah_019 auto stops after N / button states | auto-play | mah-001-020 |
| Mah_015 stop at any win | auto-stop-at-win | mah-autoplay |
| Mah_017 user stop | auto-user-stop | mah-autoplay |
| Mah_018 reload cancels auto | auto-reload-cancels | mah-autoplay |
| Mah_020 change bet before auto | auto-change-bet-first | mah-autoplay |
| Mah_043 base multiplier init x1 | base-spin (video) | mah-basegame-freespin |
| Mah_044 base multiplier +1/cascade | base-cluster-win (video) | mah-basegame-freespin |
| Mah_045 base multiplier reset | base-cluster-win (video) | mah-basegame-freespin |
| Mah_046 buttons disabled during spin | base-spin (video) | mah-basegame-freespin |
| Mah_047 free-spin trigger | fs-complete (stream+video) | mah-basegame-freespin |
| Mah_048 FS multiplier init x2 | fs-complete (video) | mah-basegame-freespin |
| Mah_049 FS multiplier +1/cascade | fs-complete (video) | mah-basegame-freespin |
| Mah_050 FS multiplier reset | fs-complete (video) | mah-basegame-freespin |
| Mah_051 buttons in free spins | fs-complete (video) | mah-basegame-freespin |
| Mah_057 open Tutorial | open-tutorial | mah-tutorial |
| Mah_059 Tutorial content | open-tutorial | mah-tutorial |
| Mah_060 Tutorial images | open-tutorial | mah-tutorial |
| Mah_052 language follows the player | language-tour + image per screen | mah-language |
| Mah_053 switch language, nothing stale | language-tour (mid-session switches, `switched from` label) | mah-language |
| Mah_054 auto text scaling | language-tour (stress language required) | mah-language |
| Mah_055 no clipped text / missing glyphs | language-tour (stress language required) | mah-language |
| Mah_056 every language translates | language-tour (any scope: `all` / pinned set / `random:N`) | mah-language |
| Mah_085 Last Bet in Free Spin (in-bonus half) | last-bet-before-rounds (stream: FS stake == 0) | mah-history-lastbet |
| Mah_087 Last Bet before each bonus round | last-bet-before-rounds (video, high_fps) | mah-history-lastbet |
| Mah_105 Turbo Enabled/Disabled popup | toggle-turbo (stream+video) | mah-popups |
| Mah_107 Auto Play Enabled/Disabled popup | toggle-auto (stream+video) | mah-popups |
| Mah_108 stacked popups: no overlap, order, auto-close | stacked-popups (stream+video) | mah-popups |

> Mah_011 (balance in Free Spin) already has a scenario (`fs-complete`, `stream: stake_zero_in_freespins`);
> its case is currently commented out in `mah-001-020.yml` — re-enable to move it to ✅.

Several multiplier reads (043/044/045/048/049/050) are AI (video) checks of a **transient on-screen
value**, graded probabilistically (orange on a miss), `confidence: medium/low`.

> ⚠ **They can now be promoted to exact `stream:` checks.** Captured payloads
> (`games/mystic/bet-response.md`) show every cascade step carries `multiplierLevel`, so the ladder is
> in the response after all. The same samples show it is a **ladder, not `+1` per cascade** — base game
> runs `1 → 2 → 3 → 5 → 8 → 10`. Mah_044 / Mah_049 ("multiplier +1 per cascade") therefore state an
> expected result the server does not produce; confirm the intended rule against the bridge's
> `getMultiplierLevels()` before trusting those two cases.

---

## Feasible now — small new scenario needed (🟡)

These only need a new closed-box scenario; the bridge already has the affordance.

| Case | What to build | Bridge affordance |
|---|---|---|
| Mah_021 Turbo on | `spin-turbo`: click `turbo`, spin, video "reels resolve faster / Stop Now" | `turbo` button ✅ |
| Mah_022 hold-mouse turbo | `hold-spin-turbo`: `holdSpinStart()` → `holdSpinEnd()`, video turbo only while held | `holdSpinStart/End()` ✅ |
| Mah_023 hold-Space turbo | same via keyboard `page.keyboard.down/up(' ')` | Playwright key input |
| Mah_035 spin via Spin button | `base-spin` already does this — add a case (video: reels spin) | `spin` ✅ |
| Mah_036 spin via Space | `spin-space`: `page.keyboard.press(' ')` then `awaitBetResponse` | Playwright key input |
| Mah_037 hold → auto spin | `hold-spin-auto`: `holdSpinStart()`, observe repeated spins | `holdSpinStart/End()` ✅ |
| Mah_038 hold sudden-stop | hold during a spinning reel; video reels stop immediately | `holdSpinStart()` ✅ |
| Mah_039 release hold stops | `holdSpinEnd()`; video auto-spin stops | `holdSpinEnd()` ✅ |
| Mah_040 double-click sudden-stop | `page.mouse.dblclick` on the spin rect while spinning | coordinate click ✅ |

---

## Needs a bridge affordance or forced outcome (🔧)

| Case(s) | Blocker |
|---|---|
| Mah_024–026 Sound (mute BGM/Music, tab-switch mute) | need a **volume/mute getter** (assert volume==0); `setting:music`/`setting:sound` toggle exists but no read-back. |
| Mah_041 / Mah_042 Reels Glow | need a **forced bonus layout** (which reels show Bonus). 7×7 has no forced-outcome paytable → can't script the layout; would need a BO fixture. |
| ~~Mah_052–056 Multi-language~~ | **Done** — `mah-language.yml`. The language lives in the Playsite launch token, so a run declares `language: <code>` and the runner mints a fresh URL via `url_script` (`PLAYSITE_LANGUAGE`) and re-enters; `ctx.set_language()` does the same mid-scenario. Needs `url_script` + `PLAYSITE_*` creds; the static `url:` is ignored for these runs (its token carries `"language":"en"`). |
| Mah_058 Tutorial navigation | Tutorial **next/prev has no bridge button** (only `auto:next/back` exist, which are the autoplay popup). |
| Mah_061 / Mah_062 Tutorial symbol payout | no button to **select a Tutorial symbol**, and no payout read-back. |
| Mah_063 Tutorial after language change | now unblocked by the Mah_052 language plumbing: a `language-tour` run with `language: <code>` already opens the Tutorial as one of its surfaces. Add a dedicated case if QA wants it graded separately. |
| Mah_064–073 Replay | need to **open Replay from History** and drive replay controls. `historyDetail` popup exists, but Replay open/Watch-Again/Copy-Link buttons aren't in the bridge button list. |
| Mah_074–080 Promo | need a player **provisioned with a Promo** (BO) and the Promo UI (`gameState==4`, `promo` popup label). No promo-account setup in the runner. |
| **Mah_081 / 082 / 084 / 086** + the post-bonus half of **Mah_085** | **BLOCKED — the bridge exposes no `lastBet` button.** Every one of these begins "open Last Bet", and the panel cannot be opened from the test API. Add a `lastBet` button (+ a `lastBet` popup label) and each becomes a small scenario shaped like `spin-then-history`. Mah_086 additionally needs a promo-provisioned player (as Mah_074–080). **Mah_087 and the in-bonus half of Mah_085 are automated** — Last Bet auto-displays for ~0.5s before each free-spin round, so they need no button. |
| Mah_083 Last Bet for Buy Bonus | **`buyBonus` is not supported by this game** (bridge-qa-api.md Gotchas: the button exists but does nothing). Not a defect, not automatable here. |
| Mah_088–096 Game Help | need a **Game Help open** affordance and read-back of Max Win / Game Limits / Volatility / RTP / Interruption / Auto-Finalize (BO-driven). No dedicated help button/getters in the bridge. |
| Mah_097–104 Compliance | each needs a **BO config toggle** (autoPlay/turboMode/minSpinDuration/oneClickPerSpin/realityCheck/maxSessionTime/bonusBuyAllowed) then observe. Needs BO-provisioned variants + some timers. |
| Mah_027–034 Auto Reconnect | need **network control** (offline/slow, Wi-Fi switch, multi-tab session). Playwright/CDP can drop the network (`context.set_offline`), so Mah_027/028/031/034 are partially feasible with a new `reconnect-*` scenario; multi-tab (030) and long-idle (032) are heavier. |

---

## Manual / external (👤)

| Case(s) | Why |
|---|---|
| Mah_106 Ante Bet popup | **Mystic has no Ante Bet** — no `anteBet` button in the build, so the case's precondition ("the game supports Ante") cannot be met. Not a defect and not automatable here. If a later build gains the feature, the scenario is a near-copy of `toggle-turbo` minus the speed-mode facts (the bridge exposes no ante getter). |
| Mah_002 / Mah_003 Loading-bar timing | `startGame()` skips the real load pipeline (goes straight to a clean base). Needs a navigate-and-record-boot harness that measures the actual loading bar; treat as manual for now. |
| Mah_013 History on BO | The BO API client can post to an endpoint, but this repo does not know the bet-history endpoint, and dev BO auth is currently rejected. The Mah_012 run exposes `facts.round_id / stake / payout / balance`, so the manual comparison is a copy-paste. To automate: add the endpoint to `framework/bo/bo_api_client.py` + working BO creds. |

---

## Suggested next batches (by ROI)

1. ~~**Popups Mah_105–108 + Turbo Mah_021–023**~~ — done: `mah-popups.yml` (`toggle-turbo` /
   `toggle-auto` / `stacked-popups`) and `mah-spin-turbo.yml` (`turbo-modes`).
2. ~~**Spin input Mah_035–040**~~ — done: `spin-inputs` in `mah-spin-turbo.yml`.
3. **Bridge asks** (unblock whole modules): add `lastBet` button (Mah_081–087), a volume getter (Mah_024–026), Replay-open buttons (Mah_064–073), and a Tutorial-nav/symbol button (Mah_058/061/062).
4. **BO/infra** — locale switch (i18n), promo-provisioned account (Promo), compliance config variants, network control (Reconnect).

### Last Bet — reopening the game (Mah_081, 082, 084, 085)

`mah-lastbet.yml`. These were parked in `mah-history-lastbet.yml` as "not automatable" because each
begins "Mở Last Bet", read as *open the Last Bet panel* — and this bridge has no `lastBet` button.
**That reading was wrong: Last Bet is a RESUME behaviour.** "Mở Last Bet" is *reopen the game*, so no
button is needed:

| Case | Scenario |
|---|---|
| Mah_081 reopening after a LOSING round: its stake, no win | last-bet-restore (`outcome: lose`) |
| Mah_082 reopening after a WINNING round: stake, win, winning cluster | last-bet-restore (`outcome: win`) |
| Mah_084 change the denomination after a round, reopen → the round's stake | last-bet-restore (`change_bet_after`) |
| Mah_085a pending free spins resume at their own stake · Mah_085b the default applies once they end | bonus-resume-denomination-swap (one run; teardown `reset-denomination`) |

**Mah_087 stays in `mah-history-lastbet.yml`** — it is the one case about the panel DRAWING itself
(it flashes ~0.5s before each bonus round) and needs the high-fps clip.
Mah_083 is not applicable (no Buy Bonus on this game); Mah_086 still needs a promo-provisioned player.

⚠ Mystic's bridge doc does not document `isGameInfoCaptured()`, so the capture gate is
`info_captured != False` — it fails when the bridge SAYS the capture is missing, and passes when it
merely cannot answer (as measured on Chaos).
