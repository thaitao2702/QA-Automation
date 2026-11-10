# Grandma Pets — BET response reference

The server's BET payload: the contract the whole framework drives on. Scenarios read game LOGIC from
here and nowhere else (`docs/session-summary-scenario-qa.md`), so this is the file to check before
writing an assert, an invariant, or anything that counts ways.

Grandma Pets is a **1024-ways** game (4 rows × 5 reels). It is the first ways game in this repo —
neither `CascadeGame` nor `LineGame` nor `HoldSpinGame` describes it, and several `SlotGame` field
names do not exist in this payload. See [§12](#12-writing-the-profile).

## Captured samples

Real captured payloads live in `fixtures/bet-responses/`. All five were captured at a bet of
**0.10**.

| File | What it is |
|---|---|
| `base-win.json` | base spin, three `wayDetails`, wild multiplier **1x**, payout 0.23 |
| `base-win-wild-x8.json` | base spin, one 4-of-a-kind way, wild multiplier **8x**, payout 1.60 |
| `buybonus-purchase.json` | the **Buy Bonus purchase round** — 75× bet, awards 15 free spins |
| `freespin-win.json` | free spin #2 of that bought bonus — the **same symbol split into a wild way and a no-wild way** |
| `freespin-no-win-wilds.json` | free spin #7 of the same bonus — three wilds on the grid, **zero ways**, `wildMultipliers: null` |

⚠ `setForcedResult` / `setDevModeResult` are **not implemented** in this build (`bridge-api.md` §3),
so unlike Mystic these are not payloads you can feed back in. They are reference only; every forced
outcome has to come from a BO paytable fixture.

The two free spins are **not consecutive** (`freeSpins` 13 then 8) and belong to the same bonus as
`buybonus-purchase.json` (`roundId` `943027205863112705`).

---

## 1. Envelope

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1790046403263,
  "result": { /* everything below */ } }
```

`status: 1` is success. `bridge-api.md` §4 says responses with `status <= 0` are **not rounds** —
they never reach `awaitBetResponse()` and appear in the event log as `response_error`.

`scenario_engine.Bet.parse()` receives the **inner** object, so `Bet.raw` is the result body.

Buy Bonus purchases (socket event `buyfreespin`) and free spins (`bet`) use this same envelope and
the same bridge cursor. The **Free Spin Extension** reply is a different stream —
`awaitExtensionResponse()`, not documented here.

## 2. The grid — `view` and `rawView`

```jsonc
"view": [ {"index":0,"value":[18,3,4,1]}, {"index":1,"value":[18,3,5,4]},
          {"index":2,"value":[2,18,1,18]}, {"index":3,"value":[18,4,4,3]},
          {"index":4,"value":[2,18,19,6]} ]
