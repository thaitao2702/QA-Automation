# Dragon VIP paytable fixtures

**Derived from this game's own paytable — not copied from Dragon.** `base.json` is
`docs/raw/paytable.json`, byte for byte. The VIP paytable is Dragon's plus six sections
(`AnteBetBaseGame`, `AnteBetFreeSpinGame`, `AnteBetSetting`, `PremiumReel_Setting`,
`PremiumReel_BaseGame`, `PremiumReel_FreeGame`) and it has **different `BaseGame` weights**. Dragon's
fixtures would push the wrong base game to BO and delete both features from it.

| File | Sections changed | Reaches | Cases |
|---|---|---|---|
| `base.json` | — | the untouched paytable | — |
| `fs-trigger3-countdown.json` | `BaseGame`, `FreeSpinGame` | 3-coin trigger, no coin ever added: 3 → 2 → 1 → 0 | DV-30/32/33/35/36/38 |
| `fs-trigger3-reset.json` | `BaseGame`, `FreeSpinGame` | a coin lands (p = 0.30 per empty cell) without filling the grid → counter resets | DV-34 |
| `fs-trigger3-addcoin.json` | `BaseGame`, `FreeSpinGame` | every empty cell fills on respin 1 → 9 coins during the feature | DV-37 |
| `fs-trigger9.json` | `BaseGame`, `FreeSpinGame` | coins on all three base reels → 9 on the trigger, no respin | DV-31 |
| `ante-trigger3-countdown.json` | `AnteBetBaseGame`, `AnteBetFreeSpinGame` | the countdown path, but ONLY on a spin bet with Ante | DV-57/58 |
| `premium-trigger3-countdown.json` | every `PremiumReel_BaseGame` / `PremiumReel_FreeGame` entry | the countdown path, but ONLY on a spin bet with a Premium Reel package | DV-64/65 |

The two feature fixtures leave `BaseGame` alone on purpose. A plain spin keeps its natural odds, so
a trigger inside the run proves the server really spun the feature's own reels. `hold-spin-complete`
stops as unreached the moment a spin is not bet with the feature, so it never hunts at plain odds.

Each file forces the smallest trigger that reaches its path (Rule 6): COIN is exclusive on reel 1, so
exactly 3 coins land. `CoinWeight` is a **per-empty-cell** coin probability out of 1000, not a
next-count distribution. The full model, and why the reset fixture is p = 0.30 and not p = 1, is in
[`games/zeus/paytables/paytable.md`](../../zeus/paytables/paytable.md).

## Regenerate

After any math change to `docs/raw/paytable.json`:

```bash
cp games/dragon-vip/docs/raw/paytable.json games/dragon-vip/paytables/base.json
D="python scripts/derive_paytable.py --game dragon-vip --base base --symbol COIN"
$D --reels 0     --section BaseGame --coin-weight 0    --coin-section FreeSpinGame --out fs-trigger3-countdown
$D --reels 0     --section BaseGame --coin-weight 300  --coin-section FreeSpinGame --out fs-trigger3-reset
$D --reels 0     --section BaseGame --coin-weight 1000 --coin-section FreeSpinGame --out fs-trigger3-addcoin
$D --reels 0,1,2 --section BaseGame --coin-weight 0    --coin-section FreeSpinGame --out fs-trigger9
$D --reels 0 --section AnteBetBaseGame      --coin-weight 0 --coin-section AnteBetFreeSpinGame  --out ante-trigger3-countdown
$D --reels 0 --section PremiumReel_BaseGame --coin-weight 0 --coin-section PremiumReel_FreeGame --out premium-trigger3-countdown
```
