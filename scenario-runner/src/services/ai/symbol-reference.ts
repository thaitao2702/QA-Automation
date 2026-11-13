/**
 * Symbol / UI reference loader for AI assertions (port of framework/helpers/symbol_reference.py).
 *
 * Builds a reusable "reference pack" from a game's `assets.yml` (legacy name: `symbols.yml`) so an
 * image/video AI request can be primed with the canonical appearance + description of each symbol
 * and UI element BEFORE the screenshot/video under test. This gives the model ground-truth context
 * to identify what it is looking at.
 *
 * The manifest is **group -> item**: top-level sections (`symbols:`, `ui_elements:`, `ui:`) are
 * *groups*; each key under them is an *item*. References can therefore be **scoped** to exactly
 * what an assert needs:
 *
 * - `asset_groups: [<group>, ...]`     — every item in those groups.
 * - `assets: ["<group>.<name>", ...]`  — specific items, qualified by group.
 *
 * Only assets whose reference image file actually exists on disk are included (the manifest may
 * list assets whose image has not been added yet).
 */
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { loadYaml } from "../env.js";
import { isDict, pyList, pyTypeName, truthy } from "../../py.js";
import { logger, pathJoin, pathParent, pyError, strOf } from "./py-support.js";

/** A prebuilt set of reference assets to prepend to every AI request. */
export class SymbolReference {
  constructor(
    readonly header: string,
    /** (image_path, label) */
    readonly items: Array<[string, string]> = [],
  ) {}

  get isEmpty(): boolean {
    return this.items.length === 0;
  }

  /** All labels as one text block — for providers that cannot take images. */
  descriptionsText(): string {
    const lines = [this.header, ""];
    for (const [, label] of this.items) lines.push(`- ${label}`);
    return lines.join("\n");
  }
}

const HEADER_TEMPLATE = (game: string) =>
  `REFERENCE ASSETS for the game '${game}'. The labeled images that follow are the `
  + "canonical, ground-truth appearance of each game symbol and UI element, each "
  + "preceded by its name and description. Use them to recognise what appears in the "
  + "screenshot/video under test below. These are reference context only — do not "
  + "describe or judge the reference images themselves.";

// The default full pack deliberately excludes the `ui:` section (full-screen states like
// base_game / free_spin overlay): those are only useful when explicitly selected.
export const DEFAULT_SECTIONS = ["symbols", "ui_elements"];

// `ref_assets` (bare names) is deprecated — warn once per process when first resolved.
let REF_ASSETS_DEPRECATION_WARNED = false;

type ManifestIndex = Map<string, Map<string, [string | null, string]>>;

/** `str + str` in Python: a non-str left operand raises TypeError. */
function concat(disp: unknown, tail: string): string {
  if (typeof disp === "string") return disp + tail;
  if (Array.isArray(disp)) throw pyError("TypeError", 'can only concatenate list (not "str") to list');
  throw pyError("TypeError", `unsupported operand type(s) for +: '${pyTypeName(disp)}' and 'str'`);
}

/** `Path.resolve()` — the real, on-disk spelling of an existing path. */
export function resolvePath(p: string): string {
  try {
    return realpathSync.native(p);
  } catch {
    return resolve(p);
  }
}

/**
 * Read the asset manifest once into a (group, name)-keyed index.
 *
 * Returns `[index, groupOrder, header]` where:
 *   - `index.get(group).get(name)` = `[imagePath | null, label]` for every entry of every top-level
 *     dict section. `imagePath` may not exist on disk (callers filter); it is null when the entry
 *     has no `file:` field.
 *   - `groupOrder.get(group)` = the entry names in manifest (YAML) order.
 *   - `header` = the reference-pack intro text.
 * Missing/unparseable manifest -> empty index + groupOrder (header still returned).
 */
