# Mystic paytables — deterministic fixtures for BO-forced tests

Some tests need a **server-authoritative** forced outcome that the client can't set — notably
**Reels Glow (Mah_041/042)**, which only fires on specific bonus-symbol reel layouts. The scenario
runner forces these via the BO paytable API (same mechanism as Zeus), driven by a run-level
`paytable:` declaration in the suite:

```yaml
- run:
    scenario: reels-glow
    paytable: { ref: mystic-glow-2col }     # -> games/mystic/paytables/mystic-glow-2col.json
    cases: [...]
```

On such a run the runner does, before the scenario: `ensure_paytable(AutomationTest, content)` →
`select_agent_paytable(AutomationTest)` → `update_paytable(AutomationTest, content)`, and after it:
`reset_agent_paytable()`. It needs a `bo:` block in `games/mystic/env.config.<env>.yml` (or `BO_*` env
vars) — `game_id`, `sas_entity_id`, `agent_id`, `currency_id`, `game_code`, plus `BO_TOKEN` /
`BO_REFRESH_TOKEN` / `BO_API_BASE`. Without BO configured, a `paytable:` run is **skipped with an
error** (never silently un-forced).

## Fixtures

- **`base.json`** — the game's base paytable. Sections: `BaseGame`, `FreeGame`, `Payouts`, `FreeSpins`.
  Each `BaseGame.ReelDistributions[]` (weighted 12/77/11) has 7 reels of `Symbols[{Name, Weight}]`.
  The bonus / free-spin symbol is **`FreeSpin!`**.
- **`mystic-glow-2col.json`** (Mah_041, exclusive/forced) — `FreeSpin!` forced into reels **0 & 1**
  (weight 1000, every other symbol 0 there; `FreeSpin!` = 0 in reels 2–6), across all three
  distributions. The bonus lands across the first two reels → Reels Glow SHOULD fire.
- **`mystic-fs-easy.json`** (FS-triggering tests, e.g. `freespin-zero-balance` / Mah_011) —
  **probability mode**: `FreeSpin!` set to ~**30%** of each of reels **0,1,2** (keeping the other
  symbols' relative weights); reels 3–6 untouched. Free spins trigger OFTEN but not every spin, so the
  base game still plays normally — a fast, deterministic-ish way to reach the bonus without YOLO
  spinning up to 400 times. Any scenario that must reach free spins can declare
  `paytable: { ref: mystic-fs-easy }` (needs BO config).

Only `BaseGame` is modified; `FreeGame` / `Payouts` / `FreeSpins` are untouched.

### Re-deriving (if the base changes)

```bash
# Reels Glow — 100% forced into 2 columns:
python scripts/derive_paytable.py --game mystic --base base --symbol "FreeSpin!" --reels 0,1 --out mystic-glow-2col
# Frequent free spins — 30% probability on reels 1-3 (game still plays):
python scripts/derive_paytable.py --game mystic --base base --symbol "FreeSpin!" --reels 0,1,2 --probability 0.30 --out mystic-fs-easy
```

Two modes: omit `--probability` for **exclusive forcing** (100%, deterministic layout — Reels Glow);
pass `--probability 0.30` for **frequent-but-not-forced** (30% of the reel, base game still plays).

Forcing full columns of `FreeSpin!` reliably TRIGGERS free spins, so the `reels-glow` scenario captures
the glow on the base spin and then drains the bonus. If the glow rule turns out to key off a different
reel span, adjust `--reels` (methodology: `games/zeus/paytables/paytable.md` §3).
