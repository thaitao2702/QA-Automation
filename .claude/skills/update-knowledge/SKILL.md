# Skill: /update-knowledge

```yaml
name: update-knowledge
version: "2.0"
domain: [game-qa, documentation-lifecycle, knowledge-maintenance]
depends_on: init-knowledge
description: |
  Detects whether raw game documentation has drifted from the last
  init-knowledge snapshot using SHA-256 content hashing and a stored
  hash manifest. If no drift: report and exit without writes. If drift
  detected: classify deltas (modified/new/deleted), report the change
  set, then trigger full knowledge regeneration via the init-knowledge
  pipeline with QA confirmation gate.

  Use when the user says "update knowledge," "refresh docs," "check if
  docs changed," "re-sync knowledge," "regenerate knowledge," or
  "has anything changed in the docs?" -- even if they don't say
  "update-knowledge" explicitly.

  Do NOT use for first-time onboarding (use /init-knowledge).
  Do NOT use for editing game-knowledge.md by hand.
```

**Usage:** `/update-knowledge --game=<game-name>`

---

## Expert Vocabulary Payload

**File Integrity & Change Detection:**
SHA-256 content hash, hash manifest (`.docs-hash.json`), dirty check, delta classification, drift detection, file enumeration, content-addressed comparison

**Knowledge Lifecycle:**
full regeneration (not partial diff), context interaction risk, knowledge staleness, QA confirmation gate, mechanism re-detection, idempotent output

**Document Pipeline:**
raw documentation corpus (`docs/raw/`), asset manifest (`symbols.yml`), game knowledge file (`game-knowledge.md`), source-of-truth hierarchy

---

## Anti-Pattern Watchlist

### 1. Partial Regeneration
**Detection:** Attempting to patch only changed sections of `game-knowledge.md` instead of full regeneration.
**Why it's wrong:** A change in one document can alter conclusions drawn from all other documents (context interaction). A coin value table change in `paytable.pdf` affects hold-and-spin mechanics documented from `bonus-rules.md`.
**Resolution:** Always perform full regeneration via init-knowledge pipeline. Read all current files from scratch.

### 2. Silent Overwrite
**Detection:** Writing `game-knowledge.md` without presenting mechanism detection findings to QA first.
**Why it's wrong:** A doc change may introduce a new mechanic or remove a previously confirmed one. QA must verify before the knowledge file is overwritten.
**Resolution:** Always present detection findings and wait for QA confirmation before any write to `game-knowledge.md`.

### 3. Stale Manifest Entry
**Detection:** A deleted file still appears in the updated `.docs-hash.json`.
**Why it's wrong:** Future runs will attempt to hash a nonexistent file, causing false-positive drift detection or errors.
**Resolution:** Build the new manifest exclusively from files that currently exist on disk. Never carry forward entries from the old manifest without verifying the file still exists.

### 4. Premature Write on No-Change
**Detection:** Any file write occurs when the change set is empty (no modified, no new, no deleted).
**Why it's wrong:** Wastes QA's time, creates misleading git diffs, and violates the idempotency contract of this skill.
**Resolution:** IF all three delta lists are empty: print the no-change report and stop. Do not touch any file.

### 5. Hash Computation Mismatch
**Detection:** Using a different hashing method, encoding, or file-open mode than the one used during `init-knowledge`.
**Why it's wrong:** Even identical files will show as "modified" if the hash algorithm or binary/text mode differs.
**Resolution:** Always use the exact command: `python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "{path}"`. Binary mode (`'rb'`) is mandatory.

---

## Behavioral Instructions

### Step 1 — Locate Hash Manifest

1. Check whether `games/{game}/.docs-hash.json` exists.
2. IF it does NOT exist:
   ```
   No .docs-hash.json found for {game}.
   init-knowledge has not been run yet for this game.
   Run /init-knowledge --game={game} first.
   ```
   STOP. Do not proceed.
3. IF it exists: read its contents. Note the `generated_at` timestamp and the `files` map.

### Step 2 — Enumerate Current Files

4. List all files currently in `games/{game}/docs/` recursively (all subdirectories, all file types).
5. Build a list using paths relative to `games/{game}/` (matching the key format in `.docs-hash.json`).
   - Example key: `docs/raw/paytable.pdf`, `docs/assets/symbols.yml`

### Step 3 — Compute Current Hashes

6. For every file found in Step 2, compute its SHA-256 hash:
   ```bash
   python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "{absolute_file_path}"
   ```
7. Build a map: `{relative_path: sha256hex}` for all current files.

### Step 4 — Classify Deltas

8. Compare the current map (Step 3) against the stored `.docs-hash.json` `files` map.
9. Classify every file into exactly one category:
   - **Modified** — present in both maps, hashes differ.
   - **New** — present in current map only.
   - **Deleted** — present in stored map only.
   - **Unchanged** — present in both maps, hashes match.

