/**
 * DOM Engine — talks directly to Chrome via CDP (Chrome DevTools Protocol).
 *
 * Architecture:
 * This plugin communicates with the Chromium instance managed by OpenClaw
 * through its CDP endpoint (typically http://127.0.0.1:18800).
 *
 * Instead of going through OpenClaw's internal browser control service
 * (which is embedded/in-memory and not accessible via HTTP), we speak
 * CDP directly. This is the most reliable approach because:
 *   - No dependency on OpenClaw internal APIs (fetchBrowserJson is tree-shaken)
 *   - No HTTP proxy needed (the browser control HTTP server is not started)
 *   - Direct, low-latency communication with Chrome
 *   - Works with any Chrome/Chromium that exposes CDP
 *
 * CDP transport: We use HTTP for simple requests (list targets, version) and
 * WebSocket for Runtime.evaluate and page commands.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DomEngineConfig {
  /** CDP endpoint URL (e.g. "http://127.0.0.1:18800") */
  cdpUrl: string;
  /** Default timeout in ms for operations */
  defaultTimeoutMs: number;
}

interface CdpTarget {
  id: string;
  type: string;
  title: string;
  url: string;
  webSocketDebuggerUrl?: string;
  devtoolsFrontendUrl?: string;
}

// ---------------------------------------------------------------------------
// CDP HTTP helpers (target discovery)
// ---------------------------------------------------------------------------

async function cdpGet<T>(cdpUrl: string, path: string, timeoutMs = 5000): Promise<T> {
  const url = `${cdpUrl.replace(/\/$/, "")}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) {
      throw new Error(`CDP HTTP ${res.status} from ${path}`);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

async function listTargets(cdpUrl: string): Promise<CdpTarget[]> {
  return cdpGet<CdpTarget[]>(cdpUrl, "/json/list");
}

async function getActivePage(cdpUrl: string): Promise<CdpTarget> {
  const targets = await listTargets(cdpUrl);
  // Prefer non-about:blank pages
  const pages = targets.filter(t => t.type === "page");
  const active = pages.find(t => !t.url.startsWith("about:")) ?? pages[0];
  if (!active) {
    throw new Error("No browser page found. Is the browser running?");
  }
  return active;
}

// ---------------------------------------------------------------------------
// CDP WebSocket command execution
// ---------------------------------------------------------------------------

let _msgId = 0;

async function cdpCommand<T = unknown>(
  wsUrl: string,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs = 15000,
): Promise<T> {
  // Prefer native WebSocket (Node 22+), fall back to 'ws' package
  let WS: typeof WebSocket | undefined = globalThis.WebSocket;
  if (!WS) {
    try {
      const mod = await import("ws");
      WS = (mod.default ?? mod.WebSocket ?? mod) as typeof WebSocket;
    } catch {
      // no ws package available either
    }
  }

  if (!WS) {
    throw new Error("No WebSocket implementation available. Requires Node 22+ (native WebSocket) or 'ws' package.");
  }

  const id = ++_msgId;

  return new Promise<T>((resolve, reject) => {
    const ws = new WS(wsUrl);
    const timer = setTimeout(() => {
      ws.close();
      reject(new Error(`CDP command "${method}" timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ id, method, params }));
    });

    ws.addEventListener("message", (event: { data: unknown }) => {
      try {
        const data = typeof event.data === "string" ? event.data : String(event.data);
        const msg = JSON.parse(data) as {
          id?: number;
          result?: T;
          error?: { message: string; code?: number };
        };
        if (msg.id === id) {
          clearTimeout(timer);
          ws.close();
          if (msg.error) {
            reject(new Error(`CDP error: ${msg.error.message}`));
          } else {
            resolve(msg.result as T);
          }
        }
      } catch (err) {
        clearTimeout(timer);
        ws.close();
        reject(err);
      }
    });

    ws.addEventListener("error", (err: unknown) => {
      clearTimeout(timer);
      const message = err instanceof Error ? err.message :
        (err && typeof err === "object" && "message" in err) ? String((err as { message: unknown }).message) :
        String(err);
      reject(new Error(`CDP WebSocket error: ${message}`));
    });
  });
}

/**
 * Run a CDP command on the active page. Automatically resolves the target.
 */
async function runOnPage<T = unknown>(
  config: DomEngineConfig,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
): Promise<T> {
  const timeout = timeoutMs ?? config.defaultTimeoutMs;
  const page = await getActivePage(config.cdpUrl);
  if (!page.webSocketDebuggerUrl) {
    throw new Error(`Page "${page.title}" has no WebSocket debugger URL`);
  }
  return cdpCommand<T>(page.webSocketDebuggerUrl, method, params, timeout);
}

