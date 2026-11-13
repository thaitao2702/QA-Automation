/**
 * What a scenario hands back, and the closed-box contract around it.
 *
 * Field names are snake_case: they are written to result.json and bound by suites
 * (`{facts.x}`, `video: at: <mark>`), so they are a data contract shared with the Python runner.
 */
import type { Bet } from "./bet.js";
import { GAME } from "./game.js";

export type Window = [number, number];

export interface Shot {
  label: string;
  file: string;
  path: string;
}

export class ScenarioResult {
  reason: string | null = null;             // why unreached
  round_windows: Window[] = [];             // per-round sub-windows, indexed by seq
  marks: Record<string, unknown> = {};      // scenario-exposed name -> round seq (video: at:)
  facts: Record<string, unknown> = {};      // scenario-exposed values, bound as {facts.*}
  shots: Shot[] = [];                       // captured stills (image:)

  constructor(
    public name: string,
    public reached: boolean,
    public res: Bet | null,                 // the key round -> {res.*}
    public rounds: Bet[],                   // all collected rounds -> {rounds[i].*}, {trigger}, {terminal}
    public timeline: unknown[],
    public events: unknown[],
    public video_window: Window | null,     // whole-scenario (start_s, end_s) into session.webm
    init: Partial<Pick<ScenarioResult, "reason" | "round_windows" | "marks" | "facts" | "shots">> = {},
  ) {
    Object.assign(this, init);
  }
}

/**
 * A precondition this scenario cannot get past. The message IS the run's `reason`.
 *
 * **Catch THIS type, never a bare Error.** Unreached means the game could not be brought to the
 * situation — an honest inconclusive. Any other error means the scenario is broken and must keep
 * propagating, or our own bug gets filed as "the build does not support this".
 */
export class Unreached extends Error {
  override name = "Unreached";
}

/**
 * Resolve every control this scenario will click, up front, and raise `Unreached` naming any the
 * profile does not have. Returns the build's names, in the order asked. A role is a string, or
 * `[role, index]` for a templated name (`buyBonus:package:{i}`).
 */
export function requireButtons(...roles: Array<string | [string, number]>): string[] {
  const names: string[] = [];
  const missing: string[] = [];
  for (const r of roles) {
    const [role, idx] = Array.isArray(r) ? r : [r, null];
    const name = idx !== null ? GAME.button(role, { i: idx }) : GAME.button(role);
    names.push(name as string);
    if (!name) missing.push(role);
  }
  if (missing.length) {
    throw new Unreached(`\`${GAME.name}\` profile has no control for: ${missing.join(", ")} (scripts/game_profile.py)`);
  }
  return names;
}