```

- **Singular `view` / `rawView`** — not Mystic's and Joker's plural `views`. A profile that copies
  `F_STEP_GRID = "views"` silently reads nothing.
- Five entries, one per reel, **column-major**: `index` 0 = reel 1, `value[0]` = top row.
- Always **4 rows** per reel, in all five captures. 4⁵ = the 1024 ways the rules page states.
- **`view == rawView` in every capture**, byte for byte, including the purchase round. No capture
  yet shows them diverging, so nothing here establishes what `rawView` is for.
- Symbols are **integer ids**, not the string names the paytable uses. See §3.

## 3. Symbol ids

Confirmed by the game team (2026-09-22), and matching `bridge-api.md` §8.5 (`catSymbolIds` 5, 7, 8 ·
`dogSymbolIds` 6, 18). The art and the descriptions the AI reads grids with are in
`docs/assets/assets.yml`.

| id | name | picture | family |
|---|---|---|---|
| 1 | `A` | playing-card A | — |
| 2 | `J` | playing-card J | — |
| 3 | `Q` | playing-card Q | — ⚠ **no art yet** (`symbols/Q.png` missing) |
| 4 | `K` | playing-card K | — |
| **5** | `P1` | fluffy **white** cat, pink headphones | CAT |
| **6** | `P2` | fawn **pug**, red bow tie | DOG |
| **7** | `P3` | **orange** ginger tabby, raised paw | CAT |
| **8** | `P4` | long-haired **grey** cat | CAT |
| **11** | `Scatter` | golden gem paw — the BONUS | — |
| **18** | `P5` | black-and-tan **dachshund** in a bonnet | DOG |
| **19** | `WILD_D` | the grandma — `wildMultipliers[].symbolId`, every cell in `wildPositions` | — |

Ids are sparse and are not paytable order: a client atlas index. `Ten` and `Nine` exist in the
paytable but pay 0 **and** carry reel weight 0 everywhere, so they never land — nine paying symbols,
not eleven.

### ⚠ The live pays disagree with `docs/raw/paytable.json`

Given the map above, two captured wins do not match the paytable in this repo:

| id | name | live pays, from the captures | `docs/raw/paytable.json` says |
|---|---|---|---|
| 18 | `P5` | 4 reels **0.5**, 5 reels **1** | P5 is `[…, 0.2, 0.35, 0.75]` — this is **P4's** row |
| 4 | `K` | 5 reels **0.5** | K is `[…, 0.1, 0.15, 0.4]` — this is **A's** row |

(Id 3 = `Q` does match: 5 reels pays 0.3.) It is not a uniform shift, so the likeliest reading is
simply that **`docs/raw/paytable.json` is not the paytable the dev server is running**.

That matters beyond the pay values, because **every forced fixture is derived from that file** and
pushed to BO as the whole paytable content. Deriving from a stale copy means the test runs against
pays — and possibly reel weights — that differ from production, and reel weights are what the
computed trigger rates and fixture densities rest on.

**Export the live paytable from BO and compare before deriving any fixture** (summary 11 §7 has the
`cmp` / re-derive recipe).

## 4. `wayDetails[]` — the ways formula, verified

```jsonc
{ "winType": "Ways", "symbolId": 18, "wayCount": 4,
  "winPositions":  [ {"index":0,"value":[0,3]}, {"index":1,"value":[2]},
                     {"index":2,"value":[1]},   {"index":3,"value":[1,3]} ],
  "wildPositions": [ {"symbolId":19,
                      "positions":[{"index":1,"value":[2]},{"index":2,"value":[1]}],
                      "multiplierValue":8} ],
  "totalWildMultiplier": 8, "symbolMultiplier": 0.5, "totalMultiplier": 4.0, "payout": 1.60 }
