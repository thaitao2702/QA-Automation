#!/usr/bin/env python3
"""Record what the PYTHON scenario runner answers, so the TypeScript port can be held to it.

Run from the repo root:  python scenario-runner/baseline/make_baseline.py

Writes one JSON file per area into scenario-runner/baseline/. The TS tests (scenario-runner/test/)
replay the same inputs and must produce the same outputs. Regenerate only on purpose: a changed
baseline means the PYTHON behaviour changed, and the port has to follow it.

Every call is recorded as {"ok": value} or {"err": "<ExceptionType>"}, so a Python raise is part of
the contract too. Sets become sorted lists, tuples become lists.
"""
from __future__ import annotations

import copy
import glob
import json
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import game_profile  # noqa: E402

# ── serialisation ────────────────────────────────────────────────────────────────────


def plain(v):
    """JSON-safe copy: sets sorted, tuples listed, non-finite floats tagged."""
    if isinstance(v, float) and not math.isfinite(v):
        return {"__float__": repr(v)}
    if isinstance(v, (set, frozenset)):
        return sorted((plain(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    return v


def call(fn, *args, **kwargs):
    try:
        return {"ok": plain(fn(*args, **kwargs))}
    except Exception as e:  # noqa: BLE001 -- the exception TYPE is the recorded answer
        return {"err": type(e).__name__}


def write(name, data):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    print(f"wrote {os.path.relpath(path, ROOT)}")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ── inputs ───────────────────────────────────────────────────────────────────────────

def fixtures():
    """{label: raw} for every captured bet reply and launch payload, plus edge cases."""
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "games", "*", "fixtures", "*", "*.json"))):
        rel = os.path.relpath(p, os.path.join(ROOT, "games")).replace("\\", "/")
        out[rel] = load(p)
    return out


SYNTHETIC_BETS = {
    "syn/empty": {},
    "syn/rejected": {"status": 0, "code": -2002, "msg": "Insufficient balance", "timestamp": 1},
    "syn/rejected-unknown": {"status": 0, "code": -7777, "timestamp": 1},
    "syn/status-ok-negative-code": {"status": 1, "code": -1, "timestamp": 1},
    "syn/status-string": {"status": "1", "code": "0", "timestamp": 1, "result": {"payout": 1}},
    "syn/status-junk": {"status": "x", "code": 0},
    "syn/envelope-no-timestamp": {"status": 1, "code": 0, "result": {"payout": 2, "stake": 1}},
    "syn/string-numbers": {"stake": "0.6", "baseStake": "0.6", "payout": "1.2", "balance": "100",
                           "freeSpins": "3", "totalFreeSpin": "12", "gameState": 2},
    "syn/junk-numbers": {"stake": "abc", "payout": None, "anteMultiplier": "x", "premiumReelId": "y",
                         "premiumReelMultiplier": [], "maxWin": "z", "accumulatedWinAmount": "q"},
    "syn/nested-null": {"freeSpinInfo": None, "gameState": 1, "result": None},
    "syn/nested-fs": {"freeSpinInfo": {"freespins": 2, "totalFreeSpin": 3}, "gameState": 2,
                      "coinPositions": [{"row": 0, "col": 1, "coinValue": 1.5},
                                        {"row": "1", "col": 2, "coinValue": "2.25"},
                                        {"row": None, "col": 0}, "junk"],
                      "result": {"views": [[13, 13, 1], {"value": [13, 9, 2]}, [1, 2, 3]]}},
    "syn/ante": {"stake": 1.8, "baseStake": 0.6, "anteMultiplier": 3, "payout": 4.5,
                 "result": {"views": [{"id": 1, "value": [11, 9, 5]}, {"id": 2, "value": [9, 9, 11]},
                                      {"id": 3, "value": [5, 11, 11]}, {"id": 4, "value": [1, 2, 3]},
                                      {"id": 5, "value": [5, 5, 4]}]},
                 "paylines": [{"payline": 1, "payoutRate": 150, "winType": "P2-WILD!-P2-WILD!-P3"},
                              {"payline": -1, "payoutRate": 1, "winType": "FreeSpin"},
                              {"payline": 2, "payoutRate": "x", "winType": "WILD!-WILD!-J"},
                              {"payline": 3, "payoutRate": 4, "winType": "J-J-J-A-K"},
                              {"payline": 4, "winType": ""}, "junk"],
                 "scatterMultiplier": 20, "randomMultiplier": 2, "betLines": 20,
                 "maxWin": 0, "accumulatedWinAmount": 12, "rawPayout": 30},
    "syn/premium": {"stake": 3, "baseStake": 1, "premiumReelId": 23, "premiumReelMultiplier": 3,
                    "anteMultiplier": 0},
    "syn/stake-above-base": {"stake": 2, "baseStake": 1},
    "syn/cascade-steps": {"payout": 3, "result": [{"payout": 1, "winnings": [{}, {}], "multiplierLevel": 1},
                                                  {"payout": "2", "winnings": [{}], "multiplierLevel": 2},
                                                  {"payout": 0, "winnings": [], "multiplierLevel": 3},
                                                  {"payout": "bad"}, 7]},
    "syn/cascade-no-payout": {"payout": 2, "steps": [{"views": []}, {"views": []}, {"views": []}]},
    "syn/cascade-winnings-only": {"result": [{"winnings": [1]}, {"winnings": []}, {"winnings": None}]},
    "syn/grandma-purchase": {"subType": 5, "miscType": "BuyBonusA", "stake": 75, "baseStake": 1,
                             "nextState": 2, "state": 1, "freeSpins": 8, "totalFreeSpin": 8,
                             "wonFreeSpins": 8, "package": {"freeSpins": 8},
                             "view": [{"index": 0, "value": [11, 19, 5, 6]}, {"index": 1, "value": [11, 2, 3, 4]},
                                      {"index": 2, "value": [11, 11, 19, 7]}, {"index": 3, "value": [1, 2, 3, 4]},
                                      {"index": 4, "value": [8, 18, 19, 5]}],
                             "wayDetails": [{"symbolId": 5, "symbolMultiplier": 0.5, "totalWildMultiplier": 2,
                                             "wayCount": 3, "payout": 3},
                                            {"symbolId": 6, "symbolMultiplier": "x", "wayCount": 1},
                                            {"symbolId": 7, "symbolMultiplier": 1, "totalWildMultiplier": 0,
                                             "wayCount": 2}],
                             "wildMultipliers": [{"symbolId": 3, "multiplierValue": 9},
                                                 {"symbolId": 19, "multiplierValue": "5"}]},
    "syn/joker-buy": {"freeSpins": 20, "freeSpinPackage": {"freeSpins": 20, "price": 100},
                      "luckyMultiplier": 0, "multiplier": 2.5,
                      "result": {"rawViews": [{"id": 1, "value": [1, 2, 3]}, {"id": 2, "value": [4, 9, 6]}],
                                 "views": [{"id": 1, "value": [1, 2, 3]}, {"id": 2, "value": [9, 9, 9]}]}},
    "syn/maxwin-mid-bonus": {"payout": 2.0, "rawPayout": 12.0, "accumulatedWinAmount": 10.0, "maxWin": 10,
                             "baseStake": 1, "hasReachedMaxWin": True},
}

