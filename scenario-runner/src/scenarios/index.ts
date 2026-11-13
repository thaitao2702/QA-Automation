/**
 * The scenario REGISTRY: name -> class. A run always builds a FRESH instance with its `params:`
 * (the Python reused the registry instance when a run had no params; a fresh one per run cannot
 * leak state from one run into the next).
 */
import { SCENARIOS as AUTO_INPUT } from "./auto-input.js";
import type { ScenarioClass } from "./base.js";
import { SCENARIOS as CORE } from "./core.js";
import { SCENARIOS as GRANDMA } from "./grandma.js";
import { SCENARIOS as HOLDSPIN_BUY } from "./holdspin-buy.js";
import { SCENARIOS as LINE_FEATURES } from "./line-features.js";
import { SCENARIOS as UI_LASTBET } from "./ui-lastbet.js";

export const ALL_SCENARIOS: readonly ScenarioClass[] = [
  ...CORE, ...AUTO_INPUT, ...UI_LASTBET, ...HOLDSPIN_BUY, ...LINE_FEATURES, ...GRANDMA,
];

export const REGISTRY: ReadonlyMap<string, ScenarioClass> = new Map(ALL_SCENARIOS.map((Cls) => [new Cls().name, Cls]));