export function readManifestIndex(game: string, baseDir: string): [ManifestIndex, Map<string, string[]>, string] {
  let assetDir = pathJoin(baseDir, "games", game, "docs", "assets");
  // Prefer the current manifest name (assets.yml); fall back to the legacy symbols.yml.
  let manifestPath = pathJoin(assetDir, "assets.yml");
  if (!existsSync(manifestPath)) manifestPath = pathJoin(assetDir, "symbols.yml");
  const header = HEADER_TEMPLATE(game);

  const index: ManifestIndex = new Map();
  const groupOrder = new Map<string, string[]>();

  if (!existsSync(manifestPath)) {
    logger.warning("asset manifest (assets.yml / symbols.yml) not found for %s: %s", game, assetDir);
    return [index, groupOrder, header];
  }

  let manifest: unknown;
  try {
    manifest = loadYaml(readFileSync(manifestPath, "utf-8"));
    if (!truthy(manifest)) manifest = {};
  } catch (exc) {
    logger.warning("Could not parse %s: %s", manifestPath, (exc as Error).message);
    return [index, groupOrder, header];
  }

  assetDir = pathParent(manifestPath);
  if (!isDict(manifest)) return [index, groupOrder, header];

  for (const [group, entries] of Object.entries(manifest)) {
    if (!isDict(entries)) continue;  // non-section top-level keys (metadata, scalars) are not groups
    const names: string[] = [];
    const byName = new Map<string, [string | null, string]>();
    for (const [name, entry] of Object.entries(entries)) {
      if (!isDict(entry)) continue;
      names.push(name);
      const fileRel = Object.prototype.hasOwnProperty.call(entry, "file") ? entry.file : "";
      let imgPath: string | null = null;
      if (truthy(fileRel)) {
        if (typeof fileRel !== "string") {
          throw pyError("TypeError", `unsupported operand type(s) for /: '${process.platform === "win32" ? "WindowsPath" : "PosixPath"}' and '${pyTypeName(fileRel)}'`);
        }
        imgPath = pathJoin(assetDir, fileRel);
      }
      const disp = Object.prototype.hasOwnProperty.call(entry, "name") ? entry.name : name;
      const desc = Object.prototype.hasOwnProperty.call(entry, "description") ? entry.description : "";
      // Human-readable label only (no internal manifest key) — what the model sees as the
      // per-image caption.
      const label = concat(disp, truthy(desc) ? ` — ${strOf(desc)}` : "");
      byName.set(name, [imgPath, label]);
    }
    index.set(group, byName);
    groupOrder.set(group, names);
  }

  return [index, groupOrder, header];
}

/**
 * Build a SymbolReference from whole manifest sections.
 *
 * `categories = null` loads the default full pack `["symbols", "ui_elements"]`; pass e.g.
 * `["ui_elements"]` to scope to those sections. Entries with an existing image file become
 * `[imagePath, label]` items, in section then manifest order. Missing files (and unknown sections)
 * are skipped and logged. Always returns a SymbolReference (possibly empty).
 */
export function loadSymbolReference(game: string, baseDir: string, categories: string[] | null = null): SymbolReference {
  const [index, groupOrder, header] = readManifestIndex(game, baseDir);
  const sections = categories !== null ? categories : DEFAULT_SECTIONS;
  const items: Array<[string, string]> = [];
  let missing = 0;

  for (const section of pyList(sections) as string[]) {
    const names = groupOrder.get(section);
    if (!names || !names.length) {
      logger.warning("asset category '%s' not found / empty for %s", section, game);
      continue;
    }
    for (const name of names) {
      const [imgPath, label] = index.get(section)!.get(name)!;
      if (imgPath === null) continue;
      if (!existsSync(imgPath)) {
        missing += 1;
        continue;
      }
      items.push([imgPath, label]);
    }
  }

  logger.info("Symbol reference for %s [%s]: %d asset image(s) loaded, %d missing file(s)",
    game, (pyList(sections) as string[]).join(","), items.length, missing);
  return new SymbolReference(header, items);
}

