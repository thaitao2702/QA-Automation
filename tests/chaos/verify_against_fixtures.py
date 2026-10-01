"""Drive the Chaos invariants against the REAL captured payloads, then against mutated copies.

Passing on real data is only half a test: an invariant that accepts everything also accepts the
real thing. So each rule is run twice — once on the untouched payload (must PASS) and once on a
copy with exactly one field corrupted (must CATCH). Fixtures: games/chaos/fixtures/bet-responses/.
"""
import copy, glob, io, json, os, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import scenario_engine as se  # noqa: E402

GAME = se.set_profile("chaos")
FIX = os.path.join(REPO, "games", "chaos", "fixtures", "bet-responses")
PASS, FAIL = [], []


def load(name):
    return json.load(io.open(os.path.join(FIX, name + ".json"), encoding="utf-8"))


def bet(env, seq=0, phase="base"):
    b = se.Bet.parse(env, seq)          # Bet.parse unwraps the envelope itself
    b.phase = phase
    return b


def check(label, inv, rounds, want_valid):
    r = se.INVARIANTS[inv](rounds)
    ok = bool(r["valid"]) == want_valid
    (PASS if ok else FAIL).append(label)
    print("  [%s] %-58s %-5s %s" % ("ok  " if ok else "FAIL", label,
                                    "pass" if r["valid"] else "catch",
                                    ("| " + "; ".join(r["violations"])[:78]) if r["violations"] else ""))


NAMES = [os.path.basename(p)[:-5] for p in sorted(glob.glob(os.path.join(FIX, "*.json")))]
print("fixtures: %s\n" % ", ".join(NAMES))

print("=== A. every real payload passes the rules that apply to it ===")
for n in NAMES:
    env = load(n)
    b = bet(env, phase="base")
    check("%s :: payout_matches_paylines" % n, "payout_matches_paylines", [b], True)
    check("%s :: winlose_consistent" % n, "winlose_consistent", [b], True)
    check("%s :: multiplier_in_ladder" % n, "multiplier_in_ladder", [b], True)
    # A round with no Bonus has no award to check, and the invariant says so rather than
    # passing vacuously — so only the trigger fixture is expected to validate here.
    check("%s :: freespin_award" % n, "freespin_award", [b], n == "freespin-trigger")

print("\n=== B. the free-spin award, on the round that really awarded 48 ===")
env = load("freespin-trigger")
raw = GAME.unwrap(env)
print("     reels=%s ways=%s base=%s -> expected %s | server awarded %s"
      % (GAME.scatter_reels(raw), GAME.scatter_ways(raw),
         GAME.FS_BASE_BY_REELS[len(GAME.scatter_reels(raw))],
         GAME.expected_fs_award(raw), raw["totalFreeSpin"]))
check("real 48-spin trigger accepted", "freespin_award", [bet(env)], True)
for wrong, why in ((20, "the 'by total symbol count' reading"),
                   (60, "the design doc's base-15 reading"),
                   (12, "base without the ways multiplier"),
                   (47, "off by one")):
    e = copy.deepcopy(env)
    e["result"]["totalFreeSpin"] = wrong
    check("award %-3d caught (%s)" % (wrong, why), "freespin_award", [bet(e)], False)

print("\n=== C. the payout rule catches each way it can break ===")
env = load("base-win-multiplier-x2")            # x2 multiplier, one line at rate 10 -> 0.6
check("real x2 round accepted", "payout_matches_paylines", [bet(env)], True)
muts = [
    ("random multiplier ignored (paid x1)", lambda e: e["result"].update(payout=0.3)),
    ("multiplier applied twice (x4)", lambda e: e["result"].update(payout=1.2)),
    ("divided by 5 instead of betLines 20", lambda e: e["result"].update(payout=2.4)),
    ("payoutRate silently raised", lambda e: e["result"]["paylines"][0].update(payoutRate=99)),
    ("betLines wrong (10 not 20)", lambda e: e["result"].update(betLines=10)),
]
for why, mutate in muts:
    e = copy.deepcopy(env)
    mutate(e)
    check("caught: %s" % why, "payout_matches_paylines", [bet(e)], False)

