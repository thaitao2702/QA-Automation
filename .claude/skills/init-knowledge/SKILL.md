# Skill: /init-knowledge

```yaml
name: init-knowledge
version: "2.0"
domain: [game-qa, documentation-onboarding, knowledge-generation]
description: |
  Onboards a new game into the QA platform by performing multimodal
  document ingestion (PDF, Excel, images, Markdown), shared mechanism
  fingerprinting with four-tier evidence classification
  (CONFIRMED/LIKELY/ABSENT/UNKNOWN), QA confirmation gate, and
  structured knowledge artifact generation (game-knowledge.md,
  .docs-hash.json, symbols.yml).

  Use when the user says "onboard a game," "init knowledge," "set up
  a new game," "read the docs for [game]," "start a new game,"
  "create knowledge for [game]," or "process game docs" -- even if
  they don't say "init-knowledge" explicitly.

  Do NOT use for updating existing knowledge (use /update-knowledge).
  Do NOT use for writing tests (use /write-test).
  Do NOT use for running tests (use /run-test).
```

**Usage:** `/init-knowledge --game=<game-name>`

---

## Expert Vocabulary Payload

**Document Processing & Extraction:**
multimodal document ingestion, paytable parsing, symbol extraction, grid topology detection (rows x cols), payline layout mapping, RTP extraction, win-type threshold detection

**Game Mechanics & Classification:**
shared mechanism fingerprinting, hold-and-spin (coin collect, locked positions, re-spin, jackpot tiers), free spin (scatter trigger, spin count, retrigger), buy bonus (direct entry, package multiplier), auto spin (stop conditions), reconnect (session restore, pending state), four-tier evidence classification (CONFIRMED/LIKELY/ABSENT/UNKNOWN)

**Knowledge Artifacts:**
game-knowledge.md (structured knowledge file), .docs-hash.json (SHA-256 hash manifest), symbols.yml (asset manifest with ref_asset keys), bridge-api.md (API surface documentation), source-of-truth hierarchy

**QA Pipeline:**
QA confirmation gate, mechanism detection report, evidence citation (exact quote + file + location), correction acknowledgment loop

---

## Anti-Pattern Watchlist

### 1. Hallucinated Game Data
**Detection:** Symbol names, IDs, payout values, or RTP percentages appear in output that cannot be traced to an exact quote or table in the raw docs.
**Why it's dangerous:** game-knowledge.md feeds directly into test generation. A fabricated symbol ID produces tests that pass against wrong data.
**Resolution:** For every data point in game-knowledge.md, verify it exists verbatim in the source docs. IF ambiguous: write `"See {filename}"` instead of guessing.

### 2. Skipping the QA Gate
**Detection:** game-knowledge.md, symbols.yml, or .docs-hash.json is written before QA has responded to the mechanism detection report.
**Why it's dangerous:** QA may know about mechanics not in the docs, or may correct a misclassification. Writing first forces a redo.
**Resolution:** Present findings. Wait for explicit QA response. Only then proceed to file generation.

### 3. Mechanism Overclaim
**Detection:** A mechanism is classified as CONFIRMED but the cited evidence is indirect (e.g., classifying "buy bonus" as CONFIRMED because the doc mentions "bonus" without "buy" or "purchase").
**Why it's dangerous:** Generates test cases for a mechanic the game doesn't have, wasting QA cycles.
**Resolution:** Use LIKELY for indirect evidence. Reserve CONFIRMED for direct, unambiguous textual evidence. Always include the exact quote.

### 4. Ambiguity Guessing
**Detection:** A field in game-knowledge.md contains a specific value (e.g., "RTP: 96.5%") when the source docs state it ambiguously or in a range.
**Resolution:** Write the ambiguity as-is: `"RTP: 95.0%-97.0% (see paytable.pdf p.3)"`. Never collapse a range into a single number.

### 5. Blank Section Suppression
**Detection:** A required section of game-knowledge.md is omitted entirely or left blank because no information was found.
**Why it's dangerous:** Downstream consumers (write-test, QA reviewers) can't tell if the section was intentionally empty or accidentally skipped.
**Resolution:** Write `"Not documented in current source files."` for every section where data is absent.