/**
 * Evaluate JS in the page context via CDP Runtime.evaluate.
 */
async function evaluate(
  config: DomEngineConfig,
  expression: string,
  timeoutMs?: number,
): Promise<unknown> {
  const result = await runOnPage<{
    result: {
      type: string;
      value?: unknown;
      description?: string;
      subtype?: string;
      className?: string;
    };
    exceptionDetails?: {
      text: string;
      exception?: { description?: string; value?: unknown };
    };
  }>(config, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  }, timeoutMs);

  if (result.exceptionDetails) {
    const errDesc = result.exceptionDetails.exception?.description
      ?? result.exceptionDetails.text
      ?? "Unknown JS error";
    throw new Error(`JS evaluation error: ${errDesc}`);
  }

  return result.result.value;
}

// ---------------------------------------------------------------------------
// Screenshot helper (via CDP Page.captureScreenshot)
// ---------------------------------------------------------------------------

async function captureScreenshot(
  config: DomEngineConfig,
  opts?: { element?: string; fullPage?: boolean; format?: "png" | "jpeg" },
): Promise<string> {
  const format = opts?.format ?? "png";

  // If element screenshot requested, get its bounding rect first
  let clip: { x: number; y: number; width: number; height: number; scale: number } | undefined;
  if (opts?.element) {
    const rect = (await evaluate(config, `
      (() => {
        const el = document.querySelector(${JSON.stringify(opts.element)});
        if (!el) throw new Error('Element not found: ${opts.element}');
        el.scrollIntoView({ block: 'center', behavior: 'instant' });
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      })()
    `)) as { x: number; y: number; width: number; height: number };
    clip = { ...rect, scale: 1 };
  }

  // For full page, we need the layout metrics
  if (opts?.fullPage && !clip) {
    const metrics = await runOnPage<{
      contentSize: { width: number; height: number };
    }>(config, "Page.getLayoutMetrics");
    clip = {
      x: 0,
      y: 0,
      width: metrics.contentSize.width,
      height: metrics.contentSize.height,
      scale: 1,
    };
  }

  const params: Record<string, unknown> = {
    format,
    quality: format === "jpeg" ? 80 : undefined,
    captureBeyondViewport: !!opts?.fullPage,
  };
  if (clip) {
    params.clip = clip;
  }

  const result = await runOnPage<{ data: string }>(
    config,
    "Page.captureScreenshot",
    params,
  );

  return result.data; // base64-encoded image
}

// ---------------------------------------------------------------------------
// Navigation helper
// ---------------------------------------------------------------------------

async function navigatePage(
  config: DomEngineConfig,
  url: string,
  timeoutMs?: number,
): Promise<{ targetId: string; url: string }> {
  const timeout = timeoutMs ?? config.defaultTimeoutMs;
  const page = await getActivePage(config.cdpUrl);
  if (!page.webSocketDebuggerUrl) {
    throw new Error("No WebSocket debugger URL for active page");
  }

  const result = await cdpCommand<{
    frameId: string;
    loaderId?: string;
    errorText?: string;
  }>(page.webSocketDebuggerUrl, "Page.navigate", { url }, timeout);

  if (result.errorText) {
    throw new Error(`Navigation error: ${result.errorText}`);
  }

  return { targetId: page.id, url };
}

// ---------------------------------------------------------------------------
// Poll-based waiting helpers
// ---------------------------------------------------------------------------

async function pollUntil(
  config: DomEngineConfig,
  checkFn: string,
  timeoutMs: number,
  pollIntervalMs = 500,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await evaluate(config, checkFn);
    if (result) return;
    await new Promise(r => setTimeout(r, pollIntervalMs));
  }
  throw new Error(`Wait timed out after ${timeoutMs}ms`);
}

// ---------------------------------------------------------------------------
// Save screenshot to disk (via Node fs)
// ---------------------------------------------------------------------------

async function saveBase64ToDisk(base64: string, format: "png" | "jpeg"): Promise<string> {
  const { writeFile, mkdtemp } = await import("node:fs/promises");
  const { join } = await import("node:path");
  const { tmpdir } = await import("node:os");

  const dir = await mkdtemp(join(tmpdir(), "dom-screenshot-"));
  const ext = format === "jpeg" ? "jpg" : "png";
  const filePath = join(dir, `screenshot-${Date.now()}.${ext}`);
  await writeFile(filePath, Buffer.from(base64, "base64"));
  return filePath;
}

// ---------------------------------------------------------------------------
// DOM Engine
// ---------------------------------------------------------------------------

