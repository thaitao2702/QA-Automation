/**
 * Promo dialogs: `clearPromoPopups` must close every promo dialog WITHOUT claiming or discarding a
 * promo. `promo:receive:confirm` claims (or irreversibly discards) whenever `promo:receive:cancel`
 * is also showing, so it may only be pressed on the end-of-promo result, which has no cancel.
 *
 * The real page functions run against a fake `window.__gameTestBridge`; clicks are mapped back to
 * the button whose rect they hit.
 */
import { afterEach, describe, expect, it } from "vitest";
import type { Page } from "playwright";
import { clearPromoPopups, onPromoAction } from "../src/engine/bridge.js";
import { setProfile } from "../src/engine/game.js";

interface Dialog { popup: string; buttons: string[] }

/** A fake game showing `queue` one dialog at a time. Records every control clicked. */
function fakeGame(queue: Dialog[]) {
  const clicked: string[] = [];
  let current: Dialog | null = queue.shift() ?? null;
  let gap = false;                       // one empty read between two dialogs, as the game fades
  const rectOf = (name: string) => {
    const i = current ? current.buttons.indexOf(name) : -1;
    if (i < 0) return null;
    return { x: 0, y: 0, width: 10, height: 10, centerX: 100 + i * 20, centerY: 100, visible: true, interactable: true };
  };
  const bridge = {
    getSnapshot: () => {
      if (gap) {
        gap = false;
        return { visiblePopups: [] };
      }
      return { visiblePopups: current ? [current.popup] : [] };
    },
    getButtonRect: (n: string) => rectOf(n),
    dismissPopups: async () => {
      clicked.push("dismissPopups()");
      current = null;
      return [];
    },
  };
  const page = {
    evaluate: async (fn: any, arg?: unknown) => {
      (globalThis as any).window = { __gameTestBridge: bridge };
      return typeof fn === "function" ? fn(arg) : null;
    },
    mouse: {
      click: async (x: number) => {
        if (!current) return;
        const name = current.buttons[Math.round((x - 100) / 20)];
        clicked.push(name);
        current = queue.shift() ?? null;
        gap = true;
      },
    },
  } as unknown as Page;
  return { page, clicked };
}

afterEach(() => {
  delete (globalThis as any).window;
  setProfile("mystic");
});

const NEW_PROMO: Dialog = { popup: "Promo:receive", buttons: ["promo:receive:confirm", "promo:receive:cancel", "promo:receive:other"] };
const DISCARD: Dialog = { popup: "Promo:receive", buttons: ["promo:receive:confirm", "promo:receive:cancel"] };
const RESULT: Dialog = { popup: "Promo:receive", buttons: ["promo:receive:confirm"] };

describe("clearPromoPopups", () => {
  it("closes new-promo and discard dialogs with cancel, never confirm", async () => {
    setProfile("grandma");
    const { page, clicked } = fakeGame([NEW_PROMO, NEW_PROMO, DISCARD]);
    const marks: string[] = [];
    onPromoAction(page, (m) => marks.push(m));
    const done = await clearPromoPopups(page);
    expect(clicked).toEqual(["promo:receive:cancel", "promo:receive:cancel", "promo:receive:cancel"]);
    expect(done).toHaveLength(3);
    expect(marks).toEqual(done);
  });

  it("presses confirm only on the end-of-promo result, which has no cancel", async () => {
    setProfile("grandma");
    const { page, clicked } = fakeGame([RESULT]);
    await clearPromoPopups(page);
    expect(clicked).toEqual(["promo:receive:confirm"]);
  });

  it("closes the list the result can open, and a promo message", async () => {
    setProfile("grandma");
    const { page, clicked } = fakeGame([
      RESULT,
      { popup: "Promo:list", buttons: ["promo:list:close"] },
      { popup: "Promo:message", buttons: ["promo:message:ok"] },
    ]);
    await clearPromoPopups(page);
    expect(clicked).toEqual(["promo:receive:confirm", "promo:list:close", "promo:message:ok"]);
  });

  it("leaves other popups alone", async () => {
    setProfile("grandma");
    const { page, clicked } = fakeGame([{ popup: "SettingPopup", buttons: ["settings:close"] }]);
    expect(await clearPromoPopups(page)).toEqual([]);
    expect(clicked).toEqual([]);
  });

  it("is a no-op on a game without promo dialogs in its profile", async () => {
    setProfile("mystic");
    const { page, clicked } = fakeGame([NEW_PROMO]);
    expect(await clearPromoPopups(page)).toEqual([]);
    expect(clicked).toEqual([]);
  });
});