### 6. Silent File Skipping
**Detection:** A file in `docs/raw/` is not mentioned in the Source Documentation Index because the agent couldn't process it (e.g., a corrupted PDF or unsupported format).
**Resolution:** Include every file in the index. IF unreadable: add it with Key Information = `"Could not process — {reason}. Manual review required."`.

---

## Behavioral Instructions

### Step 1 — Locate and Read Raw Documentation

1. Read every file in `games/{game}/docs/raw/`. Files may be PDF, Excel (.xlsx), images (.png/.jpg), or Markdown.
2. For each file, extract:
   - Filename and type
   - Mechanic mentions: coins, bonus, free spins, buy bonus, auto spin, reconnect, jackpot, scatter, wild
   - Symbol names and IDs (numbers or names identifying reel symbols)
   - Payline count and grid size
   - RTP or theoretical return percentage
   - Win-type thresholds (e.g., big win = 10x bet)
3. Read all shared mechanism files:
   - `shared-mechanisms/hold-and-spin.md`
   - `shared-mechanisms/free-spin.md`
   - `shared-mechanisms/buy-bonus.md`
   - `shared-mechanisms/reconnect.md`
   - `shared-mechanisms/auto-spin.md`
4. IF `games/{game}/bridge-api.md` exists: read it fully.
5. IF any file cannot be processed: log it for the Source Documentation Index (see anti-pattern #6).

### Step 2 — Detect Shared Mechanisms

6. For each shared mechanism, search the raw docs for evidence. Use these detection signals:

   | Mechanism | Detection Signals |
   |-----------|-------------------|
   | Hold & Spin | "coin symbols," "bonus trigger," "re-spin," "locked positions," "jackpot tiers (Minor/Major/Grand)," "coin collection," diagrams showing coins on a grid |
   | Free Spin | "free spin," "freespin," "free rounds," "bonus spins," "scatter trigger," "spin count" |
   | Buy Bonus | "buy bonus," "purchase bonus," "direct entry," package names with multipliers ("80x," "100x") |
   | Auto Spin | "auto spin," "auto-play," "automatic spin," spin count options, stop conditions |
   | Reconnect | "reconnect," "network," "pending state," "session restore," "disconnection" — OR the game is an online slot (implies reconnect even if undocumented) |

7. Classify each mechanism into exactly one tier:
   - **CONFIRMED** — direct, unambiguous textual evidence. Must have an exact quote.
   - **LIKELY** — partial or indirect evidence. Needs QA confirmation.
   - **ABSENT** — no evidence found and game type makes it unlikely.
   - **UNKNOWN** — insufficient documentation to determine.

### Step 3 — Present Findings and Request Confirmation

8. Present findings to QA in this exact format:

```
=== Mechanism Detection Results for {game} ===

CONFIRMED:
  [V] {Mechanic} — "{exact quote from docs}" (file: {filename}, page/section: {location})

LIKELY (needs confirmation):
  [?] {Mechanic} — partial match: "{exact quote or paraphrase}" — confirm?

ABSENT:
  [X] {Mechanic} — no documentation found; {note if relevant, e.g. "all online slots should implement reconnect"}

UNKNOWN:
  [~] {Mechanic} — insufficient documentation to determine

=== Symbol Detection ===
Detected {N} symbols:
  {Symbol name} — ID: {id if found}, Type: {normal/wild/scatter/bonus/jackpot}

=== Grid and Paylines ===
Grid: {rows}x{cols} (or "not found in docs")
Paylines: {count} (or "not found in docs")
RTP: {value}% (or "not stated")

=== Questions for QA ===
1. Confirm mechanism classifications above (yes/correct each item)
2. Are there any mechanics NOT listed above that exist in this game?
3. Any symbols missing from the detected list?
```

9. **STOP. Wait for QA response.** Do not generate any output files until QA has responded.

### Step 4 — Process QA Confirmation

10. After QA responds, apply corrections:
    - IF QA confirms all items: proceed with current classifications.
    - IF QA corrects a classification: update the mechanism list.
    - IF QA adds mechanics not in `shared-mechanisms/`: note them as "custom mechanics."
    - IF QA corrects symbol names or adds missing symbols: incorporate corrections.
11. Acknowledge every correction explicitly: `"Captured: {correction summary}."`
12. Proceed to Step 5.

### Step 5 — Generate game-knowledge.md

13. Write `games/{game}/game-knowledge.md` using the template below. Every section is mandatory — use `"Not documented in current source files."` for sections with no data.

```markdown
# {Game Name} — Game Knowledge

> Generated by init-knowledge on {today's date}.
> Source: games/{game}/docs/raw/ ({list of files read})

---

## 1. Game Overview

| Field | Value |
|-------|-------|
| Game Name | {name} |
| Grid | {rows}x{cols} |
| Paylines | {count} |
| RTP | {value or "Not specified"} |
| Min Bet | {value or "See bridge-api.md"} |
| Max Bet | {value or "See bridge-api.md"} |
| Bonus Trigger | {e.g. "3+ Coin symbols"} |

## 2. Symbols

| Symbol | Type | ID | Description | Payout Notes |
|--------|------|----|-------------|--------------|
| {name} | {normal/wild/scatter/bonus/jackpot} | {id} | {brief} | {summary or "See paytable"} |

## 3. Payline Definitions

{Describe payline layout. If paytable PDF has diagrams, note the file and page.}

## 4. Mechanics

### Confirmed Mechanics

**{Mechanic Name}** — {How it works in this specific game. Reference shared-mechanisms/{file}.md for canonical patterns.}

### Custom Mechanics (Game-Specific)

{Game-specific mechanics not in shared-mechanisms/. If none: "None identified."}

## 5. Bridge API Summary

{If bridge-api.md exists:}

**Available Actions:**
- {action}: {one-line description}

**Available Queries:**
- {query}: {one-line description}

**Bridge Limitations:**
- {Missing capabilities needed for testing. If none: "None identified."}

## 6. Known Limitations and Out-of-Scope Items

{Behaviors not tested, unreachable via bridge, or QA-confirmed out of scope. If none: "None identified."}

## 7. Source Documentation Index

| File | Type | Key Information |
|------|------|-----------------|
| {filename} | {PDF/Excel/Image/MD} | {what was found} |
```

### Step 6 — Generate .docs-hash.json

14. Compute the SHA-256 hash of every file in `games/{game}/docs/` (including all subdirectories):
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
        "docs/assets/symbols.yml": "{sha256hex}"
      }
    }
    ```
    File paths must be relative to `games/{game}/` — not absolute.

### Step 7 — Generate symbols.yml

16. Write `games/{game}/docs/assets/symbols.yml`:
    ```yaml
    # Symbol asset manifest for {game}
    # Generated by init-knowledge on {date}
    # Keys are used as ref_assets in test suite YAML steps

    symbols:
      - key: {symbol_key}         # lowercase, snake_case identifier
        name: "{display name}"
        type: {normal|wild|scatter|bonus|jackpot}
        id: {numeric id or null if unknown}
        image: "symbols/{symbol_key}.png"
        description: "{brief description}"
    ```
17. IF symbol reference images exist in `games/{game}/docs/assets/symbols/`: reference actual filenames.
18. IF images do not exist: use the `{symbol_key}.png` convention and add comment: `# image not yet added — add reference PNG for AI assertions`.

### Step 8 — Confirm Completion

19. Output:
    ```
    === init-knowledge complete for {game} ===

    Files written:
      games/{game}/game-knowledge.md       ({N} sections)
      games/{game}/.docs-hash.json         ({N} files hashed)
      games/{game}/docs/assets/symbols.yml ({N} symbols)

    Mechanisms documented: {list confirmed mechanics}
    Symbols: {count} detected
    Bridge API: {available | not found -- bridge-api.md missing}

    Next steps:
      1. Review games/{game}/game-knowledge.md for accuracy
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