SYNTHETIC_INFOS = {
    "syn/info-empty": {},
    "syn/info-envelope": {"status": 1, "code": 0, "timestamp": 1,
                          "result": {"pending": {"freeSpins": 3, "gameState": 2}, "settings": {"maxPayout": 50}}},
    "syn/info-lucky": {"pending": {"freeSpins": 20, "gameState": 3}},
    "syn/info-extension": {"pending": {"freeSpins": 0, "gameState": 2, "canBuyExtension": True}},
    "syn/info-promo": {"pending": {"freeSpins": 0, "gameState": 4}, "needToStartPromo": True},
    "syn/info-maxwin-state": {"pending": {"freeSpins": 0, "gameState": 2}},
    "syn/info-zeus-flat": {"pending": {"freeSpins": 2, "freespins": 0}},
    "syn/info-general": {"settings": {"general": {"maxPayout": "50", "maxMultiplier": 500,
                                                  "winFormula": [{"name": "big", "min": 6},
                                                                 {"name": "MEGA ", "min": "15"},
                                                                 {"name": "SMALL", "min": 1},
                                                                 {"name": "ULTRA", "min": "x"}, 3]},
                                      "defaultBetLevel": 4}},
    "syn/info-caps-zero": {"settings": {"maxPayout": 0, "maxMultiplier": -1}},
    "syn/info-pending-null": {"pending": None, "settings": None, "lastBet": None},
}


# ── py-compat ────────────────────────────────────────────────────────────────────────

def py_compat():
    from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

    def quant(v, places, mode):
        try:
            d = Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            return None
        q = Decimal(1).scaleb(-places)
        return str(d.quantize(q, rounding=ROUND_DOWN if mode == "down" else ROUND_HALF_UP))

    nums = [2.675, 0.125, 0.5, 1.5, 2.5, -0.5, -2.5, 0.1 + 0.2, 1 / 3, 15141.436, 1e-7, 123.456789,
            0.0, -1.005, 1.005, 2.5e-10, 0.6 * 3, 0.3 * 3, 1.0000000000000002, 7.5, 8.5, -7.5, 1e15 + 0.5,
            4.35, 0.045, 2.0000000005, 10.000000000049999]
    rounds = []
    for x in nums:
        for nd in (None, 0, 1, 2, 3, 6, 10):
            rounds.append({"x": x, "nd": nd, **call(lambda: round(x) if nd is None else round(x, nd))})
    reprs = [{"x": x, "s": repr(x)} for x in
             [0.1, 1e-05, 1.5e-05, 0.0001, 0.00012, 123456789012345.6, 1e16, 1.2345e20, 5.6, 15141.436,
              -0.001, 1e-7, 2.5e-10, (2 / 3) * 1e-5, 1e22, -1.5e300, 9.999999999999999e15, 0.30000000000000004]]
    sums = [{"xs": xs, "s": sum(xs)} for xs in
            [[0.1] * 10, [0.1, 0.2], [1e16, 1.0, -1e16], [0.6, 0.6, 0.6], [5.6, 2.4, 0.195, 0.005],
             [0.1, 0.7, 0.3, 1e-9, 12.5], [], [3.3] * 7, [-0.1, 0.3, -0.7, 1.1]]]
    conv_inputs = ["3", " 3 ", "3.5", "1_000", "", "abc", "1e3", ".5", "5.", "inf", "-inf", "nan", "+2",
                   "-0", "0x10", "1__0", "_1", "1e", "e5", "Infinity", True, False, None, [], {}, 3.7, -3.7,
                   0, "  -2.5e-3 "]
    conv = [{"v": v, "int": call(int, v), "float": call(float, v)} for v in conv_inputs]
    quants = []
    for v in [0.195, 15069.237, 0.005, 1.005, 2.675, -0.195, 7, 7.0, "0.195", "abc", None, True, 1e-7,
              123456.785, "1e2", " 2.5 ", "1_0.5", "-0.001", 0, "", "5.", ".5"]:
        for places in (0, 2, 3):
            for mode in ("half_up", "down"):
                quants.append({"v": v, "places": places, "mode": mode, "s": quant(v, places, mode)})
    strs = [{"v": v, "str": str(v), "repr": repr(v)} for v in
            [None, True, False, 0, 5, -3, 1.5, "a", "it's", 'say "hi"', "both ' and \"", "tab\tnew\nline",
             "back\\slash", [1, "a", None, [True]], {"a": 1, "b": [1.5, "x"]}, [], {}, "ünï", "\x01"]]
    import random
    rand = []
    for seed in [0, 1, 42, 123456, 999999, 2 ** 40 + 7]:
        r = random.Random(seed)
        rand.append({"seed": seed, "sample16_7": random.Random(seed).sample(list(range(16)), 7),
                     "sample100_9": random.Random(seed).sample(list(range(100)), 9),
                     "sample16_16": random.Random(seed).sample(list(range(16)), 16),
                     "random": [r.random() for _ in range(3)],
                     "randrange": random.Random(seed).randrange(1000000)})
    return {"round": rounds, "repr": reprs, "sum": sums, "conv": conv, "quantize": quants, "str": strs,
            "random": rand}