/**
 * Build a SymbolReference from a (groups, items) selection.
 *
 * The loaded reference is the **deduped union** of:
 *   - every item of each `assetGroups` group (in listed group order, items in manifest order), then
 *   - each `assets` `"{group}.{name}"` item (in listed order),
 * deduped by resolved image path (first occurrence wins).
 *
 * `assetGroups === null && assets === null` -> the full pack (today's default). Items whose image
 * file is missing on disk are warned + skipped; unknown group/name is warned and contributes
 * nothing (load-time validation catches these).
 */
export function loadReferenceSelection(game: string, baseDir: string, assetGroups: unknown, assets: unknown): SymbolReference {
  if (assetGroups === null && assets === null) return loadSymbolReference(game, baseDir);

  const [index, groupOrder, header] = readManifestIndex(game, baseDir);
  const items: Array<[string, string]> = [];
  const seen = new Set<string>();
  let missing = 0;

  const add = (group: string, name: string): void => {
    const entry = index.get(group)?.get(name);
    if (entry === undefined) {
      logger.warning("unknown asset '%s.%s' — not in manifest for %s", group, name, game);
      return;
    }
    const [imgPath, label] = entry;
    if (imgPath === null) return;  // manifest entry without a file: field
    if (!existsSync(imgPath)) {
      missing += 1;
      logger.warning("reference asset '%s.%s' image not found: %s", group, name, imgPath);
      return;
    }
    // Path equality is case-insensitive on Windows
    const resolved = process.platform === "win32" ? resolvePath(imgPath).toLowerCase() : resolvePath(imgPath);
    if (seen.has(resolved)) return;
    seen.add(resolved);
    items.push([imgPath, label]);
  };

  for (const group of pyList(truthy(assetGroups) ? assetGroups : []) as string[]) {
    const names = groupOrder.get(group);
    if (names === undefined) {
      logger.warning("unknown asset group '%s' for %s", group, game);
      continue;
    }
    for (const name of names) add(group, name);
  }

  for (const qualified of pyList(truthy(assets) ? assets : []) as unknown[]) {
    if (typeof qualified !== "string") {
      if (qualified === null || typeof qualified !== "object") {
        throw pyError("TypeError", `argument of type '${pyTypeName(qualified)}' is not iterable`);
      }
      logger.warning("asset '%s' must be '{group}.{name}' (qualified) — skipped", qualified);
      continue;
    }
    if (!qualified.includes(".")) {
      logger.warning("asset '%s' must be '{group}.{name}' (qualified) — skipped", qualified);
      continue;
    }
    const dot = qualified.indexOf(".");
    add(qualified.slice(0, dot), qualified.slice(dot + 1));
  }

  logger.info("Reference selection for %s: asset_groups=%s assets=%s -> %d image(s), %d missing file(s)",
    game, assetGroups, assets, items.length, missing);
  return new SymbolReference(header, items);
}

/**
 * group -> set(item names) over ALL manifest sections (for load-time validation).
 *
 * Includes items whose image file is missing (a real-but-imageless name is not a typo). Returns
 * `{}` when the manifest is absent/unparseable (callers skip validation in that case).
 */
export function buildAssetIndex(game: string, baseDir: string): Record<string, Set<string>> {
  const [, groupOrder] = readManifestIndex(game, baseDir);
  const out: Record<string, Set<string>> = {};
  for (const [group, names] of groupOrder) out[group] = new Set(names);
  return out;
}

/** Group lookup order for the `ref_assets` alias: symbols, ui_elements, then the rest. */
export function orderedGroups(assetIndex: Record<string, Set<string>>): string[] {
  const preferred = DEFAULT_SECTIONS.filter((g) => g in assetIndex);
  preferred.push(...Object.keys(assetIndex).filter((g) => !DEFAULT_SECTIONS.includes(g)));
  return preferred;
}