export function createDomEngine(config: DomEngineConfig) {
  return {
    // ----- Navigation -----

    async navigate(
      url: string,
    ): Promise<{ targetId: string; url: string }> {
      return navigatePage(config, url);
    },

    // ----- Wait strategies -----

    async waitForSelector(
      selector: string,
      opts?: { timeoutMs?: number },
    ): Promise<void> {
      const timeout = opts?.timeoutMs ?? config.defaultTimeoutMs;
      const escaped = JSON.stringify(selector);
      await pollUntil(
        config,
        `(() => { const el = document.querySelector(${escaped}); return el && el.offsetParent !== null; })()`,
        timeout,
      );
    },

    async waitForText(
      text: string,
      opts?: { timeoutMs?: number; gone?: boolean },
    ): Promise<void> {
      const timeout = opts?.timeoutMs ?? config.defaultTimeoutMs;
      const escaped = JSON.stringify(text);
      if (opts?.gone) {
        await pollUntil(
          config,
          `(() => !document.body.innerText.includes(${escaped}))()`,
          timeout,
        );
      } else {
        await pollUntil(
          config,
          `(() => document.body.innerText.includes(${escaped}))()`,
          timeout,
        );
      }
    },

    async waitForLoadState(
      _state: "load" | "domcontentloaded" | "networkidle" = "networkidle",
      opts?: { timeoutMs?: number },
    ): Promise<void> {
      const timeout = opts?.timeoutMs ?? config.defaultTimeoutMs;
      // Simple approach: wait for document.readyState to be 'complete'
      await pollUntil(
        config,
        `(() => document.readyState === 'complete')()`,
        timeout,
      );
      // Extra 500ms settle for networkidle-like behavior
      await new Promise(r => setTimeout(r, 500));
    },

    async waitForUrl(
      urlPattern: string,
      opts?: { timeoutMs?: number },
    ): Promise<void> {
      const timeout = opts?.timeoutMs ?? config.defaultTimeoutMs;
      const escaped = JSON.stringify(urlPattern);
      await pollUntil(
        config,
        `(() => window.location.href.includes(${escaped}) || new RegExp(${escaped}).test(window.location.href))()`,
        timeout,
      );
    },

    async waitForFunction(
      fn: string,
      opts?: { timeoutMs?: number },
    ): Promise<void> {
      const timeout = opts?.timeoutMs ?? config.defaultTimeoutMs;
      await pollUntil(
        config,
        `(${fn})()`,
        timeout,
      );
    },

    // ----- JavaScript evaluation -----

    async evaluate(fn: string): Promise<unknown> {
      // Support both "() => expr" and raw expressions
      const expression = fn.trim().startsWith("(")
        ? `(${fn.trim()})()`
        : fn;
      return evaluate(config, expression);
    },

    // ----- Click via CSS selector -----

    async clickSelector(
      selector: string,
      opts?: { timeoutMs?: number },
    ): Promise<void> {
      await this.waitForSelector(selector, { timeoutMs: opts?.timeoutMs });
      const escaped = JSON.stringify(selector);
      await evaluate(config, `(() => {
        const el = document.querySelector(${escaped});
        if (!el) throw new Error('Element not found: ' + ${escaped});
        el.scrollIntoView({ block: 'center', behavior: 'instant' });
        el.click();
        return { clicked: true, tag: el.tagName, text: (el.textContent || '').slice(0, 100) };
      })()`);
    },

    // ----- Click via XPath -----

    async clickXPath(
      xpath: string,
    ): Promise<void> {
      const escaped = JSON.stringify(xpath);
      await evaluate(config, `(() => {
        const result = document.evaluate(${escaped}, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        const el = result.singleNodeValue;
        if (!el) throw new Error('XPath not found: ' + ${escaped});
        el.scrollIntoView({ block: 'center', behavior: 'instant' });
        el.click();
        return { clicked: true, tag: el.tagName, text: (el.textContent || '').slice(0, 100) };
      })()`);
    },

    // ----- Click via text content -----

    async clickText(
      text: string,
      opts?: { exact?: boolean; tag?: string; timeoutMs?: number },
    ): Promise<void> {
      await this.waitForText(text, { timeoutMs: opts?.timeoutMs });
      const escapedText = JSON.stringify(text);
      const tag = JSON.stringify(opts?.tag ?? "*");
      const exact = opts?.exact ?? false;
      await evaluate(config, `(() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        let node;
        while ((node = walker.nextNode())) {
          if (${tag} !== '*' && node.tagName.toLowerCase() !== ${tag}.toLowerCase()) continue;
          const nodeText = (node.textContent || '').trim();
          const match = ${exact}
            ? nodeText === ${escapedText}
            : nodeText.includes(${escapedText});
          if (match && node.offsetParent !== null) {
            node.scrollIntoView({ block: 'center', behavior: 'instant' });
            node.click();
            return { clicked: true, tag: node.tagName, text: nodeText.slice(0, 100) };
          }
        }
        throw new Error('Text not found in visible elements: ' + ${escapedText});
      })()`);
    },

    // ----- Fill input -----

    async fillSelector(
      selector: string,
      value: string,
      opts?: { submit?: boolean; timeoutMs?: number },
    ): Promise<void> {
      await this.waitForSelector(selector, { timeoutMs: opts?.timeoutMs });
      const escapedSelector = JSON.stringify(selector);
      const escapedValue = JSON.stringify(value);
      const submit = opts?.submit ?? false;
      await evaluate(config, `(() => {
        const el = document.querySelector(${escapedSelector});
        if (!el) throw new Error('Element not found: ' + ${escapedSelector});
        el.scrollIntoView({ block: 'center', behavior: 'instant' });
        el.focus();
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value'
        )?.set || Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value'
        )?.set;
        if (nativeInputValueSetter) {
          nativeInputValueSetter.call(el, ${escapedValue});
        } else {
          el.value = ${escapedValue};
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        ${submit ? `el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true })); el.closest('form')?.requestSubmit?.();` : ""}
        return { filled: true, value: ${escapedValue}.slice(0, 50) };
      })()`);
    },

    // ----- Extract data -----

    async extract(
      selector: string,
      opts?: { attribute?: string; all?: boolean },
    ): Promise<string | string[]> {
      const escapedSelector = JSON.stringify(selector);
      const attr = JSON.stringify(opts?.attribute ?? "textContent");
      const all = opts?.all ?? false;
      const result = await evaluate(config, `(() => {
        ${all ? `
        const elements = Array.from(document.querySelectorAll(${escapedSelector}));
        if (!elements.length) throw new Error('No elements found: ' + ${escapedSelector});
        return elements.map(el => {
          const val = ${attr} === 'textContent' ? (el.textContent || '').trim()
            : ${attr} === 'innerHTML' ? el.innerHTML
            : el.getAttribute(${attr}) || '';
          return val;
        });` : `
        const el = document.querySelector(${escapedSelector});
        if (!el) throw new Error('Element not found: ' + ${escapedSelector});
        const val = ${attr} === 'textContent' ? (el.textContent || '').trim()
          : ${attr} === 'innerHTML' ? el.innerHTML
          : el.getAttribute(${attr}) || '';
        return val;`}
      })()`);
      return result as string | string[];
    },

    // ----- Screenshot -----

    async screenshot(opts?: {
      element?: string;
      fullPage?: boolean;
      type?: "png" | "jpeg";
    }): Promise<{ path: string }> {
      const format = opts?.type ?? "png";
      const base64 = await captureScreenshot(config, {
        element: opts?.element,
        fullPage: opts?.fullPage,
        format,
      });
      const filePath = await saveBase64ToDisk(base64, format);
      return { path: filePath };
    },

    // ----- Tab/target management -----

    async listTabs(): Promise<CdpTarget[]> {
      return listTargets(config.cdpUrl);
    },

    // ----- Compound helpers -----

    async navigateAndWait(
      url: string,
      opts?: { waitFor?: string; timeoutMs?: number },
    ): Promise<{ targetId: string; url: string }> {
      const nav = await this.navigate(url);
      // Wait for page to start loading
      await new Promise(r => setTimeout(r, 1000));
      if (opts?.waitFor) {
        await this.waitForSelector(opts.waitFor, { timeoutMs: opts.timeoutMs });
      } else {
        await this.waitForLoadState("networkidle", { timeoutMs: opts?.timeoutMs });
      }
      return nav;
    },

    async queryAll(
      selector: string,
      opts?: { limit?: number },
    ): Promise<Array<{ tag: string; text: string; attributes: Record<string, string> }>> {
      const escapedSelector = JSON.stringify(selector);
      const limit = opts?.limit ?? 20;
      const result = await evaluate(config, `(() => {
        const els = Array.from(document.querySelectorAll(${escapedSelector})).slice(0, ${limit});
        return els.map(el => ({
          tag: el.tagName.toLowerCase(),
          text: (el.textContent || '').trim().slice(0, 200),
          attributes: Object.fromEntries(
            Array.from(el.attributes).map(a => [a.name, a.value.slice(0, 200)])
          ),
        }));
      })()`);
      return result as Array<{ tag: string; text: string; attributes: Record<string, string> }>;
    },
  };
}
