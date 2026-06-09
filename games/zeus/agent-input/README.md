# agent-input — QA source material for test generation

Drop **all** game source material the test-author agent should read here. The
`/write-test` skill (Step 1) globs this whole folder and reads every file as the
authoritative description of the game's behavior, symbols, states, and bridge surface.

Anything useful goes here — there is no fixed schema or naming:

- Game rules / mechanic specs (base game, free spin, hold & spin, buy bonus, …)
- Paytable definitions and the forced-result paytables per test group
- Bridge API notes (method names, signatures, app states) — if not in `../bridge-api.md`
- State transition diagrams, reel layouts, win formulas
- Any raw docs exported from the game team (PDF text, spreadsheets-as-text, etc.)

Files here take precedence over `../game-knowledge.md` / `../bridge-api.md` when they
conflict. Keep it current — the agent only knows what this folder tells it.