print("\n=== D. the scatter pseudo-line must not be counted as a win ===")
env = load("freespin-trigger")                  # payline -1, payoutRate 1, but the round paid 0
raw = GAME.unwrap(env)
print("     paylines=%s -> win_lines=%s (the -1 entry is excluded)"
      % (len(raw["paylines"]), len(GAME.win_lines(raw))))
check("trigger round paying 0 accepted", "payout_matches_paylines", [bet(env)], True)

print("\n=== E. every symbol id is now known; the guard still holds for a gridless round ===")
b = bet(load("base-win-two-lines"))
print("     SYM_P1=%s SYM_WILD=%s SYM_SCATTER=%s" % (GAME.SYM_P1, GAME.SYM_WILD, GAME.SYM_SCATTER))
check("p1_reels_only runs for real now", "p1_reels_only", [b], True)

# The guard that mattered while P1 was unknown must still work when the GRID is missing.
blind = se.Bet.parse({"status": 1, "timestamp": 1,
                      "result": {"gameState": 2, "stake": 1, "payout": 0, "balance": 99,
                                 "totalFreeSpin": 12, "freeSpins": 12}}, 0)
blind.phase = "base"
for inv in ("p1_reels_only", "freespin_award"):
    r = se.INVARIANTS[inv]([blind])
    ok = not r["valid"] and "cannot verify" in " ".join(r["violations"])
    (PASS if ok else FAIL).append("%s cannot-verify on a gridless round" % inv)
    print("  [%s] %-58s %s" % ("ok  " if ok else "FAIL", inv + " (gridless)",
                               "; ".join(r["violations"])[:64]))
print("\n=== F. ANTE: multiplies the stake, never the payout base ===")
rows = [bet(load(n), seq=i) for i, n in enumerate(NAMES)]
for n in NAMES:
    raw = GAME.unwrap(load(n))
    print("     %-24s stake=%.2f base=%.2f anteMultiplier=%s -> is_ante=%s"
          % (n, raw["stake"], raw["baseStake"], raw["anteMultiplier"], GAME.is_ante_round(raw)))
# The whole fixture set: one real ante round (stake 1.8 = base 0.6 x 3) alongside plain rounds at
# the same base. That is exactly the pairing the invariant needs to prove the base did NOT move.
check("real ante round + plain rounds accepted", "ante_stake_multiplier", rows, True)

ante = load("ante-bet-wild-win")
raw = GAME.unwrap(ante)
print("     ante round paid %.2f | from baseStake %.2f -> %.2f | from stake %.2f -> %.2f"
      % (raw["payout"], raw["baseStake"],
         raw["randomMultiplier"] * 150 * raw["baseStake"] / raw["betLines"],
         raw["stake"], raw["randomMultiplier"] * 150 * raw["stake"] / raw["betLines"]))
check("ante round's payout derives from baseStake", "payout_matches_paylines", [bet(ante)], True)

# The defect that matters: ante inflating the payout base as well as the stake.
inflated = copy.deepcopy(ante)
inflated["result"]["baseStake"] = 1.8
inflated["result"]["payout"] = 13.5
check("caught: ante inflated the payout base too", "ante_stake_multiplier",
      [bet(load("base-win-two-lines"), seq=0), bet(inflated, seq=1)], False)

halved = copy.deepcopy(ante)
halved["result"]["stake"] = 0.9                  # the design doc's wrong 1.5x
check("caught: 1.5x deducted when the server said x3", "ante_stake_multiplier", [bet(halved)], False)

print("\n=== G. Wild is now known (id 9) -> the Raining Wild precondition is checkable ===")
w = GAME.unwrap(load("ante-bet-wild-win"))
print("     wilds=%s on reels %s -> Raining Wild expected: %s"
      % (GAME.wilds(w), GAME.reels_with(w, GAME.SYM_WILD), GAME.raining_wild_expected(w)))
ok = GAME.wilds(w) == 3 and GAME.raining_wild_expected(w) is True
PASS.append("wild census") if ok else FAIL.append("wild census")
print("  [%s] Wild census reads 3 wilds and demands the animation" % ("ok  " if ok else "FAIL"))

print("\n=== H. rates are checked against the BO paytable, not just self-consistency ===")
for n in NAMES:
    env = load(n)
    lines = GAME.win_lines(GAME.unwrap(env))
    if not lines:
        continue
    for p in lines:
        print("     %-24s %-22s reported %-5s | BO table %s"
              % (n, p["winType"], p["payoutRate"], GAME.expected_line_rate(p)))
    check("%s :: payline_rate_matches_paytable" % n, "payline_rate_matches_paytable",
          [bet(env)], True)