/**
 * Resolve deprecated bare `ref_assets` names to qualified `"{group}.{name}"`.
 *
 * A name found in multiple groups resolves deterministically (symbols > ui_elements > other
 * sections in manifest order) with an ambiguity warning; an unknown name is warned and dropped.
 * Returns `[]` when no index is available (cannot qualify).
 */
export function resolveRefAssetsAlias(names: unknown[], assetIndex: Record<string, Set<string>> | null): string[] {
  if (!REF_ASSETS_DEPRECATION_WARNED) {
    logger.warning("'ref_assets' is deprecated; use 'asset_groups'/'assets' (\"{group}.{name}\"). "
      + "Resolving bare names across all asset groups.");
    REF_ASSETS_DEPRECATION_WARNED = true;
  }

  if (!assetIndex || !Object.keys(assetIndex).length) return [];

  const order = orderedGroups(assetIndex);
  const qualified: string[] = [];
  for (const name of names) {
    const hits = order.filter((g) => assetIndex[g].has(name as string));
    if (!hits.length) {
      logger.warning("ref_asset '%s' not found in any asset group — skipped", name);
      continue;
    }
    if (hits.length > 1) {
      logger.warning("ref_asset '%s' is ambiguous (groups: %s); using '%s'", name, hits.join(", "), hits[0]);
    }
    qualified.push(`${hits[0]}.${strOf(name)}`);
  }
  return qualified;
}

/**
 * Resolve the (asset_groups, assets) selection in effect, assert > test > group.
 *
 * The most specific level (aiBlock, then test, then group) that sets *any* of `asset_groups` /
 * `assets` / `ref_assets` wins **entirely** — no cross-level or cross-field merge. A level that sets
 * the new fields ignores its own `ref_assets`. A `ref_assets` level is resolved via the deprecated
 * alias into a qualified `assets` list (empty resolution falls back to the full pack -> null).
 *
 * Returns `[asset_groups, assets]` (either element may be null), or null when no level sets
 * anything (caller uses the full pack default).
 */
export function effectiveSelection(
  groupDoc: unknown, testDoc: unknown, aiBlock: unknown, assetIndex: Record<string, Set<string>> | null = null,
): [unknown[] | null, unknown[] | null] | null {
  for (const level of [aiBlock, testDoc, groupDoc]) {
    if (!isDict(level)) continue;
    const hasGroups = Array.isArray(level.asset_groups);
    const hasAssets = Array.isArray(level.assets);
    if (hasGroups || hasAssets) {
      return [hasGroups ? (level.asset_groups as unknown[]) : null, hasAssets ? (level.assets as unknown[]) : null];
    }
    if (Array.isArray(level.ref_assets)) {
      const qualified = resolveRefAssetsAlias(level.ref_assets, assetIndex);
      return qualified.length ? [null, qualified] : null;
    }
  }
  return null;
}

/**
 * Build (and memoize) scoped SymbolReferences for one game by selection.
 *
 * Reused across tests so the same selection is only loaded from disk once. `forSelection(null,
 * null)` returns the full pack. Cache keys preserve order (image order is meaningful in the prompt)
 * and distinguish null (unset/full pack) from `[]` (explicitly empty).
 */
export class ScopedReferenceCache {
  private readonly cache = new Map<string, SymbolReference>();

  constructor(private readonly game: string, private readonly baseDir: string) {}

  forSelection(assetGroups: string[] | null, assets: string[] | null): SymbolReference {
    const key = JSON.stringify([assetGroups, assets]);
    let ref = this.cache.get(key);
    if (ref === undefined) {
      ref = loadReferenceSelection(this.game, this.baseDir, assetGroups, assets);
      this.cache.set(key, ref);
    }
    return ref;
  }

  /** Back-compat: a whole-section (category) list, or null for the full pack. */
  scoped(categories: string[] | null): SymbolReference {
    return this.forSelection(categories, null);
  }
}
