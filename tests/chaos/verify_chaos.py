"""Drive every Chaos invariant with synthetic response streams, in BOTH directions.

Session-summary-3 §6: "An invariant that never fires is worthless. Each should catch its own
defect AND NOTHING ELSE." So each rule gets a GOOD stream (must pass) and a BAD one (must catch).

This harness builds payloads in the game's REAL contract — numeric symbol ids, `randomMultiplier`,
the doubly-nested `result.result.views` — as established in games/chaos/bet-response.md. For the
shape of that contract checked against actual captured payloads, see verify_against_fixtures.py;
this file covers the cases no capture exists for (retriggers, ante rounds, P1 wins).
"""
import os, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import scenario_engine as se  # noqa: E402

G = se.set_profile("chaos")
PASS, FAIL = [], []

SCATTER, A, J, K, P2 = 11, 1, 2, 4, 6
P1_ID, WILD_ID = 5, 9       # both CONFIRMED from captured payloads


def grid(p1=(), scatter=(), wild=(), filler=A):
    """5 reels x 3 rows. p1/scatter/wild are 1-based reel numbers; repeat one for two on that reel."""
    reels = []
    for r in range(1, 6):
        col = ([P1_ID] * sum(1 for x in p1 if x == r)
               + [SCATTER] * sum(1 for x in scatter if x == r)
               + [WILD_ID] * sum(1 for x in wild if x == r))
        col += [filler] * (3 - len(col))
        reels.append({"value": col[:3]})
    return {"rawViews": None, "views": reels}


def bet(seq, phase="base", stake=1.0, base=None, payout=0.0, balance=100.0, fs=0, total_fs=0,
        state=1, mult=1, ante=0, lines=None, bet_lines=20, scatter_mult=0, **g):
    """One round in the real envelope shape; Bet.parse unwraps it."""
    base = stake if base is None else base
    inner = {"result": grid(**g), "gameState": state, "stake": stake, "baseStake": base,
             "payout": payout, "rawPayout": payout, "balance": balance, "freeSpins": fs,
             "totalFreeSpin": total_fs, "totalFreeSpinPayout": 0, "randomMultiplier": mult,
             "scatterMultiplier": scatter_mult, "anteMultiplier": ante, "paylines": lines or [],
             "betLines": bet_lines, "winlose": round(payout - stake, 10), "jackpot": 0,
             "accumulatedWinAmount": payout, "hasReachedMaxWin": False, "maxWin": 600,
             "roundId": "r%d" % seq, "promoResult": None}
    b = se.Bet.parse({"status": 1, "code": 0, "msg": "", "timestamp": 1, "result": inner}, seq)
    b.phase = phase
    return b


def line(rate, symbols=(J, J, J, A, K), idx=1, win_type="J-J-J-A-K"):
    return {"symbols": list(symbols), "payline": idx, "payoutRate": rate, "winType": win_type}


def check(label, inv, rounds, want_valid):
    r = se.INVARIANTS[inv](rounds)
    ok = bool(r["valid"]) == want_valid
    (PASS if ok else FAIL).append(label)
    print("  [%s] %-56s expect %-5s got %-5s %s"
          % ("ok  " if ok else "FAIL", label, "pass" if want_valid else "catch",
             "pass" if r["valid"] else "catch",
             ("| " + "; ".join(r["violations"])[:70]) if r["violations"] else ""))


print("\n=== A. freespin_award — base(REELS) x ways, the rule the real payload settled ===")
# Bonus on 3 reels with counts 1,2,2 -> ways 4, base 12 -> 48 (this is the captured case).
check("3 reels, ways 4 -> 48", "freespin_award",
      [bet(0, total_fs=48, state=2, scatter=(1, 2, 2, 4, 4))], True)
check("3 reels, ways 1 -> 12", "freespin_award",
      [bet(0, total_fs=12, state=2, scatter=(1, 2, 4))], True)
check("4 reels, ways 1 -> 15", "freespin_award",
      [bet(0, total_fs=15, state=2, scatter=(1, 2, 3, 4))], True)
