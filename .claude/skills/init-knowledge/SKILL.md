# Skill: /init-knowledge

```yaml
name: init-knowledge
version: "2.0"
domain: [game-qa, documentation-onboarding, knowledge-generation]
description: |
  Onboards a new game into the QA platform by performing multimodal
  document ingestion (PDF, Excel, images, Markdown), detection of EVERY
  documented mechanic (shared mechanisms + custom/game-specific ones)
  with four-tier evidence classification (CONFIRMED/LIKELY/ABSENT/UNKNOWN),
  comparison of each shared mechanism against its canonical pattern
  (matches/deviates), QA confirmation gate, and structured knowledge
  artifact generation into docs/final/ (game-knowledge.md + a separate
  paytable.md when documented), plus .docs-hash.json and assets.yml.

  Use when the user says "onboard a game," "init knowledge," "set up
  a new game," "read the docs for [game]," "start a new game,"
  "create knowledge for [game]," or "process game docs" -- even if
  they don't say "init-knowledge" explicitly.

  Do NOT use for updating existing knowledge (use /update-knowledge).
  Do NOT use for writing tests (use /write-test).
  Do NOT use for running tests (use /run-test).
```

**Usage:** `/init-knowledge --game=<game-name>`

**Flags:**
- `--game` (required) — the game folder name under `games/` (e.g. `zeus`). It is used
  verbatim as `{game}` and resolves every `games/{game}/...` path in this skill (raw
  docs, `docs/final/`, `.docs-hash.json`, `docs/assets/assets.yml`). See Step 0 for how
  it is resolved when omitted.

---

## Expert Vocabulary Payload

**Document Processing & Extraction:**
multimodal document ingestion, paytable parsing, symbol extraction, grid topology detection (rows x cols), payline layout mapping, win-type threshold detection

**Game Mechanics & Classification:**
mechanic detection (shared + custom/game-specific), shared mechanism fingerprinting (hold-and-spin, free spin, buy bonus, auto spin, …), custom mechanics (gamble/double-up, cascading/tumbling reels, multiplier wilds, expanding/sticky symbols, ways-to-win/Megaways, progressive jackpots, mystery symbols, pick bonuses), four-tier evidence classification (CONFIRMED/LIKELY/ABSENT/UNKNOWN), mechanism-vs-shared-pattern comparison (matches/deviates)

**Knowledge Artifacts:**
docs/final/game-knowledge.md (structured knowledge file), docs/final/paytable.md (separate payout reference, linked from game-knowledge), .docs-hash.json (SHA-256 source-corpus manifest, excludes docs/final/), assets.yml (asset manifest: symbols + UI, ref_asset keys, images under docs/assets/symbols/), source-of-truth hierarchy

**QA Pipeline:**
QA confirmation gate, mechanism detection report, evidence citation (exact quote + file + location), correction acknowledgment loop

---

## Anti-Pattern Watchlist

### 1. Hallucinated Game Data
**Detection:** Symbol names, IDs, or payout values appear in output that cannot be traced to an exact quote or table in the raw docs.
**Why it's dangerous:** game-knowledge.md feeds directly into test generation. A fabricated symbol ID produces tests that pass against wrong data.
**Resolution:** For every data point in game-knowledge.md, verify it exists verbatim in the source docs. IF ambiguous: write `"See {filename}"` instead of guessing.

### 2. Skipping the QA Gate
**Detection:** game-knowledge.md, paytable.md, assets.yml, or .docs-hash.json is written before QA has responded to the mechanism detection report.
**Why it's dangerous:** QA may know about mechanics not in the docs, or may correct a misclassification. Writing first forces a redo.
**Resolution:** Present findings. Wait for explicit QA response. Only then proceed to file generation.

### 3. Mechanism Overclaim
**Detection:** A mechanism is classified as CONFIRMED but the cited evidence is indirect (e.g., classifying "buy bonus" as CONFIRMED because the doc mentions "bonus" without "buy" or "purchase").
**Why it's dangerous:** Generates test cases for a mechanic the game doesn't have, wasting QA cycles.
**Resolution:** Use LIKELY for indirect evidence. Reserve CONFIRMED for direct, unambiguous textual evidence. Always include the exact quote.

### 4. Ambiguity Guessing
**Detection:** A field in game-knowledge.md contains a specific value (e.g., a single payout figure) when the source docs state it ambiguously or as a range.
**Resolution:** Write the ambiguity as-is: `"5x payout: 200-500 (see paytable.pdf p.3)"`. Never collapse a range into a single number.

