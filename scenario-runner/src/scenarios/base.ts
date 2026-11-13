/**
 * The scenario contract (the closed box). A SCENARIO is a closed, engineer-owned box: public contract
 * = name + description + the outputs it exposes; internals are hidden. `run(ctx)` returns a
 * ScenarioResult. Port of `class Scenario` in scenario_engine.py.
 *
 * Parameters: a suite run's `params:` build a FRESH instance (`new Cls(params)`). Each scenario
 * declares the keys it accepts in `PARAMS`; an unknown key is refused the way Python's constructor
 * refused an unexpected keyword argument, so a typo in a suite is an error, not a silent default.
 */
import type { DriveContext } from "../engine/drive.js";
import type { ScenarioResult } from "../engine/result.js";

export type Params = Record<string, unknown>;

export class BadParams extends Error {
  override name = "TypeError";
}

export abstract class Scenario {
  abstract readonly name: string;
  abstract readonly description: string;
  abstract run(ctx: DriveContext): Promise<ScenarioResult>;
}

export interface ScenarioClass {
  new (params?: Params): Scenario;
  /** The `params:` keys this scenario accepts (its Python `__init__` keyword arguments). */
  readonly PARAMS: readonly string[];
}

/** Refuse keys a scenario does not declare (Python: `__init__() got an unexpected keyword argument`). */
export function checkParams(cls: { name: string; PARAMS: readonly string[] }, params: Params): void {
  for (const k of Object.keys(params)) {
    if (!cls.PARAMS.includes(k)) throw new BadParams(`${cls.name}.__init__() got an unexpected keyword argument '${k}'`);
  }
}

/** `params[key]` when given, else the default — a constructor keyword with its default. */
export function param<T>(params: Params, key: string, fallback: T): T {
  return (Object.prototype.hasOwnProperty.call(params, key) ? params[key] : fallback) as T;
}