```

**The formula, checked against all 8 `wayDetails` in the captures — exact, no mismatches:**

```
totalMultiplier = symbolMultiplier × totalWildMultiplier
payout          = totalMultiplier × wayCount × baseStake
```

⚠ **`baseStake`, not `stake`.** A free spin costs `stake: 0` while `baseStake` keeps the real bet, and
its payouts are still computed off `baseStake`. `freespin-win.json` proves it: `stake: 0`,
`baseStake: 0.1`, payout 0.8. Dividing by `stake` divides by zero on every free spin.

Also verified on every capture:

- `wayCount` = the product of `len(value)` over `winPositions` — the rules page's `1 × 3 × 2 = 6`.
- `totalWays` = **Σ `wayCount`** across all `wayDetails` (5 = 1+2+2; 4 = 2+2). It is not a count of
  entries and not a distinct-symbol count.
- `len(winPositions)` = how many reels the run spans, always starting at reel 0 and contiguous.
  3, 4 or 5 — matching the paytable's `Pays[n-1]`.
- Every cell named in `winPositions` holds either the symbol itself or a 19.

### ⚠ One symbol can produce several `wayDetails`

This is the structural trap of the payload. The engine enumerates per **(symbol, set of
participating wilds)**, so a reel holding *both* the symbol and a wild splits the symbol's wins into
separate entries:

| capture | entries | why |
|---|---|---|
| `freespin-win.json` | symbol 18 twice: `wildPositions: null` → ×1, payout 0.2 · and one wild → ×3, payout 0.6 | reel 5 has a natural 18 at row 1 **and** a wild at row 2 |
| `base-win.json` | symbol 4 twice, both ×1 | same shape on reel 5; here the multiplier is 1 so the split costs nothing |

Consequences for asserts:

1. **Never assume one `wayDetails` entry per symbol.** A symbol's true ways count is the sum across
   its entries (symbol 4 in `base-win.json` wins 4 ways, reported as 2 + 2).
2. `freespin-win.json` is the direct proof of the rules page's wild rule (1.jpg / 3.jpg): the
   no-wild half of the *same* symbol's win is **not** multiplied. The assert is per entry —
   `wildPositions == null` ⟹ `totalWildMultiplier == 1`.

### `wildMultipliers` and `wildType`

| field | shape |
|---|---|
| `wildType` | `{"type": "NonStacking", "calculationType": null}` — matches the paytable's `NonStackingRandomWildMultiplier` |
| `wildMultipliers` | a **list keyed by symbolId**, one entry here: `[{"symbolId": 19, "multiplierValue": 8}]` |

The round-level `multiplierValue` equals the `multiplierValue` repeated inside every
`wildPositions` entry and equals `totalWildMultiplier` wherever a wild participates — verified on
all three winning captures. One multiplier per round, as the rules page says.

Observed values sit in the configured ladders (`bridge-api.md` §8.1, `getMultiplierLevels()`):
base `1x` and `8x` from `[1,2,3,5,8,10,15,30,40]`; the free spin `3x` from `[2,3,5,8,10,15,30,40]`,
which has **no 1x**.

⚠ **Both fields are `null` on a round that wins nothing** — `freespin-no-win-wilds.json` has three
wilds sitting on the grid and still reports `wildType: null`, `wildMultipliers: null`. The rules page
says a multiplier is drawn *every* spin, so it is drawn and simply not reported. **You cannot assert
"every round carries a multiplier in the ladder"** — only rounds with `wayDetails`. Gate the assert
on `payout > 0`.

> Not yet settled: whether a round that wins but whose wins contain **no** wild at all still reports
> `wildMultipliers`. All three winning captures include at least one wild-bearing way. The gate could
> be "any win" or "any win with a wild"; nothing here separates them.

## 5. The money

| Field | Verified on all five captures |
|---|---|
| `payout` | = Σ `wayDetails[].payout`. **Equals `rawPayout` everywhere** — no capture shows them diverging |
| `winlose` | = `payout − stake`, including the purchase (`0 − 7.5 = −7.5`) |
| `balance` | **authoritative** post-payout balance — use it, never recompute. 16075.42 → +0.80 → 16076.22 across the bonus |
| `accumulatedWinAmount` | running total for the ROUND: 0 on the purchase, 0.8 after the winning free spin, still 0.8 after the losing one |
| `totalFreeSpinPayout` | bonus running total, **inclusive of the current round** (0.8 on the round that paid it) |
| `stake` | what THIS bet cost: `0.1` base · **`0` in free spins** · **`7.5` on the purchase** |
| `baseStake` | the real bet, `0.1`, on **every** round including the purchase — see §9 |

⚠ **`maxWin` is `0` on the purchase round** and `100000.0000` on every other. Anything that reads a
cap off a bought round gets 0. (Its unit is unconfirmed — it reads as an absolute cap mirroring
`settings.maxPayout`, not a bet multiple.)

`hasReachedMaxWin` is `false` everywhere; truncation is uncaptured (§11).

`winType` is a win **band**, not a flag: `0` on both non-winning rounds, then 2 (2.3× base stake),
4 (8×), 5 (16×). The ordering is consistent but four points do not pin the thresholds — only
`winType == 0` ⟺ no win is safe to assert.

## 6. Free-spin counters — a clean three-way split

Unlike Joker (where the award had to be inferred) and Mystic (two fields), Grandma reports all three
separately, and all three are present on every round:

| Field | Meaning | purchase | FS #2 | FS #7 |
|---|---|---|---|---|
| `wonFreeSpins` | awarded **by this round** | **15** | 0 | 0 |
| `freeSpins` | **REMAINING** after this round | 15 | 13 | 8 |
| `totalFreeSpin` | total awarded for the whole bonus | 15 | 15 | 15 |

So `is_trigger(round)` is **`wonFreeSpins > 0`** — no gating on a package field, no aliasing, and it
stays correct on a retrigger (which should raise `totalFreeSpin` too; uncaptured).

`originalTotalFreeSpins` / `originalTotalFreeSpinsPayout` shadow `totalFreeSpin` /
`totalFreeSpinPayout` exactly in every capture. They exist to separate the original bonus from a
purchased Free Spin Extension — `extensionTotalFreeSpins` and `extensionTotalFreeSpinsPayout` are 0
throughout, because no capture bought one.

## 7. State — there is **no `gameState`**

Mystic, Zeus and Joker all carry `gameState`. Grandma carries **`state` and `nextState`**:

| capture | `state` | `nextState` |
|---|---|---|
| base spin ×2 | 1 | 1 |
| **buy bonus purchase** | **1** | **2** |
| free spin ×2 | 2 | 2 |

Values match the bridge's `pending.gameState` scale (`bridge-api.md` §4): `1` base, `2` free game,
`3` buy bonus / lucky spin pending, `4` promo.

⚠ **`state` is where the round was PLAYED; `nextState` is where it goes.** The purchase is played in
base (`state: 1`) and hands over to the bonus (`nextState: 2`). This differs from Mystic, whose
trigger response already reports `gameState: 2` on the paid base spin. A profile that maps
`F_GAME_STATE` to `state` will call the purchase a base round — correct, but it means the
bonus-entry signal is `nextState`, not `state`.

Terminating a bonus drain, per `bridge-api.md` §4:

```
freeSpins == 0 || nextState == 1
```

Check **both** — max win can end a bonus with `freeSpins > 0`, and `freeSpins == 0` with
`nextState == 2` means the Free Spin Extension offer is open rather than the bonus being over.

`currentRoundType` is a useful second discriminator: `null` on base rounds, `"bonus"` on the
purchase and on every free spin.

## 8. Round identity

There is **no `refId`** in this game (Joker has one).

| round | `id` | `roundId` |
|---|---|---|
| purchase | `943027205863112705` | same |
| its free spins | per-spin, differs | `943027205863112705` — **the purchase's id** |

So a bonus is grouped by `roundId`, and the bonus's `roundId` is the id of the round that opened it.
A History ROW is one `roundId`; its detail ticket pages one bet per `id`.

⚠ **`id == roundId` does NOT hold reliably on base rounds.** `base-win-wild-x8.json` has them equal;
`base-win.json` does not (`…552928257` vs `…561316865`), on an ordinary base spin with no bonus and
no free spins. The two captures are ~15.5 hours apart, so this may be a server change rather than a
per-round property. **Unexplained — do not build a "this is a base round" test on it**, and ask the
game team which shape is current.

## 9. The Buy Bonus purchase round

`fixtures/bet-responses/buybonus-purchase.json` — the round that lands after `buyBonus:confirm`.

```jsonc
{ "id": "943027205863112705", "roundId": "943027205863112705",
  "stake": 7.5, "baseStake": 0.1, "payout": 0, "winlose": -7.5, "balance": 16075.42,
  "betType": 5, "subType": 5, "miscType": "BuyBonusA",
  "state": 1, "nextState": 2, "currentRoundType": "bonus",
  "package": { "id": 1, "name": "packagea", "buyMultiplier": 75, "freeSpins": 15 },
  "wonFreeSpins": 15, "freeSpins": 15, "totalFreeSpin": 15,
  "totalWays": 0, "wayDetails": [], "maxWin": 0,
  "wildType": null, "wildMultipliers": null,
  "view": [ {"index":0,"value":[-1,-1,-1,-1]}, /* ×5 */ ] }
