/**
 * The ACTIVE game profile. The runner points it at a game with `setProfile()` (from `--game`); the
 * scenarios and checks read it through the live binding `GAME`, so they stay game-agnostic.
 */
import { getProfile } from "../profile/games.js";
import type { SlotGame } from "../profile/slot-game.js";

export let GAME: SlotGame = getProfile("mystic");

/** Point the engine at a game's profile. */
export function setProfile(name: string): SlotGame {
  GAME = getProfile(name);
  return GAME;
}

/** gameState: base game, of the active profile. */
export function baseState(): number {
  return GAME.BASE_STATE;
}

/** Languages the SITE offers, as (code, name, script). `all` / `random:N` selections draw from this
 * list. Script names go into the AI prompts. */
export const SUPPORTED_LANGUAGES: ReadonlyArray<readonly [string, string, string]> = [
  ["en", "English", "Latin"], ["vi", "Vietnamese", "Latin (with diacritics)"],
  ["th", "Thai", "Thai"], ["my", "Burmese", "Burmese/Myanmar"],
  ["zh", "Simplified Chinese", "Chinese (Han)"], ["ja", "Japanese", "Japanese (kana/kanji)"],
  ["ko", "Korean", "Hangul"], ["hi", "Hindi", "Devanagari"],
  ["ru", "Russian", "Cyrillic"], ["ms", "Bahasa Melayu", "Latin"],
  ["id", "Indonesian", "Latin"], ["tl", "Tagalog", "Latin"],
  ["pt", "Portuguese", "Latin"], ["pt-br", "Portuguese (Brazil)", "Latin"],
  ["es", "Spanish", "Latin"], ["tr", "Turkish", "Latin"],
];
export const LANGUAGE_NAME: Readonly<Record<string, string>> = Object.fromEntries(
  SUPPORTED_LANGUAGES.map(([code, name]) => [code, name]),
);
/** Burmese is the stress case the test matrix calls out (Mah_054/055): the longest strings and a
 * script with stacked diacritics, so it breaks text scaling and clipping first. */
export const STRESS_LANGUAGE = "my";