# The defect this catches that payout_matches_paylines cannot: a round that pays exactly what its
# lines claim, where the CLAIM itself contradicts the configured paytable.
env = copy.deepcopy(load("base-win-multiplier-x2"))          # J x4 -> 10
env["result"]["paylines"][0]["payoutRate"] = 30              # 30 is J x5, not x4
env["result"]["payout"] = 2 * 30 * 0.6 / 20                  # self-consistent, still wrong
check("caught: rate 30 for a 4-of-a-kind J (30 is the x5 rate)",
      "payline_rate_matches_paytable", [bet(env)], False)
check("...and payout_matches_paylines is happy with it, which is why both exist",
      "payout_matches_paylines", [bet(env)], True)

print("\n=== I. reel restrictions read off the BO reel strips ===")
print("     P1 allowed on %s (weight 0 elsewhere) | Wild forbidden on reel %s"
      % (list(GAME.P1_REELS_FROM_CONFIG), list(GAME.WILD_FORBIDDEN_REELS)))
for n in NAMES:
    raw = GAME.unwrap(load(n))
    print("     %-24s wild reels %s" % (n, GAME.wild_reels(raw)))
    check("%s :: symbol_reel_restrictions" % n, "symbol_reel_restrictions", [bet(load(n))], True)

env = copy.deepcopy(load("ante-bet-wild-win"))
env["result"]["result"]["views"][0]["value"][0] = 9          # a Wild on reel 1
check("caught: Wild on reel 1 (strip weight is 0 there)",
      "symbol_reel_restrictions", [bet(env)], False)

print("\n=== J. P1 (id 5) — scatter paid by ways, and the max-win cap ===")
env = load("p1-scatter-win-maxwin-capped")
raw = GAME.unwrap(env)
print("     P1 per reel %s -> reels %s, %s way(s) x 10 = scatterMultiplier %s"
      % (GAME.reel_counts(raw, GAME.SYM_P1), GAME.reels_with(raw, GAME.SYM_P1),
         GAME.p1_ways(raw), GAME.scatter_multiplier(raw)))
print("     lineWin %.2f + scatterWin %.2f = raw %.2f | cap %s -> credited %.2f (actual %.2f)"
      % (GAME.expected_line_win(raw), GAME.expected_scatter_win(raw),
         GAME.expected_raw_payout(raw), GAME.max_win_cap(raw),
         GAME.expected_payout(raw), raw["payout"]))
for inv in ("scatter_pay_matches_p1_ways", "max_win_cap_applied",
            "payout_matches_paylines", "winlose_consistent", "symbol_reel_restrictions"):
    check("p1 round :: %s" % inv, inv, [bet(env)], True)

# ways miscounted -> the scatter multiplier no longer matches the grid
e = copy.deepcopy(env); e["result"]["scatterMultiplier"] = 30
check("caught: scatterMultiplier 30 for a 2-way P1 grid", "scatter_pay_matches_p1_ways", [bet(e)], False)

# the cap ignored -> the full 12 credited
e = copy.deepcopy(env); e["result"]["payout"] = 12; e["result"]["winlose"] = 11.4
check("caught: paid 12 over a maxWin of 10", "max_win_cap_applied", [bet(e)], False)

# truncated but not flagged
e = copy.deepcopy(env); e["result"]["hasReachedMaxWin"] = False
check("caught: truncated without hasReachedMaxWin", "max_win_cap_applied", [bet(e)], False)

# flagged without a truncation
e = copy.deepcopy(load("base-win-two-lines")); e["result"]["hasReachedMaxWin"] = True
check("caught: hasReachedMaxWin set on an uncapped round", "max_win_cap_applied", [bet(e)], False)

# P1 on an illegal reel -- now checkable, since id 5 is known
e = copy.deepcopy(env); e["result"]["result"]["views"][1]["value"][0] = 5
check("caught: P1 on reel 2 (strip weight 0 there)", "symbol_reel_restrictions", [bet(e)], False)

print("\n" + "=" * 84)
print("PASS %d   FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