check("5 reels, ways 1 -> 20", "freespin_award",
      [bet(0, total_fs=20, state=2, scatter=(1, 2, 3, 4, 5))], True)
check("5 reels, ways 2 -> 40", "freespin_award",
      [bet(0, total_fs=40, state=2, scatter=(1, 1, 2, 3, 4, 5))], True)
check("caught: ways multiplier dropped (12 not 48)", "freespin_award",
      [bet(0, total_fs=12, state=2, scatter=(1, 2, 2, 4, 4))], False)
check("caught: doc's base-15 reading (60)", "freespin_award",
      [bet(0, total_fs=60, state=2, scatter=(1, 2, 2, 4, 4))], False)
check("caught: by-symbol-count reading (20)", "freespin_award",
      [bet(0, total_fs=20, state=2, scatter=(1, 2, 2, 4, 4))], False)
# 3 Bonuses but only 2 REELS -> must NOT trigger, and must not award.
check("2 reels only -> no trigger, no award", "freespin_award",
      [bet(0, total_fs=0, scatter=(1, 1, 3))], False)
check("caught: awarded despite reaching only 2 reels", "freespin_award",
      [bet(0, total_fs=12, state=2, scatter=(1, 1, 3))], False)

print("\n=== B. payout_matches_paylines — mult x rate x base / betLines ===")
check("x1, one line rate 10, base 0.6 -> 0.30", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=0.30, mult=1, lines=[line(10)])], True)
check("x2 doubles it -> 0.60", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=0.60, mult=2, lines=[line(10)])], True)
check("two lines sum their rates", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=0.60, mult=1, lines=[line(10), line(10, idx=10)])], True)
check("caught: multiplier ignored", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=0.30, mult=2, lines=[line(10)])], False)
check("caught: /5 instead of /20", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=1.20, mult=1, lines=[line(10)])], False)
check("caught: paid on a line it never reported", "payout_matches_paylines",
      [bet(0, stake=0.6, payout=0.30, mult=1, lines=[])], False)

print("\n=== C. winlose_consistent ===")
check("winlose = payout - stake", "winlose_consistent",
      [bet(0, stake=0.6, payout=0.24)], True)
b = bet(0, stake=0.6, payout=0.24)
b.raw["winlose"] = 0.5
check("caught: winlose does not reconcile", "winlose_consistent", [b], False)

print("\n=== D. multiplier_in_ladder (field is randomMultiplier) ===")
check("base x2 + free spin x10", "multiplier_in_ladder",
      [bet(0, mult=2), bet(1, "freespin", stake=0, mult=10)], True)
check("caught: x3 is not on the ladder", "multiplier_in_ladder", [bet(0, mult=3)], False)

print("\n=== E. P1 rules — id 5 is CONFIRMED, so these run for real ===")
print("     SYM_P1 = %s (only on reels 1/3/5, and ways x 10 == scatterMultiplier)" % G.SYM_P1)
check("P1 on 1,3,5 accepted", "p1_reels_only", [bet(0, p1=(1, 3, 5))], True)
check("caught: P1 leaked onto reel 2", "p1_reels_only", [bet(0, p1=(1, 2, 3))], False)
check("caught: P1 leaked onto reel 4", "p1_reels_only", [bet(0, p1=(4,))], False)
check("1 way, base 1 -> pays 10", "p1_ways_payout",
      [bet(0, p1=(1, 3, 5), stake=1.0, payout=10.0)], True)
check("2 ways -> pays 20", "p1_ways_payout",
      [bet(0, p1=(1, 1, 3, 5), stake=1.0, payout=20.0)], True)
check("caught: 2 ways underpaid as 1", "p1_ways_payout",
      [bet(0, p1=(1, 1, 3, 5), stake=1.0, payout=10.0)], False)
check("scatter pay = ways x 10", "scatter_pay_matches_p1_ways",
      [bet(0, p1=(1, 1, 3, 5), scatter_mult=20)], True)
check("caught: scatter pay does not match the grid", "scatter_pay_matches_p1_ways",
      [bet(0, p1=(1, 1, 3, 5), scatter_mult=30)], False)