### 5. Blank Section Suppression
**Detection:** A required section of game-knowledge.md is omitted entirely or left blank because no information was found.
**Why it's dangerous:** Downstream consumers (write-test, QA reviewers) can't tell if the section was intentionally empty or accidentally skipped.
**Resolution:** Write `"Not documented in current source files."` for every section where data is absent.

### 6. Silent File Skipping
**Detection:** A file in `docs/raw/` is not mentioned in the Source Documentation Index because the agent couldn't process it (e.g., a corrupted PDF or unsupported format).
**Resolution:** Include every file in the index. IF unreadable: add it with Key Information = `"Could not process — {reason}. Manual review required."`.

### 7. Uncompared Mechanism
**Detection:** A CONFIRMED/LIKELY mechanism is documented without a Matches/Deviates verdict against its `shared-mechanisms/{file}.md`, or a "Matches" verdict is recorded without the shared pattern file actually having been read.
**Why it's dangerous:** Tests are generated from the shared pattern. An undocumented deviation (e.g. "this game's free spins don't retrigger") produces tests that assert the canonical behaviour and fail — or worse, pass against wrong expectations.
**Resolution:** For every detected mechanism, read the matching shared pattern file and record either "Matches" or each concrete deviation with an exact quote from the game docs. Never assume Matches.

---

## Behavioral Instructions

### Step 0 — Resolve the game

0a. Bind `{game}` from the `--game` argument and use it verbatim as the game folder name
    throughout (every `games/{game}/...` path below).
0b. **If `--game` is absent:** look at the directories under `games/`. If **exactly one**
    contains a `docs/raw/` folder, use that game and state which one you picked. Otherwise
    (zero or multiple candidates), list the candidate game directories and **ask the user
    which game** — do **not** guess.
0c. **Validate before reading:** confirm `games/{game}/docs/raw/` exists and is non-empty.
    If it is missing or empty, tell the user to place the raw game docs at
    `games/{game}/docs/raw/` and **stop** — do not write any output files.

### Step 1 — Locate and Read Raw Documentation

1. Using `{game}` resolved in Step 0, read every file in `games/{game}/docs/raw/`. Files may be PDF, Excel (.xlsx), images (.png/.jpg), or Markdown.
2. For each file, extract:
   - Filename and type
   - Mechanic mentions: coins, bonus, free spins, buy bonus, auto spin, jackpot, scatter, wild
   - Symbol names and IDs (numbers or names identifying reel symbols)
   - Payline count and grid size (only if documented)
   - Win-type thresholds (e.g., big win = 10x bet)
3. Read **every** file in `shared-mechanisms/` (glob the directory — the set of shared
   mechanisms can grow, so never assume a fixed list). Each file is the canonical pattern for
   one mechanism, used for the comparison in Step 2. (At the time of writing the folder holds
   `hold-and-spin.md`, `free-spin.md`, `buy-bonus.md`, `auto-spin.md` — but read whatever is
   actually present.)