```

### ⚠ There is no grid — and `bridge-api.md` says otherwise

Both `view` and `rawView` are five reels of `[-1,-1,-1,-1]`. The server sends **no reels at all** for
the purchase.

`bridge-api.md` §3 says *"The client replaces the bought round's grid with a teaser
(`isFakeBuyBonusView`). The bridge hands over the **server's** payload, before that rewrite"*, and
§8.3 says *"The purchase round carries the server's real grid."* **This capture contradicts both.**
There is no real grid to hand over. File it against the bridge doc.

Consequence: the "1 BONUS on each of reels 1–5" reveal that 4.jpg promises can only ever be graded
on the **clip**, never from the response.

### ⚠ `package` is present on free spins too — it does not identify a purchase

`package` carries `{id: 1, name: "packagea", buyMultiplier: 75, freeSpins: 15}` on the purchase **and
on every free spin of that bonus** (`null` on base rounds). Gating "this is a purchase" on
`package != null` is wrong — it is true for the whole bought bonus.

The purchase discriminators that do work, in order of directness:

1. `subType == 5` — `0` on every other captured round;
2. `miscType == "BuyBonusA"` — `"NA"` on every other captured round;
3. `stake != baseStake` together with `wonFreeSpins > 0`.

`betType` does **not** separate them: `2` on base spins, `5` on **both** the purchase and its free
spins.

> Whether a *naturally* triggered bonus also carries `package` is uncaptured. If it does, `package`
> says nothing at all about how the bonus started.

### `baseStake` survives — unlike Joker

`stake: 7.5` is the money charged (`0.1 × 75`, matching `Packages[0].BuyMultiplier`); `baseStake: 0.1`
is the real bet and **is present**. So `base_stake()` returns the bet, not the price.

This is the opposite of Joker, where `baseStake` is absent on a purchase and `base_stake()` returns
the 400 price (summary 11 §5). Grandma needs no workaround; the price ratio is simply
`stake / baseStake == package.buyMultiplier == 75`.

There is also only **one** thing called "price" here — `package` uses the unambiguous
`buyMultiplier`. Joker's 400-vs-100 trap does not exist in this payload.

## 10. Traps

- **`stake` is 0 in free spins; payouts are computed off `baseStake`.** Any per-bet ratio must use
  `baseStake`.
- **`freeSpins` is REMAINING, not awarded.** The awarded figure is `wonFreeSpins`. And `freeSpins`
  is not a loop variable on its own — see §7.
- **`wildMultipliers` is `null` on a losing round**, even with wilds on the grid. Gate multiplier
  asserts on `payout > 0`.
- **One symbol can appear in several `wayDetails`** (§4). Sum, don't index.
- **`view`, not `views`**; 4 rows, not 3.
- **No `gameState` field** (§7). No `refId` field (§8).
- **`maxWin` is 0 on the purchase round** (§5).
- **The raw bytes carry non-normalised decimals** — `0.10`, `1.60`, `16100.17000000`,
  `100000.0000`. `json.load` normalises them; never compare the response as text.
- **Use a tolerance on float sums.** Σ `wayDetails[].payout` happened to equal `rawPayout` exactly in
  all five captures, but that is binary-rounding luck, not a guarantee.
- **Balance is authoritative.** The `conservation` invariant checks
  `balance == prev − stake + payout` against it.

## 11. ⚠ What is NOT captured yet — do not guess these

1. **A natural free-spin TRIGGER.** Everything in §6–§9 about how a bonus starts comes from a
   *bought* one. Unknown: whether a natural trigger carries `package`, what its `betType` is (base
   spins are 2, the purchase and its free spins are 5), and whether it reports `state: 1` /
   `nextState: 2` the way the purchase does. ⚠ At **1 in 149 base spins** (computed from
   `docs/raw/paytable.json`), this cannot be caught by spinning — it needs a forced BO paytable.
2. **A retrigger.** 2.jpg says landing BONUS on 3/4/5 reels *during* free spins adds 8/12/15 more
   with no limit. Expected shape is `wonFreeSpins > 0` with `totalFreeSpin` rising, but nothing
   confirms it.
3. **Anything about the Free Spin Extension.** `canBuyExtension` is `false` and `extensionPhase`,
   `extensionTotalFreeSpins`, `extensionTotalFreeSpinsPayout` are all 0 in every capture — the last
   free spin captured still had 8 remaining, so the offer never opened. The reply itself is a
   separate stream (`awaitExtensionResponse()`).
   ⚠ **5.jpg says "GET 5 MORE SPINS"; the paytable's two extension packages both say
   `FreeSpins: 3`** (`extfreespin` at 14×, `extbonusa` at 15×). One of them is stale. Read
   `getExtensionOfferState().packages[].extraFreeSpins` live rather than asserting either number.
4. **A max-win truncation.** `hasReachedMaxWin: false` everywhere, and `maxWin`'s unit is
   unconfirmed. Whether `rawPayout` is the untruncated figure and `payout` the capped one is a
   plausible reading of the pair, but **nothing in these captures tests it** — they are equal in all
   five.
5. **Which server paytable name id 18 is** (§3: paid as `P4`, named `P5` by the client), whether
   `P1` really pays anywhere, and which of ids 1 / 2 is `K` and which is `J`.
6. **A winning round with no wild in any way** — the open question in §4.
7. **A second Buy Bonus package.** Only `packagea` (id 1, 75×, 15 spins) has been bought. The other
   two paytable packages are the extension offers, not main-panel purchases.

Fields that are `null` or 0 in every capture because this game does not use them — consistent with
`docs/raw/paytable.json`, which disables each one:

| field | paytable says |
|---|---|
| `anteMultiplier`, `actualAnteMultiplier` | `AnteBetSetting.Enabled: false` |
| `winRandomMultiplier` | `RandomWinMultiplier.Enabled: false` |
| `expandedReels` | `ExpandingWild.Enabled: false` |
| `luckyMultiplier` | `LuckySpinMultiplierSetting.Enabled: false` on all three packages |
| `promoResult` | no promo configured |

⚠ `luckyMultiplier` is worth watching: the **bridge** carries a full Lucky Spin surface
(`LuckySpinPopup`, `luckySpin:spin` / `luckySpin:keep`, `gameMode: LUCKY_SPIN`, `state: 3`,
`isLuckyFixed`) that this paytable turns off. Against this config, `state: 3` should be unreachable
and no lucky-spin case should be written.

## 12. Writing the profile

Grandma needs a **new `WaysGame` base class** — `scripts/game_profile.py` has `CascadeGame`,
`HoldSpinGame` and `LineGame`, and none of them fits. What the captures settle:

| | |
|---|---|
| grid | 5 reels × **4 rows**, column-major, integer symbol ids |
| ways | 1024 fixed; `PAYLINES` / `LINE_DIVISOR` are meaningless — **there is no per-line divisor**, the formula multiplies by `baseStake` directly |
| `F_STEPS` | none — one grid per round. Wrap `view` so `Bet.cascades == 1`, as Joker does |
| `F_STEP_GRID` | **`"view"`** — the base class's `"views"` reads nothing |
| `F_GAME_STATE` | **`"nextState"`** (default `"gameState"` is absent). `nextState` is the state AFTER the round, which is what `gameState` means in the other engines; `state` is where it was played, so the Buy Bonus purchase would read as base. §7 |
| `F_FREE_SPINS` | `"freeSpins"` (remaining) ✓ default |
| `F_TOTAL_FREE_SPINS` | `"totalFreeSpin"` ✓ default |
| awarded-this-round | **`wonFreeSpins`** — a field no existing profile has. `is_trigger()` is `wonFreeSpins > 0` |
| `F_BASE_STAKE` | `"baseStake"` ✓ default, and present on **every** round including the purchase |
| `F_MAX_WIN` | `"hasReachedMaxWin"` ✓ default |
| `HAS_BUY_BONUS` | true (75×, one package) |
| `HAS_ANTE_BET` | false — disabled in this paytable, and `anteBet` returns `null` |
| `HAS_DEV_MODE` | **false** — not implemented (`bridge-api.md` §3) |
| `HAS_SOUND_GETTERS` | **true** — §6 of the bridge doc lists `isSoundEnabled()` and `isMusicEnabled()` |
| `HAS_BET_POPUP` | true — the stake arrows open the denomination list |
| capabilities | wild multiplier (per round), walking wild (free spins), Buy Bonus, Free Spin Extension |

The new mechanism invariant this game wants is the ways payout check, per `wayDetails` entry:

```
payout == symbolMultiplier × totalWildMultiplier × wayCount × baseStake
```

Verified against all 8 entries in these captures. Follow `joker_payout_matches_paylines`'s lead
(summary 11 §6.2): **fail rather than pass when every round was skipped**, and test it against
mutations — the `stake`-instead-of-`baseStake` trap, a dropped `totalWildMultiplier`, and summing
one `wayDetails` entry per symbol instead of all of them.
