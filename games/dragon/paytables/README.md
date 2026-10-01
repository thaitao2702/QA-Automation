# Dragon paytable fixtures

These five files are **copies of Zeus's**, unchanged:

| File | Reaches | Cases |
|---|---|---|
| `base.json` | the untouched paytable | — |
| `fs-trigger3-countdown.json` | 3-coin trigger, no coin ever added: 3 → 2 → 1 → 0 | DR-30/32/33/35/36/38 |
| `fs-trigger3-reset.json` | a coin lands (p = 0.30 per empty cell) without filling the grid → counter resets | DR-34 |
| `fs-trigger3-addcoin.json` | every empty cell fills on respin 1 → 9 coins during the feature | DR-37 |
| `fs-trigger9.json` | coins on all three base reels → 9 on the trigger, no respin | DR-31 |

**Why copying is correct.** Dragon's server paytable (`games/dragon/docs/raw/paytable.json`) is
byte-identical to Zeus's `base.json`, which every Zeus fixture was derived from. The same weights
therefore reach the same paths. Check it again before trusting these fixtures after any math change:

```bash
cmp games/dragon/docs/raw/paytable.json games/dragon/paytables/base.json
```

If that ever differs, re-derive these from Dragon's own paytable instead of copying.

How the file is structured, what `FreeSpinGame.CoinWeight` means (a **per-empty-cell** probability,
not a next-count distribution), and why the reset fixture is p = 0.30 and not p = 1 are all in
[`games/zeus/paytables/paytable.md`](../../zeus/paytables/paytable.md).