# ── profiles ─────────────────────────────────────────────────────────────────────────

PROFILES = ["mystic", "zeus", "dragon", "dragon-vip", "chaos", "joker", "grandma"]
RAW_METHODS = [
    "reply_error", "unwrap", "free_spins", "package_spins", "won_free_spins", "game_state", "steps",
    "is_trigger", "base_stake", "ante_multiplier", "is_ante_round", "premium_reel_id",
    "premium_reel_multiplier", "max_win_cap", "round_won_before", "max_win_left",
    # hold-spin
    "coin_positions", "coin_count", "coin_total", "grid_full",
    # line / ways
    "grid", "multiplier",
    # chaos
    "scatters", "wilds", "p1_ways", "scatter_reels", "scatter_ways", "expected_fs_award", "bet_lines",
    "line_bet_of", "win_lines", "wild_reels", "scatter_multiplier", "expected_line_win",
    "expected_scatter_win", "expected_raw_payout", "expected_payout", "raining_wild_expected",
    # joker
    "raw_reels", "reels", "expanded_reels", "lucky_multiplier", "random_multiplier",
    # grandma
    "is_purchase", "bonus_reels", "expected_award", "max_scatters_on_a_reel", "wild_cells",
    "way_details", "wild_multiplier",
]
INFO_METHODS = ["game_info", "pending", "pending_free_spins", "pending_is_bonus", "pending_resume",
                "promo_offered", "last_bet", "settings", "default_bet_level", "win_formula",
                "big_win_threshold"]
CAPS = {"none": None, "set": {"max_payout": 10.0, "max_multiplier": 500.0},
        "mult-only": {"max_payout": None, "max_multiplier": 2.5}}
NAMES_TO_RESOLVE = ["", "spin", "stake_up", "stakeIncrease", "stakeDecrease", "bet:close", "auto:count:4",
                    "bet:row:12", "auto:count:x", "anteBet", "auto:stop", "settings", "history", "info",
                    "settings:history", "unknownControl", "message:ok", "historyDetail:next", "buyBonus",
                    "trophy", "lucky_keep", "auto_count", "bet_row", "ante"]
POPUPS_TO_RESOLVE = ["", "bet", "denominations", "autoPlay", "autoplay", "history", "setting", "bigWin",
                     "freeSpinResult", "promo", "nonexistent", "lucky_spin", "ante_notify", "anteNotify"]


def attributes(g):
    """Every public UPPER_CASE constant plus name/mechanism, as the class resolves them."""
    out = {}
    for k in sorted(dir(g)):
        if k.startswith("_"):
            continue
        v = getattr(g, k)
        if callable(v):
            continue
        if k.isupper() or k in ("name", "mechanism"):
            out[k] = plain(v)
    return out


def profile_case(g, label, raw):
    rec = {}
    for m in RAW_METHODS:
        if hasattr(g, m):
            rec[m] = call(getattr(g, m), raw)
    for caps_label, caps in CAPS.items():
        g.launch_caps = copy.deepcopy(caps)
        rec[f"max_win_cap@{caps_label}"] = call(g.max_win_cap, raw)
        rec[f"max_win_left@{caps_label}"] = call(g.max_win_left, raw)
        rec[f"max_win_left@{caps_label}+cap7"] = call(g.max_win_left, raw, 7.0)
    g.launch_caps = None
    for th in (None, 6, 0.5, "x"):
        rec[f"is_big_win@{th}"] = call(g.is_big_win, raw, th)
    steps = call(g.steps, raw)
    if "ok" in steps:
        payout = raw.get("payout") if isinstance(raw, dict) else None
        rec["cluster_wins"] = call(g.cluster_wins, steps["ok"], payout if isinstance(payout, (int, float)) else 0.0)
        rec["cluster_wins@payout0"] = call(g.cluster_wins, steps["ok"])
        rec["win_clusters"] = call(g.win_clusters, steps["ok"])
        if hasattr(g, "multiplier_ladder"):
            rec["multiplier_ladder"] = call(g.multiplier_ladder, steps["ok"])
    if hasattr(g, "reel_counts"):
        for sym in (None, 11, "11", "FreeSpin", 9, "WILD", 19, 5, 13):
            rec[f"reel_counts@{sym}"] = call(g.reel_counts, raw, sym)
            rec[f"symbol_count@{sym}"] = call(g.symbol_count, raw, sym)
            rec[f"reels_with@{sym}"] = call(g.reels_with, raw, sym)
        for reels in ((1, 3, 5), (1, 2), (6,)):
            rec[f"ways@5@{list(reels)}"] = call(g.ways, raw, 5, reels)
    if hasattr(g, "expected_line_rate") and isinstance(raw, dict):
        lines = raw.get("paylines") if isinstance(raw.get("paylines"), list) else []
        rec["expected_line_rate"] = [call(g.expected_line_rate, p if isinstance(p, dict) else None) for p in lines]
    if hasattr(g, "way_payout") and isinstance(raw, dict):
        ways = raw.get("wayDetails") if isinstance(raw.get("wayDetails"), list) else []
        rec["way_payout"] = [call(g.way_payout, w, raw.get("baseStake")) for w in ways if isinstance(w, dict)]
    return rec