4. IF any file cannot be processed: log it for the Source Documentation Index (see anti-pattern #6).

### Step 2 — Detect Shared Mechanisms

5. **Detect EVERY game mechanic the raw docs describe — not only the common ones.** Sort each
   detected mechanic into one of two buckets:
   - **Shared mechanism** — it has a matching `shared-mechanisms/{name}.md` file (read in Step 1).
   - **Custom / game-specific mechanic** — documented but with **no** shared-mechanisms file
     (e.g. gamble / double-up, tumbling / cascading reels, multiplier wilds, expanding or sticky
     symbols, ways-to-win / Megaways, progressive jackpots, mystery symbols, pick bonuses, symbol
     upgrades, win multipliers, wheel bonuses). These are real mechanics — detect and document
     them too; do **not** drop a mechanic just because there is no shared file for it.

   Use these detection signals for the **common shared** mechanisms (**non-exhaustive** — one row
   per shared-mechanisms file; detect anything else from its own documented description):

   | Mechanism | Detection Signals | Shared pattern file |
   |-----------|-------------------|---------------------|
   | Hold & Spin | "coin symbols," "bonus trigger," "re-spin," "locked positions," "jackpot tiers (Minor/Major/Grand)," "coin collection," diagrams showing coins on a grid | `shared-mechanisms/hold-and-spin.md` |
   | Free Spin | "free spin," "freespin," "free rounds," "bonus spins," "scatter trigger," "spin count" | `shared-mechanisms/free-spin.md` |
   | Buy Bonus | "buy bonus," "purchase bonus," "direct entry," package names with multipliers ("80x," "100x") | `shared-mechanisms/buy-bonus.md` |
   | Auto Spin | "auto spin," "auto-play," "automatic spin," spin count options, stop conditions | `shared-mechanisms/auto-spin.md` |

6. Classify **every** detected mechanic (shared and custom) into exactly one tier:
   - **CONFIRMED** — direct, unambiguous textual evidence. Must have an exact quote.
   - **LIKELY** — partial or indirect evidence. Needs QA confirmation.
   - **ABSENT** — no evidence found and game type makes it unlikely (applies to the known shared mechanisms you looked for and didn't find).
   - **UNKNOWN** — insufficient documentation to determine.

7. **Compare each detected SHARED mechanism to its canonical shared pattern.** For every
   mechanism that has a matching `shared-mechanisms/{file}.md` and is classified CONFIRMED or
   LIKELY, re-read that file and compare the game's documented behaviour against the canonical
   pattern field by field — trigger condition, counts (spins/re-spins), locked/held state,
   retrigger/reset rules, payout/jackpot tiers, end conditions. Record exactly one verdict:
   - **MATCHES** — the game follows the shared pattern with no behavioural difference.
   - **DEVIATES** — list each concrete difference with an exact quote from the game docs (e.g. "shared free-spin retriggers on 3+ scatters; this game does NOT retrigger — 'no retrigger during free spins' rules.pdf p.4").

   **Custom mechanics have no shared file** → there is nothing to compare; document their
   behaviour directly from the docs and record them as custom (game-knowledge.md §4 "Custom
   Mechanics"). For shared mechanics, **never record MATCHES without actually reading the shared
   pattern file** (see anti-pattern #7). This comparison is surfaced to QA in Step 3 and written
   into game-knowledge.md §4 (Step 5).

### Step 3 — Present Findings and Request Confirmation

8. Present findings to QA in this exact format:

```
=== Mechanism Detection Results for {game} ===

CONFIRMED:
  [V] {Mechanic} — "{exact quote from docs}" (file: {filename}, page/section: {location})

LIKELY (needs confirmation):
  [?] {Mechanic} — partial match: "{exact quote or paraphrase}" — confirm?

ABSENT:
  [X] {Mechanic} — no documentation found; {note if relevant}

UNKNOWN:
  [~] {Mechanic} — insufficient documentation to determine

=== Mechanism vs Shared Pattern ===
(for each CONFIRMED/LIKELY mechanism, compared against shared-mechanisms/{file}.md)
  {Mechanic} — MATCHES shared-mechanisms/{file}.md
  {Mechanic} — DEVIATES: {difference} ("{exact quote}", {file}, {location})

=== Symbol Detection ===
Detected {N} symbols:
  {Symbol name} — ID: {id if found}, Type: {normal/wild/scatter/bonus/jackpot}

=== Grid and Paylines ===
Grid: {rows}x{cols} (or "not found in docs")
Paylines: {count} (or "not found in docs")

=== Questions for QA ===
1. Confirm mechanism classifications above (yes/correct each item)
2. Confirm each deviation is real game behaviour (not a documentation gap)
3. Are there any mechanics NOT listed above that exist in this game?
4. Any symbols missing from the detected list?
```

9. **STOP. Wait for QA response.** Do not generate any output files until QA has responded.

### Step 4 — Process QA Confirmation

10. After QA responds, apply corrections:
    - IF QA confirms all items: proceed with current classifications.
    - IF QA corrects a classification: update the mechanism list.
    - IF QA corrects a Matches/Deviates verdict (e.g. confirms a deviation is real, or says it's a doc gap): update the comparison.
    - IF QA adds mechanics not in `shared-mechanisms/`: note them as "custom mechanics."
    - IF QA corrects symbol names or adds missing symbols: incorporate corrections.
11. Acknowledge every correction explicitly: `"Captured: {correction summary}."`
12. Proceed to Step 5.

### Step 5 — Generate game-knowledge.md (+ paytable.md)

13. Write `games/{game}/docs/final/game-knowledge.md` (create the `docs/final/` directory if it
   does not exist) using the template below. Every section is mandatory — use
   `"Not documented in current source files."` for sections with no data.

```markdown
# {Game Name} — Game Knowledge

> Generated by init-knowledge on {today's date}.
> Source: games/{game}/docs/raw/ ({list of files read})
> Paytable detail: docs/final/paytable.md (if a paytable was documented; see step 13a)

---

## 1. Game Overview

| Field | Value |
|-------|-------|
| Game Name | {name} |
| Grid | {rows}x{cols} |
| Paylines | {count} |
| Bonus Trigger | {e.g. "3+ Coin symbols"} |

## 2. Symbols

| Symbol | Type | ID | Description | Payout Notes |
|--------|------|----|-------------|--------------|
| {name} | {normal/wild/scatter/bonus/jackpot} | {id} | {brief} | {one-line summary; full payouts in paytable.md} |

> Full symbol payouts are in [paytable.md](paytable.md) (if a paytable was documented).

## 3. Payline Definitions

{Describe payline layout. Full payline/payout detail is in [paytable.md](paytable.md); note the source file and page. If no paytable was documented: "Not documented in current source files."}

## 4. Mechanics

### Confirmed Mechanics

For each confirmed mechanic, document how it works in THIS game **and** how it compares to its canonical shared pattern (from Step 2's comparison).

**{Mechanic Name}** — vs `shared-mechanisms/{file}.md`: **Matches** | **Deviates**
- How it works here: {brief, cite source file + location}
- Comparison: {"Follows the shared pattern with no behavioural difference." OR list each deviation with an exact quote, e.g. "Shared pattern resets the re-spin counter to 3 on each new coin; this game keeps a fixed 3 with no reset (rules.pdf p.4)."}

### Custom Mechanics (Game-Specific)

{Game-specific mechanics not in shared-mechanisms/. If none: "None identified."}

## 5. Known Limitations and Out-of-Scope Items

{Behaviors not tested or QA-confirmed out of scope. If none: "None identified."}

## 6. Source Documentation Index

| File | Type | Key Information |
|------|------|-----------------|
| {filename} | {PDF/Excel/Image/MD} | {what was found} |
```

13a. **If the source docs document a paytable** (symbol payout tables, win values per match
   count, scatter/coin pays, jackpot tiers, or paytable rules/weights), extract it into a
   **separate** file `games/{game}/docs/final/paytable.md` and reference it from game-knowledge.md
   (§2 Symbols and §3 Paylines already link to `paytable.md`). Keep game-knowledge.md focused on
   structure and mechanics; put the detailed payout numbers in `paytable.md`. Apply the same
   rules as game-knowledge.md (no hallucinated values — anti-pattern #1; preserve ranges —
   anti-pattern #4; cite the source file + location). **If no paytable is documented, do NOT
   create the file** — leave the "Not documented in current source files." note in
   game-knowledge.md §3. Suggested shape:

   ```markdown
   # {Game Name} — Paytable

   > Generated by init-knowledge on {today's date}. Source: {paytable source file(s)}.

   ## Symbol Payouts
   | Symbol | 3x | 4x | 5x | Notes |
   |--------|----|----|----|-------|
   | {name} | {value} | {value} | {value} | {special rules / "See {file} p.N"} |

   ## Special / Bonus Payouts
   {scatter pays, coin values, jackpot tiers, multipliers — exactly as documented}

   ## Paytable Rules / Weights (if documented)
   {how outcomes are sampled / any weight tables — cite source; preserve ranges verbatim}
   ```

### Step 6 — Generate .docs-hash.json

14. Compute the SHA-256 hash of every file under `games/{game}/docs/` (all subdirectories),
    **EXCLUDING the generated `docs/final/` directory**. The files in `docs/final/`
    (`game-knowledge.md`, `paytable.md`) are init-knowledge **outputs**, not source — hashing
    them would make `/update-knowledge` treat its own output as drift and regenerate forever.
    The manifest tracks the **source** corpus (`docs/raw/`) plus `docs/assets/assets.yml`.
    ```bash
    python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "{absolute_file_path}"
    ```
15. Write `games/{game}/.docs-hash.json`:
    ```json
    {
      "generated_at": "{ISO 8601 timestamp}",
      "game": "{game}",
      "files": {
        "docs/raw/{filename}": "{sha256hex}",
        "docs/assets/assets.yml": "{sha256hex}"
      }
    }
    ```
    File paths must be relative to `games/{game}/` — not absolute.

### Step 7 — Generate assets.yml

16. Write `games/{game}/docs/assets/assets.yml` — the game's **asset manifest**. List every
    asset the docs let you identify; at **minimum it MUST include the symbols the docs mention**.
    Symbol image files live under `games/{game}/docs/assets/symbols/` and are referenced by a
    relative `file:` path. Use the **dict** structure the framework reads (a key → entry under
    each section; the AI reference pack is built from the `symbols:` and `ui_elements:` sections,
    and each key is the `ref_asset` used in test YAML):
    ```yaml
    # assets.yml — {game} asset manifest
    # Generated by init-knowledge on {date}
    # Keys are the ref_asset names used in test YAML; `file:` is relative to docs/assets/.

    symbols:                          # REQUIRED — every symbol the docs mention
      {symbol_key}:                   # lowercase snake_case identifier (the ref_asset key)
        file: symbols/{Symbol}.png    # image under docs/assets/symbols/ (.png or .jpg)
        name: "{display name}"
        type: {normal|wild|scatter|bonus|jackpot|low|high}
        description: "{what it is / how it functions — cite source}"

    ui_elements:                      # OPTIONAL — UI assets the docs mention (counters, buttons, popups)
      {ui_key}:
        file: ui/{element}.png        # image under docs/assets/ui/
        name: "{display name}"
        type: ui
        description: "{brief}"
    ```
    (Add further sections for other documented asset categories if useful; the runtime reference
    pack currently loads `symbols:` + `ui_elements:`.)
17. Reference the **actual filenames** when the images exist in `games/{game}/docs/assets/symbols/`
    (and `ui/`).
18. IF an asset's image file is not present yet: still list the asset with its intended `file:`
    path and add a comment `# image not yet added — drop the reference image here for AI assertions`.
    (Only assets whose `file:` exists on disk are loaded into the AI reference pack at runtime.)

### Step 8 — Confirm Completion

19. Output:
    ```
    === init-knowledge complete for {game} ===

    Files written:
      games/{game}/docs/final/game-knowledge.md   ({N} sections)
      games/{game}/docs/final/paytable.md         (only if a paytable was documented)
      games/{game}/.docs-hash.json                ({N} source files hashed)
      games/{game}/docs/assets/assets.yml         ({N} assets: symbols + UI)

    Mechanisms documented: {list confirmed mechanics — shared + custom}
    Symbols: {count} detected

    Next steps:
      1. Review games/{game}/docs/final/game-knowledge.md (+ paytable.md) for accuracy
      2. Add symbol reference images to games/{game}/docs/assets/symbols/
      3. Run /write-test --game={game} --suite=suite-01 to begin writing tests
    ```

---

## Examples

### Example 1: BAD vs GOOD — Mechanism Classification

**BAD:**
```
CONFIRMED:
  [V] Buy Bonus — the document mentions "bonus feature" (file: rules.pdf)
```
"Bonus feature" is not "buy bonus." This is mechanism overclaim — indirect evidence classified as CONFIRMED. Generates tests for a mechanic the game may not have.

**GOOD:**
```
LIKELY (needs confirmation):
  [?] Buy Bonus — partial match: "bonus feature can be accessed directly" (file: rules.pdf, p.4)
       — does this game have a purchasable buy bonus, or is "directly" referring to scatter triggers?
```
Honest classification. Cites the exact quote. Asks QA the right question. Avoids false certainty.

### Example 2: BAD vs GOOD — Handling Missing Data

**BAD (game-knowledge.md excerpt):**
```
## 3. Payline Definitions

## 4. Mechanics
```
Section 3 is blank — no content, no explanation. Downstream consumers can't tell if paylines don't exist, weren't documented, or were accidentally skipped.

**GOOD:**
```
## 3. Payline Definitions

Not documented in current source files. See paytable.pdf for potential
payline diagrams (file could not be fully parsed — manual review recommended).
```
Explicitly states what's missing and points to where data might be found.

### Example 3: BAD vs GOOD — Symbol Extraction

**BAD:**
```
| Symbol | Type | ID | Description | Payout Notes |
|--------|------|----|-------------|--------------|
| Golden Dragon | wild | 1 | A dragon | Pays well |
| Lucky Coin | bonus | 2 | A coin | Triggers bonus |
```
"Pays well" is vague. ID "1" and "2" are suspiciously sequential — likely fabricated. "A dragon" and "A coin" add nothing.

**GOOD:**
```
| Symbol | Type | ID | Description | Payout Notes |
|--------|------|----|-------------|--------------|
| Golden Dragon | wild | See paytable.pdf | Substitutes for all symbols except Scatter and Coin | 5x: 500, 4x: 200, 3x: 50 (paytable.pdf p.2) |
| Lucky Coin | bonus | 7 | Triggers Hold & Spin when 6+ land | No direct payout — awards coin values (rules.pdf p.5) |
```
IDs traced to source. Payout notes cite specific pages. Descriptions explain game function, not appearance.

---

## Questions This Skill Answers

- "Onboard [game] into the QA platform"
- "Set up a new game"
- "Init knowledge for [game]"
- "Read the docs for [game] and generate knowledge"
- "Start a new game project"
- "Process game documentation"
- "Create the knowledge files for [game]"
- "What shared mechanics does [game] use?"
- "Generate game-knowledge.md for [game]"
- "I have new game docs, set up the knowledge base"
- "Detect mechanics from the raw docs"
- "Parse the paytable and symbol info for [game]"
