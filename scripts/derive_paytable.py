#!/usr/bin/env python3
"""
Derive a FORCED-REEL paytable variant from a base paytable, for deterministic tests.

Reels Glow (Mah_041/042) only fires on specific bonus-symbol layouts, which are server-authoritative.
This tool takes the game's base paytable and forces a symbol (e.g. the bonus / free-spin symbol) into
chosen reels — setting its Weight high in the target reels and 0 elsewhere, and (optionally) zeroing
the OTHER symbols in those target reels so ONLY that symbol can land there. Same methodology Zeus uses
by hand (see games/zeus/paytables/paytable.md §3 "Weight collapsing for deterministic outcomes").

It operates on the standard structure `ReelDistributions[].Reels[].Symbols[{Name, Weight}]` found under
a game section (BaseGame / FreeSpinGame). ⚠ Confirm this matches the game's real paytable before use.

A section may be a LIST of sections as well as one (Dragon VIP's `PremiumReel_BaseGame` holds one per
package), and `--section` takes a comma-separated list; every section named is forced the same way.

Two modes:
  * EXCLUSIVE (default) — force the symbol to 100% of the target reels (weight high, others 0): a
    deterministic layout (e.g. Reels Glow — the bonus definitely lands in those reels).
  * PROBABILITY (`--probability 0.30`) — set the symbol to ~30% of each target reel's total, keeping
    the other symbols: FREQUENT but not forced, so the base game still plays normally (e.g. an
    easy-free-spin paytable for scenarios that must reach the bonus quickly).

Hold & Spin games also need the respin coin weight: `--coin-weight N --coin-section S` sets EVERY cell
of every `CoinDistributions[].CoinWeight` table under S to N. The cell is a PER-EMPTY-CELL coin
probability out of 1000 (games/zeus/paytables/paytable.md): 0 = no coin ever lands (count-down),
1000 = every empty cell fills on the first respin.

USAGE (Mystic's bonus symbol is "FreeSpin!"; section BaseGame; reels are 0-based columns of the 7x7):
    # Reels Glow — force FreeSpin! into 2 columns:
    python scripts/derive_paytable.py --game mystic --base base --symbol "FreeSpin!" --reels 0,1 \
        --out mystic-glow-2col
    # Frequent free spins — FreeSpin! at 30% on reels 1-3 (game still plays normally):
    python scripts/derive_paytable.py --game mystic --base base --symbol "FreeSpin!" --reels 0,1,2 \
        --probability 0.30 --out mystic-fs-easy
    # ...and pinned to the SHORTEST award: impossible on reels 4-5, so a trigger is always 3 reels:
    python scripts/derive_paytable.py --game joker --base base --symbol "FreeSpin!" --reels 0,1,2 \
        --probability 0.30 --section BaseGame --zero-elsewhere --out joker-fs-easy
    # Hold & Spin on 3 coins that counts down with no new coin (Dragon VIP):
    python scripts/derive_paytable.py --game dragon-vip --base base --symbol COIN --reels 0 \
        --section BaseGame --coin-weight 0 --coin-section FreeSpinGame --out fs-trigger3-countdown

The suite then references it: `paytable: { ref: mystic-glow-2col }` on a `reels-glow` run.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_HIGH = 1000     # forced weight for the target symbol in target reels


def _sections(paytable: dict, section: str) -> list:
    """Every section dict `section` names: a comma-separated list, each entry either one section or
    a list of them. Exits naming the available keys when one is missing."""
    out = []
    for name in (s.strip() for s in section.split(",") if s.strip()):
        sec = paytable.get(name)
        if isinstance(sec, dict):
            out.append(sec)
        elif isinstance(sec, list) and any(isinstance(s, dict) for s in sec):
            out.extend(s for s in sec if isinstance(s, dict))
        else:
            raise SystemExit(f"section '{name}' not found in the base paytable (keys: {list(paytable)})")
    return out


def _iter_reel_distributions(section: dict):
    """Yield each Reels list under a section's ReelDistributions (list of {Reels:[...]}) —
    tolerant of either `ReelDistributions` or a bare `Reels` key."""
    for dist in section.get("ReelDistributions", []) or []:
        reels = dist.get("Reels")
        if isinstance(reels, list):
            yield reels
    if isinstance(section.get("Reels"), list):
        yield section["Reels"]


def zero_symbol_on_reels(paytable: dict, symbol: str, reels: list[int],
                         section: str = "BaseGame") -> dict:
    """Return a deep-copied paytable with `symbol` set to weight 0 on the TARGET reels, leaving
    every other reel alone. The inverse of `--zero-elsewhere`, and the only way to express a
    NEGATIVE precondition.

    ⚠ It is what makes a "this cannot win" fixture actually unable to win. A ways win runs from
    reel 1 rightwards with no gap, so "P1 on reels 1-2 and none on reel 3" looks like a broken
    chain -- until a WILD lands on reel 3 and substitutes, and the chain is whole again. Zeroing
    the wild on that reel is the difference between testing the rule and testing luck.

    `--probability 0` cannot say this: the probability mode takes an OPEN interval, because 0 there
    would silently mean "unchanged" in the arithmetic that keeps the other symbols' ratios.
    """
    pt = copy.deepcopy(paytable)
    target, touched = set(reels), 0
    for sec in _sections(pt, section):
        for reel_list in _iter_reel_distributions(sec):
            for idx, reel in enumerate(reel_list):
                syms = reel.get("Symbols") if isinstance(reel, dict) else None
                if not isinstance(syms, list) or idx not in target:
                    continue
                for s in syms:
                    if s.get("Name") == symbol:
                        s["Weight"] = 0
                        touched += 1
    if not touched:
        raise SystemExit(f"--zero {symbol!r} matched no reel in section {section!r} -- check the "
                         f"symbol name against the paytable")
    return pt


def set_symbol_probability(paytable: dict, symbol: str, reels: list[int], fraction: float,
                           section: str = "BaseGame", zero_elsewhere: bool = False) -> dict:
    """Return a deep-copied paytable with `symbol` set to `fraction` (0..1) of each TARGET reel's
    total weight, keeping the OTHER symbols' relative weights. Non-target reels are left unchanged.

    So `symbol` lands ~`fraction` of the time on the target reels — frequent triggers WITHOUT
    forcing 100% (base game still plays normally). e.g. FreeSpin! at 0.30 on reels 0,1,2 makes free
    spins trigger often but not every spin.

    `zero_elsewhere` sets the symbol to weight 0 on every NON-target reel. That is what caps a
    reel-counted award at its minimum (Rule 6): with the scatter raised on exactly three reels and
    impossible on the other two, a trigger is always the 3-reel, shortest award — never the 4- or
    5-reel one that costs the run another 3 to 8 rounds of animation. Leave it off (the default,
    and what every Chaos fixture uses) when the other reels' real rate is wanted."""
    if not 0.0 < fraction < 1.0:
        raise SystemExit(f"--probability must be in (0,1); got {fraction}")
    pt = copy.deepcopy(paytable)
    target, touched = set(reels), 0
    for sec in _sections(pt, section):
        for reel_list in _iter_reel_distributions(sec):
            for idx, reel in enumerate(reel_list):
                syms = reel.get("Symbols") if isinstance(reel, dict) else None
                if not isinstance(syms, list):
                    continue
                if idx not in target:
                    if zero_elsewhere:
                        for s in syms:
                            if s.get("Name") == symbol:
                                s["Weight"] = 0
                    continue
                others = sum(s.get("Weight", 0) for s in syms if s.get("Name") != symbol)
                new_w = max(1, round(fraction / (1.0 - fraction) * others))   # symbol/(symbol+others) == fraction
                if any(s.get("Name") == symbol for s in syms):
                    for s in syms:
                        if s.get("Name") == symbol:
                            s["Weight"] = new_w
                else:
                    syms.append({"Name": symbol, "Weight": new_w})
                touched += 1
    if touched == 0:
        raise SystemExit(f"no target reels found under {section}.ReelDistributions -- check --reels/--section.")
    return pt