def info_case(g, raw):
    rec = {m: call(getattr(g, m), raw) for m in INFO_METHODS}
    g.launch_caps = {"max_payout": 99.0, "max_multiplier": None}
    rec["remember_launch"] = call(g.remember_launch, raw)
    rec["launch_caps_after"] = plain(g.launch_caps)
    g.launch_caps = None
    return rec


def profiles():
    fx = fixtures()
    bets = {k: v for k, v in fx.items() if "/bet-responses/" in k}
    infos = {k: v for k, v in fx.items() if "/game-info/" in k}
    bets.update(SYNTHETIC_BETS)
    # The bet fixtures are envelopes; the inner rounds are what most methods read.
    for k, v in list(bets.items()):
        if isinstance(v, dict) and isinstance(v.get("result"), dict) and "timestamp" in v:
            bets[k + "#inner"] = v["result"]
    # The launch payloads carry a lastBet and a pending: both are rounds too.
    for k, v in infos.items():
        inner = v.get("result") if isinstance(v.get("result"), dict) else v
        if isinstance(inner.get("lastBet"), dict):
            bets[k + "#lastBet"] = inner["lastBet"]
    infos.update(SYNTHETIC_INFOS)
    for k, v in list(infos.items()):
        if isinstance(v, dict) and isinstance(v.get("result"), dict):
            infos[k + "#inner"] = v["result"]

    write("profiles_inputs.json", {"bets": bets, "infos": infos})
    out = {"profiles": {}}
    for name in PROFILES:
        g = game_profile.get(name)
        g.launch_caps = None
        rec = {"attributes": attributes(g), "bets": {}, "infos": {}, "dialect": {}}
        for label, raw in bets.items():
            rec["bets"][label] = profile_case(g, label, raw)
        for label, raw in infos.items():
            rec["infos"][label] = info_case(g, raw)
        d = rec["dialect"]
        d["button"] = {r: call(g.button, r) for r in sorted(g.BUTTONS)}
        d["button@i=3"] = {r: call(g.button, r, i=3) for r in sorted(g.BUTTONS)}
        d["popup"] = {r: call(g.popup, r) for r in sorted(g.POPUPS)}
        d["resolve"] = {n: call(g.resolve, n) for n in NAMES_TO_RESOLVE}
        d["via"] = {n: call(g.via, n) for n in NAMES_TO_RESOLVE}
        d["resolve_popup"] = {n: call(g.resolve_popup, n) for n in POPUPS_TO_RESOLVE}
        d["display_amount"] = [{"v": v, **call(g.display_amount, v)} for v in
                               [0.195, 15069.237, 5.6, 7, "1.005", None, "x", -0.005, 1e-9, 1234567.891]]
        d["line_bet"] = [{"v": v, **call(g.line_bet, v)} for v in (0.6, 1, 0, 7.5)]
        d["api_check_js"] = call(g.api_check_js)
        if hasattr(g, "win_tier"):
            d["win_tier"] = [{"p": p, "s": s, **call(g.win_tier, p, s)} for p, s in
                             [(0, 1), (0.5, 1), (1, 1), (2.5, 1), (6, 1), (10, 1), (15, 1), (25, 1), (40, 1),
                              (50, 1), (100, 1), (1, 0), (-1, 1), (0.6 * 10, 0.6)]]
        if hasattr(g, "family_of"):
            d["family_of"] = [{"v": v, **call(g.family_of, v)} for v in (5, "7", 8, 6, 18, 19, None, "x", 5.9)]
        if hasattr(g, "ways_total"):
            d["ways_total"] = call(g.ways_total)
        out["profiles"][name] = rec
    out["server_errors"] = {str(k): v for k, v in game_profile.SERVER_ERRORS.items()}
    out["unknown_profile"] = call(lambda: game_profile.get("nope").name)
    return out


# ── rounds + invariants ──────────────────────────────────────────────────────────────

def _inner(raw):
    return raw["result"] if isinstance(raw.get("result"), dict) and "timestamp" in raw else raw


def _fixture_rounds(game):
    """The game's captured rounds: bet replies (as the bridge hands them over) and lastBets."""
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "games", game, "fixtures", "bet-responses", "*.json"))):
        out.append(load(p))
    for p in sorted(glob.glob(os.path.join(ROOT, "games", game, "fixtures", "game-info", "*.json"))):
        info = _inner(load(p))
        if isinstance(info.get("lastBet"), dict):
            out.append(info["lastBet"])
    return out


def _mut(raw, **changes):
    """A copy of a round with fields changed (on the INNER round, so envelopes keep working)."""
    r = copy.deepcopy(raw)
    _inner(r).update(changes)
    return r