### Step 5 — Report and Branch

10. IF modified, new, and deleted lists are ALL empty → **Case A (No Changes).**
11. IF any list is non-empty → **Case B (Changes Detected).**

#### Case A — No Changes

12. Output:
    ```
    === update-knowledge: {game} ===

    No changes detected.
    Last updated: {generated_at from .docs-hash.json}
    Checked {N} files -- all hashes match.

    No action taken. games/{game}/game-knowledge.md is current.
    ```
13. STOP. Do not modify any file.

#### Case B — Changes Detected

14. Output the change report:
    ```
    === update-knowledge: {game} ===

    Changes detected since {generated_at}:

    Modified ({N}):
      - {relative_path}

    New ({N}):
      - {relative_path}

    Deleted ({N}):
      - {relative_path}

    Regenerating knowledge files...
    ```
15. Proceed to Step 6.

### Step 6 — Full Regeneration via init-knowledge

16. Execute the complete init-knowledge pipeline (Steps 1-8 of `/init-knowledge`) using all files currently on disk.
    - Read all current files in `games/{game}/docs/raw/` from scratch.
    - Load shared mechanisms from `shared-mechanisms/`.
    - Detect mechanics from current docs.
    - **Present detection findings to QA.** Wait for confirmation before writing.
    - Write `game-knowledge.md`.
    - Write `symbols.yml` IF symbol changes are detected.

17. After regeneration, write the updated `.docs-hash.json`:
    ```json
    {
      "generated_at": "{current ISO 8601 timestamp}",
      "game": "{game}",
      "files": {
        "{relative_path}": "{sha256hex}"
      }
    }
    ```
    Include ONLY files that currently exist. Deleted files must not appear.

### Step 7 — Confirm Completion

18. Output:
    ```
    === update-knowledge complete for {game} ===

    Changes processed:
      Modified: {N} file(s)
      New:      {N} file(s)
      Deleted:  {N} file(s)

    Files regenerated:
      games/{game}/game-knowledge.md
      games/{game}/.docs-hash.json
      games/{game}/docs/assets/symbols.yml  (if symbols changed)

    Last updated: {new generated_at timestamp}
    ```

---

## Examples

### Example 1: BAD vs GOOD — No-Change Handling

**BAD:**
```
No changes detected. Regenerating anyway to be safe...
Writing game-knowledge.md...
Done.
```
Violates anti-pattern #4 (Premature Write on No-Change). Creates a false git diff. Wastes QA time on a spurious confirmation prompt.

**GOOD:**
```
=== update-knowledge: fortune-tiger ===

No changes detected.
Last updated: 2026-03-15T14:30:00Z
Checked 7 files -- all hashes match.

No action taken. games/fortune-tiger/game-knowledge.md is current.
```
No files written. No QA prompt. Exits cleanly.

### Example 2: BAD vs GOOD — Change Report

**BAD:**
```
Some files changed. Updating knowledge...
[proceeds to rewrite game-knowledge.md without showing QA what changed]
```
Violates anti-pattern #2 (Silent Overwrite). QA never saw what changed or confirmed the new mechanism detection.

**GOOD:**
```
=== update-knowledge: fortune-tiger ===

Changes detected since 2026-03-15T14:30:00Z:

Modified (1):
  - docs/raw/paytable.pdf

New (1):
  - docs/raw/bonus-rules-v2.md

Deleted (1):
  - docs/raw/bonus-rules-draft.md

Regenerating knowledge files...

=== Mechanism Detection Results for fortune-tiger ===
[full detection findings presented here]
[waits for QA confirmation before writing]
```
Shows exactly what changed, then runs the full init-knowledge detection and waits for QA sign-off.

### Example 3: Deleted File Handling

**Scenario:** `docs/raw/old-paytable.pdf` was removed from disk but still exists in `.docs-hash.json`.

**BAD:** Updated `.docs-hash.json` still contains `"docs/raw/old-paytable.pdf": "abc123..."` because it was copied from the old manifest.

**GOOD:** Updated `.docs-hash.json` omits `docs/raw/old-paytable.pdf` entirely. Only files verified to exist on disk appear in the new manifest.

---

## Questions This Skill Answers

- "Update knowledge for [game]"
- "Have the docs changed for [game]?"
- "Refresh the knowledge files"
- "Re-sync knowledge after doc updates"
- "Check if [game] docs are stale"
- "Regenerate game-knowledge.md"
- "I added new docs to [game], now what?"
- "I deleted some old docs, update the knowledge"
- "Is game-knowledge.md still current?"
- "When was knowledge last updated for [game]?"
- "Docs changed, re-run knowledge generation"
