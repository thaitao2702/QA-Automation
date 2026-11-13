/**
 * Running bridge JavaScript in the page.
 *
 * ⚠ Python's Playwright CALLS a function passed as a string (`page.evaluate("() => 1")` -> 1). Node's
 * does NOT: a string is evaluated as an expression, so `"() => !!window.__gameTestBridge"` comes back
 * as an un-called function — and `waitForFunction` with that string resolves at once, because a
 * function is truthy. Every bridge call in the engine is written as a JS function string, so they all
 * go through `pageFunction`, which turns the source into a real function on the Node side (building
 * a function never runs its body) and lets Playwright send it as one.
 */
import type { Page } from "playwright";

const FUNCTION_SOURCE = /^\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/;

/** The JS source as something Playwright will CALL: a function when the source is one, else the
 * expression string as-is. */
export function pageFunction(js: string): string | ((arg?: unknown) => unknown) {
  if (!FUNCTION_SOURCE.test(js)) return js;
  // eslint-disable-next-line no-new-func
  return new Function(`return (${js});`)() as (arg?: unknown) => unknown;
}

/** `page.evaluate(js[, arg])` with Python Playwright's meaning. */
export async function evaluate<T = any>(page: Page, js: string, ...arg: [unknown?]): Promise<T> {
  const fn = pageFunction(js) as any;
  return (arg.length ? page.evaluate(fn, arg[0]) : page.evaluate(fn)) as Promise<T>;
}

/** `page.wait_for_function(js, timeout=...)` with Python Playwright's meaning. */
export async function waitForFunction(page: Page, js: string, timeoutMs: number): Promise<void> {
  await page.waitForFunction(pageFunction(js) as any, undefined, { timeout: timeoutMs });
}

/** `asyncio.sleep(s)`, in milliseconds. */
export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export class TimeoutError extends Error {
  override name = "TimeoutError";
}

/** `asyncio.wait_for(p, timeout)`: rejects with TimeoutError when `p` takes longer than `ms`. The
 * underlying work is not cancelled (Python's cancellation never reached the browser either). */
export async function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      p,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new TimeoutError(`timed out after ${ms}ms`)), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Seconds since the epoch, as Python's `time.time()`. */
export function now(): number {
  return Date.now() / 1000;
}