def _zeus_round(fs, coins, state=2, payout=0.0, balance=100.0, stake=0.0, acc=None, total=3):
    pos = [{"row": rc // 3, "col": rc % 3, "coinValue": 1.5} for rc in coins]
    r = {"gameState": state, "freeSpinInfo": ({"freespins": fs, "totalFreeSpin": total} if fs or state == 2 else None),
         "coinPositions": pos, "coinCount": len(pos), "stake": stake, "baseStake": 1.0, "payout": payout,
         "balance": balance, "result": {"views": [[13, 1, 2], [3, 4, 5], [6, 7, 8]]}}
    if acc is not None:
        r["accumulatedWinAmount"] = acc
    return r


def _streams():
    """{label: (profile, [(raw, phase), ...])} — captured sequences plus hand-built ones that break
    each rule, so a check is seen both passing and failing."""
    S = {}
    games = {"mystic": "mystic", "zeus": "zeus", "dragon-vip": "zeus", "chaos": "chaos",
             "joker": "joker", "grandma": "grandma"}
    for prof, game in games.items():
        rounds = _fixture_rounds(game)
        phased = [(r, "freespin" if not (_inner(r).get("stake") or 0) else "base") for r in rounds]
        S[f"{prof}/all"] = (prof, phased)
        for i, r in enumerate(rounds):
            for ph in (None, "base", "freespin"):
                S[f"{prof}/single{i}@{ph}"] = (prof, [(r, ph)])
    S["mystic/empty"] = ("mystic", [])

    # conservation: a chain, a broken link, and the Dragon VIP `balance: -1` respins.
    chain = [{"stake": 1.0, "payout": 0.0, "balance": 99.0}, {"stake": 1.0, "payout": 3.5, "balance": 101.5},
             {"stake": 0.0, "payout": 2.0, "balance": -1}, {"stake": 0.0, "payout": 2.0, "balance": 0},
             {"stake": 0.0, "payout": 1.25, "balance": 106.75}, {"stake": 0.6, "payout": 0.0, "balance": 106.0}]
    S["conservation/chain"] = ("mystic", [(r, "base") for r in chain])
    S["conservation/broken-after-carry"] = ("mystic", [(r, "base") for r in chain[:4]]
                                            + [({"stake": 0.0, "payout": 1.0, "balance": 106.75}, "freespin")])
    S["conservation/fs-stake"] = ("mystic", [({"stake": 1.0, "payout": 0, "balance": 9, "gameState": 2, "freeSpins": 3,
                                               "totalFreeSpin": 3}, "trigger"),
                                             ({"stake": 0.5, "payout": 0, "balance": 8.5, "gameState": 2, "freeSpins": 4,
                                               "totalFreeSpin": 3}, "freespin"),
                                             ({"stake": 0, "payout": 0, "balance": 8.5, "gameState": 2, "freeSpins": 2,
                                               "totalFreeSpin": 3}, "freespin"),
                                             ({"stake": 0, "payout": 0, "balance": 8.5, "gameState": 2, "freeSpins": 1,
                                               "totalFreeSpin": 3}, "freespin")])

    # Hold & Spin: a clean feature, and one that breaks every rule.
    good = [_zeus_round(3, [0, 4, 8], stake=1.0, balance=99), _zeus_round(2, [0, 4, 8], balance=99),
            _zeus_round(3, [0, 4, 8, 1], balance=99), _zeus_round(2, [0, 4, 8, 1], balance=99),
            _zeus_round(1, [0, 4, 8, 1], balance=99), _zeus_round(0, [0, 4, 8, 1], state=1, payout=6.0,
                                                                  balance=105, acc=6.0)]
    S["holdspin/clean"] = ("zeus", [(r, "trigger" if i == 0 else "freespin") for i, r in enumerate(good)])
    bad = [_zeus_round(2, [0, 4], stake=1.0, total=2), _zeus_round(1, [0, 4, 8], total=2),
           _zeus_round(3, [4, 8], total=2), _zeus_round(0, list(range(9)), total=2),
           _zeus_round(0, [4, 8, 2], total=2), _zeus_round(2, [4, 8, 2], state=1, payout=1.0, acc=3.0, total=2)]
    S["holdspin/broken"] = ("zeus", [(r, "freespin") for r in bad])
    S["holdspin/instant9"] = ("zeus", [(_zeus_round(0, list(range(9)), state=1, payout=13.5, stake=1.0), "base")])
    S["holdspin/none"] = ("zeus", [(_zeus_round(0, [1], state=1, stake=1.0), "base")])

    # Chaos: wrong payout, wrong winlose, off-ladder multiplier, wrong award, inflated ante base,
    # P1 / Wild on forbidden reels, an unjustified retrigger.
    ch = _fixture_rounds("chaos")
    by = {os.path.basename(p): load(p) for p in
          glob.glob(os.path.join(ROOT, "games", "chaos", "fixtures", "bet-responses", "*.json"))}
    win2 = by["base-win-two-lines.json"]
    trig = by["freespin-trigger.json"]
    ante = by["ante-bet-wild-win.json"]
    p1 = by["p1-scatter-win-maxwin-capped.json"]
    grid_bad = {"rawViews": None, "views": [{"id": 1, "value": [9, 2, 5]}, {"id": 2, "value": [5, 6, 8]},
                                             {"id": 3, "value": [3, 11, 5]}, {"id": 4, "value": [11, 2, 2]},
                                             {"id": 5, "value": [11, 1, 5]}]}
    S["chaos/broken"] = ("chaos", [
        (_mut(win2, payout=_inner(win2)["payout"] + 1, winlose=99), "base"),
        (_mut(win2, randomMultiplier=3), "base"),
        (_mut(trig, totalFreeSpin=_inner(trig)["totalFreeSpin"] + 1), "base"),
        (_mut(ante, baseStake=_inner(ante)["baseStake"] * 3, stake=_inner(ante)["stake"] * 3), "base"),
        (_mut(ante, stake=_inner(ante)["stake"] + 0.1), "base"),
        (_mut(win2, result=grid_bad, scatterMultiplier=40, paylines=[
            {"symbols": [9, 9, 9], "payline": 1, "payoutRate": 7, "winType": "J-J-J-A-K"},
            {"symbols": [9], "payline": 2, "payoutRate": 3, "winType": "WILD!-WILD!-WILD!"},
            {"symbols": [5], "payline": 3, "payoutRate": 1, "winType": "Z-Z-Z"}]), "base"),
        (_mut(p1, rawPayout=1.0, hasReachedMaxWin=False), "base"),
        (_mut(trig, freeSpins=40, stake=0), "freespin"),
        (_mut(win2, freeSpins=45, stake=0, anteMultiplier=3), "freespin"),
        (_mut(win2, freeSpins=44, stake=0, anteMultiplier=2), "freespin"),
    ])
    S["chaos/ante-only"] = ("chaos", [(ante, "base"), (_mut(ante, anteMultiplier=0), "base")])
    S["chaos/no-grid"] = ("chaos", [(_mut(win2, result={"views": []}), "base"),
                                    (_mut(trig, result=None), "freespin")])

    # Mystic cascades: a climb that stalls, a next round that does not reset, a next round with none.
    my = {os.path.basename(p): load(p) for p in
          glob.glob(os.path.join(ROOT, "games", "mystic", "fixtures", "bet-responses", "*.json"))}
    cw = my["base-cluster-win.json"]
    steps = copy.deepcopy(_inner(cw)["result"])
    if len(steps) >= 3:
        steps[2]["multiplierLevel"] = steps[1].get("multiplierLevel")
    stalled = _mut(cw, result=steps)
    shifted = copy.deepcopy(_inner(cw)["result"])
    for s in shifted:
        if "multiplierLevel" in s:
            s["multiplierLevel"] = (s["multiplierLevel"] or 0) + 1
    S["cascade/stall-noreset"] = ("mystic", [(stalled, "base"), (_mut(cw, result=shifted), "base")])
    S["cascade/next-has-none"] = ("mystic", [(cw, "base"), (_mut(cw, result=[{"payout": 0}]), "base")])
    S["cascade/clean-pair"] = ("mystic", [(cw, "base"), (my["freespin-round.json"], "freespin"),
                                          (cw, "base"), (cw, "base")])

    # Premium Reel (Dragon VIP): charged right, charged wrong, base moved, package changed mid-bonus.
    def prem(rid, mult, stake, base=1.0, fs_phase=False):
        return ({"stake": stake, "baseStake": base, "premiumReelId": rid, "premiumReelMultiplier": mult,
                 "payout": 0, "balance": 50, "gameState": 2 if fs_phase else 1,
                 "freeSpinInfo": {"freespins": 2, "totalFreeSpin": 3} if fs_phase else None},
                "freespin" if fs_phase else "base")
    S["premium/mixed"] = ("dragon-vip", [prem(0, 0, 1.0), prem(23, 3, 3.0), prem(23, 3, 3.5), prem(12, 0, 2.0),
                                         prem(23, 3, 6.0, base=2.0), prem(23, 3, 0, fs_phase=True),
                                         prem(12, 2, 0, fs_phase=True), prem(0, 0, 1.0),
                                         prem(0, 0, 0, fs_phase=True)])
    S["premium/none"] = ("dragon-vip", [prem(0, 0, 1.0), prem(0, 0, 1.0)])

    # Joker expansion: several symbols written, a symbol that never landed, a paid-round expansion.
    jk = {os.path.basename(p): load(p) for p in
          glob.glob(os.path.join(ROOT, "games", "joker", "fixtures", "bet-responses", "*.json"))}
    jw = jk["freespin-wild-expand-terminal.json"]
    rv = copy.deepcopy(_inner(jw)["result"])
    views = rv.get("views") or []
    if len(views) >= 2:
        views[0]["value"] = ["Q!", "K!", "A!"]
        views[1]["value"] = ["WILD!", "J", "WILD!"]
    S["joker/expansion-broken"] = ("joker", [(_mut(jw, result=rv), "freespin"),
                                             (_mut(jw, result=rv, stake=0.6), "base"),
                                             (_mut(jk["base-win-three-lines.json"], payout=9.9), "base"),
                                             (_mut(jw, luckyMultiplier=5, stake=0), "freespin")])

    # Grandma ways: a way that pays wrong, a round whose ways do not sum, a way missing a factor.
    gm = {os.path.basename(p): load(p) for p in
          glob.glob(os.path.join(ROOT, "games", "grandma", "fixtures", "bet-responses", "*.json"))}
    gw = gm["base-win-wild-x8.json"]
    wd = copy.deepcopy(_inner(gw).get("wayDetails") or [])
    if wd:
        wd[0]["payout"] = (wd[0].get("payout") or 0) + 1
        wd.append({"symbolId": 3, "wayCount": 1})
    S["grandma/broken"] = ("grandma", [(_mut(gw, wayDetails=wd), "base"),
                                       (_mut(gw, payout=(_inner(gw).get("payout") or 0) + 2), "base"),
                                       (_mut(gw, hasReachedMaxWin=True, payout=0), "base"),
                                       (_mut(gw, baseStake=0, stake=0), "base")])

    # Max-win: a remainder credited right, the full cap credited (wrong), a flag that lies.
    mw = {"rawPayout": 12.0, "payout": 2.0, "accumulatedWinAmount": 10.0, "maxWin": 10, "baseStake": 1,
          "stake": 1, "hasReachedMaxWin": True, "balance": 10}
    S["maxwin/mixed"] = ("chaos", [(mw, "freespin"), (dict(mw, payout=10.0, accumulatedWinAmount=18.0), "freespin"),
                                   (dict(mw, hasReachedMaxWin=False), "freespin"),
                                   (dict(mw, rawPayout=2.0, hasReachedMaxWin=False), "freespin"),
                                   (dict(mw, rawPayout=1.0, payout=1.0, accumulatedWinAmount=4.0), "freespin"),
                                   (dict(mw, rawPayout="x"), "freespin"), (dict(mw, maxWin=0), "freespin")])
    return S


def invariants():
    import scenario_engine as se
    out = {"streams": {}, "parse": {}}
    streams = _streams()
    profiles_for = {label: [prof] for label, (prof, _) in streams.items()}
    for label in [k for k in streams if k.endswith("/all")] + ["holdspin/clean", "chaos/broken", "grandma/broken"]:
        profiles_for[label] = PROFILES          # cross-profile: what a check does on the wrong game
    for label, (prof, items) in streams.items():
        for p in profiles_for[label]:
            se.set_profile(p)
            se.GAME.launch_caps = None
            rec = {"profile": p, "rounds": [{"raw": raw, "phase": ph} for raw, ph in items]}
            bets, parse = [], []
            for i, (raw, ph) in enumerate(items):
                try:
                    b = se.Bet.parse(raw, i)
                except Exception as e:  # noqa: BLE001
                    parse.append({"err": type(e).__name__})
                    continue
                b.phase = ph
                bets.append(b)
                parse.append({"ok": plain({**b.row(), "max_win": b.max_win, "terminal": se.is_terminal(b),
                                           "win_steps": se.win_steps(b), "enters_holdspin": call(se.enters_holdspin, b),
                                           "is_trigger": se.is_freespin_trigger(b)})})
            rec["parse"] = parse
            rec["results"] = {name: call(fn, bets) for name, fn in se.INVARIANTS.items()}
            out["streams"][f"{label}#{p}"] = rec
    # BetRejected wording.
    se.set_profile("mystic")
    try:
        se.Bet.parse({"status": 0, "code": -2002, "msg": "Insufficient balance", "timestamp": 1}, 3)
    except se.BetRejected as e:
        out["rejected_message"] = str(e)
    return out


# ── cases: tokens, windows, stream predicates, video/image specs, verdicts ───────────

# Fact values for the synthetic variant, picked per fact NAME (crc32) so every run is stable and
# every kind of value reaches every kind of predicate.
FACT_POOL = [True, False, 0, 1, 2, 3.5, 0.6, 20, "TURBO", "NORMAL", "none", "my", [1, 3, 5], [],
             ["premium_reels_active"], None, "0.6", "12", -1, 100, "922789333730066433", {"a": 1},
             "it's", "True", 1e-7]
MARK_KINDS = ["seq0", "seq1", "list", "moment", "seq9"]
SHOTS = [{"label": "base screen en", "file": "s1.png", "path": "/shots/s1.png"},
         {"label": "settings AFTER", "file": "s2.png", "path": "/shots/s2.png"},
         {"label": "my tutorial page 2", "file": "s3.png", "path": "/shots/s3.png"}]
PROFILE_GAME = {"dragon": "zeus", "dragon-vip": "zeus"}       # fixture rounds to borrow


def _walk_at(o, ats, eaches):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "at":
                (ats.extend(v) if isinstance(v, list) else ats.append(v))
            elif k == "each":
                eaches.append(v)
            else:
                _walk_at(v, ats, eaches)
    elif isinstance(o, list):
        for x in o:
            _walk_at(x, ats, eaches)


def _synthetic_facts(run_doc):
    import re
    import zlib
    text = json.dumps(run_doc, default=str)
    names = sorted(set(re.findall(r"\{facts\.([a-zA-Z_][a-zA-Z_0-9]*)\}", text)))
    return {n: copy.deepcopy(FACT_POOL[zlib.crc32(n.encode()) % len(FACT_POOL)]) for n in names}


def _synthetic_marks(run_doc):
    import zlib
    ats, eaches = [], []
    _walk_at(run_doc, ats, eaches)
    marks = {}
    for name in ats:
        if not isinstance(name, str) or name in ("", "all", "trigger", "terminal", "res") or name.startswith("rounds["):
            continue
        kind = MARK_KINDS[zlib.crc32(name.encode()) % len(MARK_KINDS)]
        marks[name] = {"seq0": 0, "seq1": 1, "list": [1, 0], "moment": {"t": 3.25}, "seq9": 9}[kind]
    for name in eaches:
        if isinstance(name, str):
            marks[name] = [0, 2, 1, 7] if zlib.crc32(name.encode()) % 2 else 1
    return marks


def _saved_results():
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "test-results", "**", "result.json"), recursive=True)):
        rel = os.path.relpath(p, os.path.join(ROOT, "test-results")).replace("\\", "/")
        game = rel.split("/")[0]
        d = load(p)
        out.setdefault((game, d.get("scenario")), d)
    return out