check("caught: Wild on reel 1", "symbol_reel_restrictions", [bet(0, wild=(1,))], False)
check("Wild on reels 2-5 accepted", "symbol_reel_restrictions", [bet(0, wild=(2, 4, 5))], True)

print("\n=== F. ante — no captured payload shows it engaged ===")
check("reports 'no ante round' on plain rounds", "ante_stake_multiplier",
      [bet(0, stake=0.6, base=0.6, ante=0)], False)
check("ante x3: base 0.6, deducted 1.8", "ante_stake_multiplier",
      [bet(0, stake=1.8, base=0.6, ante=3)], True)
check("caught: doc's 1.5x when the server said 3", "ante_stake_multiplier",
      [bet(0, stake=0.9, base=0.6, ante=3)], False)
check("caught: ante inflated the PAYOUT BASE too", "ante_stake_multiplier",
      [bet(0, stake=0.6, base=0.6, ante=0), bet(1, stake=5.4, base=1.8, ante=3)], False)
check("caught: stake > base but no multiplier reported", "ante_stake_multiplier",
      [bet(0, stake=1.8, base=0.6, ante=0)], False)

print("\n=== G. shared invariants on a chaos stream ===")
check("conservation", "conservation",
      [bet(0, stake=1, payout=0, balance=99), bet(1, stake=1, payout=5, balance=103)], True)
check("caught: bad credit", "conservation",
      [bet(0, stake=1, payout=0, balance=99), bet(1, stake=1, payout=5, balance=999)], False)
check("stake_zero_in_freespins", "stake_zero_in_freespins",
      [bet(0, state=2, total_fs=12), bet(1, "freespin", stake=0, fs=11, state=2)], True)
check("caught: a charged free spin", "stake_zero_in_freespins",
      [bet(0, state=2, total_fs=12), bet(1, "freespin", stake=1.0, fs=11, state=2)], False)

print("\n=== H. freespin_retrigger_adds ===")
check("normal count-down 11 -> 10", "freespin_retrigger_adds",
      [bet(0, "freespin", stake=0, fs=11), bet(1, "freespin", stake=0, fs=10)], True)
check("rise on a real retrigger (Bonus on 3 reels)", "freespin_retrigger_adds",
      [bet(0, "freespin", stake=0, fs=5),
       bet(1, "freespin", stake=0, fs=20, scatter=(1, 2, 4))], True)
check("caught: counter rose with no Bonus at all", "freespin_retrigger_adds",
      [bet(0, "freespin", stake=0, fs=5), bet(1, "freespin", stake=0, fs=20)], False)

print("\n=== I. the unreadable-grid backstop: cannot verify != pass ===")
blind = se.Bet.parse({"status": 1, "timestamp": 1,
                      "result": {"gameState": 1, "stake": 1, "payout": 0, "balance": 99,
                                 "totalFreeSpin": 12, "freeSpins": 0}}, 0)
blind.phase = "base"
for inv in ("freespin_award", "p1_reels_only"):
    r = se.INVARIANTS[inv]([blind])
    ok = not r["valid"] and "cannot verify" in " ".join(r["violations"])
    (PASS if ok else FAIL).append("%s cannot-verify" % inv)
    print("  [%s] %-56s %s" % ("ok  " if ok else "FAIL", inv + " on a gridless round",
                               "; ".join(r["violations"])[:64]))

print("\n=== J. the double-nested envelope survives Bet.parse ===")
b = bet(0, stake=0.6, payout=0.24, mult=2)
ok = b.payout == 0.24 and b.stake == 0.6 and b.cascades == 1 and G.grid(b.raw) is not None
(PASS if ok else FAIL).append("envelope + nested grid parsed")
print("  [%s] payout=%s stake=%s steps=%s grid=%s"
      % ("ok  " if ok else "FAIL", b.payout, b.stake, b.cascades,
         "5x3" if G.grid(b.raw) else "NONE"))

print("\n" + "=" * 82)
print("PASS %d   FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