def force_symbol_into_reels(paytable: dict, symbol: str, reels: list[int],
                            section: str = "BaseGame", exclusive: bool = True,
                            high_weight: int = _HIGH) -> dict:
    """Return a deep-copied paytable with `symbol` forced into `reels` (0-based) of `section`.

    In each reel: target reels -> symbol Weight = high_weight (and, if exclusive, every other symbol
    Weight = 0 so only `symbol` can land); non-target reels -> symbol Weight = 0."""
    pt = copy.deepcopy(paytable)
    target = set(reels)
    touched = 0
    for sec in _sections(pt, section):
        for reel_list in _iter_reel_distributions(sec):
            for idx, reel in enumerate(reel_list):
                syms = reel.get("Symbols") if isinstance(reel, dict) else None
                if not isinstance(syms, list):
                    continue
                in_target = idx in target
                for s in syms:
                    name = s.get("Name")
                    if name == symbol:
                        s["Weight"] = high_weight if in_target else 0
                    elif in_target and exclusive:
                        s["Weight"] = 0
                touched += 1
    if touched == 0:
        raise SystemExit(f"no Reels/Symbols found under {section}.ReelDistributions -- structure "
                         f"mismatch; inspect the base paytable and adjust --section / this tool.")
    return pt


def set_coin_weight(paytable: dict, weight: int, section: str = "FreeSpinGame") -> dict:
    """Return a deep-copied paytable with every `CoinDistributions[].CoinWeight` cell under `section`
    set to `weight` (a per-empty-cell coin probability out of `TotalCoinWeight`, normally 1000)."""
    if not 0 <= weight <= 1000:
        raise SystemExit(f"--coin-weight must be in [0, 1000]; got {weight}")
    pt = copy.deepcopy(paytable)
    touched = 0
    for sec in _sections(pt, section):
        for dist in sec.get("CoinDistributions", []) or []:
            table = dist.get("CoinWeight")
            if not isinstance(table, dict):
                continue
            for row in table.values():
                if isinstance(row, dict):
                    for key in row:
                        row[key] = weight
                        touched += 1
    if touched == 0:
        raise SystemExit(f"no CoinDistributions[].CoinWeight found under {section} -- check --coin-section.")
    return pt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default="mystic")
    ap.add_argument("--base", default="base", help="base fixture stem under games/{game}/paytables/")
    ap.add_argument("--symbol", required=True, help="symbol Name to force (e.g. the bonus/free-spin symbol)")
    ap.add_argument("--reels", required=True, help="comma-separated 0-based reel indices to target")
    ap.add_argument("--section", default="BaseGame",
                    help="paytable section(s), comma-separated (BaseGame | FreeGame | a list-valued section)")
    ap.add_argument("--probability", type=float, default=None,
                    help="PROBABILITY mode: set the symbol to this fraction (0..1) of each target reel's "
                         "total, keeping other symbols (frequent, not forced). Omit for exclusive forcing.")
    ap.add_argument("--no-exclusive", action="store_true",
                    help="exclusive mode only: do NOT zero the other symbols in target reels")
    ap.add_argument("--zero-elsewhere", action="store_true",
                    help="probability mode only: also set the symbol to weight 0 on every NON-target "
                         "reel, so a reel-counted award can only ever be the 3-reel minimum (Rule 6)")
    ap.add_argument("--zero", action="store_true",
                    help="set the symbol to weight 0 on the TARGET reels (the inverse of "
                         "--zero-elsewhere). For a fixture that must NOT be able to win: zero the "
                         "wild on the reel where a run is supposed to break, or the scatter "
                         "everywhere so a bonus cannot interrupt the run.")
    ap.add_argument("--coin-weight", type=int, default=None,
                    help="Hold & Spin: set every CoinWeight cell under --coin-section to this (0..1000)")
    ap.add_argument("--coin-section", default="FreeSpinGame",
                    help="section(s) whose CoinDistributions --coin-weight rewrites, comma-separated")
    ap.add_argument("--out", required=True, help="output fixture stem under games/{game}/paytables/")
    args = ap.parse_args()

    pdir = REPO / "games" / args.game / "paytables"
    base_path = pdir / f"{args.base}.json"
    if not base_path.exists():
        sys.exit(f"base paytable not found: {base_path}\n"
                 f"Export the game's base paytable to that path first (from BO or a known fixture).")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    reels = [int(x) for x in args.reels.split(",") if x.strip() != ""]
    if args.zero:                                    # negative precondition
        derived = zero_symbol_on_reels(base, args.symbol, reels, args.section)
    elif args.probability is not None:               # frequent-but-not-forced
        derived = set_symbol_probability(base, args.symbol, reels, args.probability, args.section,
                                         zero_elsewhere=args.zero_elsewhere)
    else:                                            # 100% exclusive forcing (deterministic layout)
        derived = force_symbol_into_reels(base, args.symbol, reels, args.section,
                                          exclusive=not args.no_exclusive)
    if args.coin_weight is not None:
        derived = set_coin_weight(derived, args.coin_weight, args.coin_section)
    out_path = pdir / f"{args.out}.json"
    out_path.write_text(json.dumps(derived, indent=2), encoding="utf-8")
    coin = (f", CoinWeight={args.coin_weight} in {args.coin_section}"
            if args.coin_weight is not None else "")
    zeroed = ", 0 elsewhere" if (args.probability is not None and args.zero_elsewhere) else ""
    if args.zero:
        zeroed = " -> weight 0"
    print(f"derived {out_path.relative_to(REPO)}  (symbol={args.symbol} -> reels {reels} of "
          f"{args.section}{zeroed}{coin})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