def cases_baseline():
    import yaml
    import run_scenario as rs
    import scenario_engine as se

    saved = _saved_results()
    exprs = {}
    orig_eval = se.eval_inline_stream

    def capturing(expr, result):
        src = se.resolve_tokens(expr, result, for_expr=True)
        if src not in exprs:
            try:
                v = eval(src, {"__builtins__": {}}, dict(se._PREDICATE_NAMES))   # noqa: S307
                exprs[src] = {"ok": repr(v), "truth": bool(v)}
            except Exception as e:  # noqa: BLE001
                exprs[src] = {"err": type(e).__name__, "msg": str(e)}
        return orig_eval(expr, result)

    se.eval_inline_stream = capturing
    out = {"runs": []}
    try:
        for suite in sorted(glob.glob(os.path.join(ROOT, "games", "*", "test-suites", "*.yml"))):
            game = os.path.basename(os.path.dirname(os.path.dirname(suite)))
            doc = yaml.safe_load(open(suite, encoding="utf-8"))
            if not isinstance(doc, dict):
                continue
            runs = [r.get("run", r) for r in (doc.get("runs") or []) if isinstance(r, dict)]
            se.set_profile(game)
            se.GAME.launch_caps = None
            raws = _fixture_rounds(PROFILE_GAME.get(game, game))
            phases = ["trigger" if i == 0 else ("freespin" if not (_inner(r).get("stake") or 0) else "base")
                      for i, r in enumerate(raws)]
            for ri, run_doc in enumerate(runs):
                if not isinstance(run_doc, dict) or not run_doc.get("cases"):
                    continue
                scen = run_doc.get("scenario")
                sv = saved.get((game, scen)) or next((v for (g, s), v in saved.items() if s == scen), None)
                variants = {"synthetic": (True, _synthetic_facts(run_doc), _synthetic_marks(run_doc), SHOTS),
                            "unreached": (False, _synthetic_facts(run_doc), {}, [])}
                if sv is not None:
                    variants["saved"] = (True, sv.get("facts") or {}, sv.get("marks") or {}, sv.get("shots") or [])
                for vname, (reached, facts, marks, shots) in variants.items():
                    bets = []
                    for i, raw in enumerate(raws):
                        b = se.Bet.parse(raw, i)
                        b.phase = phases[i]
                        bets.append(b)
                    result = se.ScenarioResult(
                        name=scen, reached=reached, res=(bets[0] if bets else None), rounds=bets,
                        timeline=[], events=[], video_window=(1.5, 88.25),
                        reason=None if reached else "synthetic: not reached",
                        round_windows=[(i * 10.0, i * 10.0 + 4.5) for i in range(len(bets))],
                        marks=copy.deepcopy(marks), facts=copy.deepcopy(facts), shots=copy.deepcopy(shots))
                    rec = {"suite": os.path.relpath(suite, ROOT).replace("\\", "/"), "run": ri, "variant": vname,
                           "game": game, "fixture_game": PROFILE_GAME.get(game, game), "phases": phases,
                           "reached": reached, "facts": facts, "marks": marks, "shots": shots,
                           "cases": call(rs.build_cases, run_doc, result)}
                    if "ok" in rec["cases"]:
                        rec["ai_calls"] = rs.ai_calls(rec["cases"]["ok"])
                    out["runs"].append(rec)
    finally:
        se.eval_inline_stream = orig_eval
    out["exprs"] = exprs
    return out


EXTRA_EXPRS = ["None >= 1", "1 < None", "'a' < 1", "None - 1", "'a' + 1", "1 + 'a'", "[1] + 1", "NORMAL != TURBO",
               "1 in 5", "'a' in None", "1 / 0", "1.0 / 0", "1 % 0", "1 // 0", "{foo.bar} >= 1", "1 ==",
               "abs(None)", "len(5)", "None * 2", "'a' * 'b'", "True + 1", "-None", "not None", "[] == []",
               "1 if True else 2", "(1, 2) == [1, 2]", "'x' in ['x']", "{facts} == 1", "max()", "round(None)",
               "2 ** 0.5", "None < None", "'5' > 3", "[1,2] < [1,3]", "1 == 1 == 1", "1 < 2 > 1", "float('x')",
               "int('3.5')", "sum([1, 'a'])", "{'a': 1}['b']", "[1][3]", "'abc'[1]", "x.y", "1.5 % 0.5",
               "-7 % 3", "7 % -3", "-7 // 2", "2 ** -1", "10 ** 20 + 1",
               "922789333730066433 == 922789333730066434", "set([1,1]) == {1}", "sorted([3,1])", "all([])",
               "1e309", "0.1+0.2", "'it''s'", "\"a\" 'b'", "(1)", "()", "[1,]", "1 is None",
               "None is not None", "'a' not in 'abc'", "~1", "[1, 3, 5] == [1, 3, 5]",
               "'my' in \"['my', 'vi']\"", "None == True or None == True", "abs(None - 7 * 75) < 0.005",
               "15 % 12 == 0", "1 >= 2 - 1", "None != None", "[] == ['premium_reels_active']",
               "0.30000000000000004 == 0.1 + 0.2", "7.0 == 7", "True == 1", "'7' == 7", "1 <", "== 1", "a b",
               "{1, 2} == {2, 1}", "{}", "{'a': 1} == {'a': 1}", "None in [None]", "1 in [1.0]", "[1] in [[1]]",
               "'' in 'a'", "5 > True", "'True' == True", "len('abc') == 3", "min(3, 1) == 1", "max([2, 9])",
               "round(2.5)", "round(2.675, 2)", "int(3.9)", "float('1.5')", "any([0, ''])", "sorted('cba')",
               "1 < 2 < 3 < 2", "not 1 == 2", "-2 ** 2", "2 ** 3 ** 2", "(1 + 2) * 3", "1 + 2 * 3",
               "'a' < 'b' <= 'b'", "[1, [2, 3]] == [1, [2, 3]]", "(1,) == (1,)", "1 and 0", "0 or 'x'",
               "None or None", "[1, 2][-1]", "'ab' * 2 == 'abab'", "3 * [0] == [0, 0, 0]", "0.5 == 1/2",
               "abs(-3) == 3", "abs(-2.5)", "round(0.125, 2)", "round(7)", "sum([0.1] * 10)", "sum([1, 2])",
               "max(1, 2.5)", "min([3, 'a'])", "'[1, 3, 5]' == [1, 3, 5]", "nan", "inf > 1", "1_000 == 1000",
               "'a' 'b' == 'ab'", "'x' in {'x': 1}", "1 in {1, 2}", "[] in []", "() == ()", "{1: 2}[1]",
               "'\\n' == '\\n'", "1 if 0 else 2 if 0 else 3", "not not 1", "1 is not 2",
               "[1, 2, 3][5]", "(1, 2)[1]", "'abc'['x']", "None[0]", "len([1, 2, 3])", "len({})",
               "int('12') + 1", "float(3)", "int(True)", "sorted([True, 0, 2])", "all([1, 'a'])",
               "set([3, 1, 3]) == {1, 3}", "2 >= 2.0", "-0.0 == 0", "10 % 3.0", "-10 // 3.0", "5 // 0.0",
               "2.0 ** 0.5 > 1.41", "1e-7 < 0.005", "True is True", "(1, 2) < (1, 3)", "[1] < (1,)",
               "{1} < {1, 2}", "{1, 2} <= {1, 2}", "'abc' > 'abd'", "abs(True)", "max('ab', 'b')"]


def exprs_baseline(captured):
    import scenario_engine as se
    out = dict(captured)
    for src in EXTRA_EXPRS:
        if src in out:
            continue
        try:
            v = eval(src, {"__builtins__": {}}, dict(se._PREDICATE_NAMES))   # noqa: S307
            out[src] = {"ok": repr(v), "truth": bool(v)}
        except Exception as e:  # noqa: BLE001
            out[src] = {"err": type(e).__name__, "msg": str(e)}
    return out


def main():
    import warnings
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    write("py_compat.json", py_compat())
    write("profiles.json", profiles())
    write("invariants.json", invariants())
    cases = cases_baseline()
    write("exprs.json", exprs_baseline(cases.pop("exprs")))
    write("cases.json", cases)


if __name__ == "__main__":
    main()
