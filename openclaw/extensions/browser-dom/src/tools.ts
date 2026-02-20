/**
 * Agent tool definitions for the browser-dom plugin.
 *
 * Each tool wraps one or more DOM engine methods and exposes them
 * to the LLM agent via the standard OpenClaw tool interface.
 *
 * Naming: all tools use the `dom_` prefix to avoid conflicts
 * with the built-in `browser` tool.
 */

import { Type } from "@sinclair/typebox";
import type { DomEngineConfig } from "./dom-engine.js";
import { createDomEngine } from "./dom-engine.js";

// ---------------------------------------------------------------------------
// Shared engine instance (lazy, created on first tool call)
// ---------------------------------------------------------------------------

let _engine: ReturnType<typeof createDomEngine> | null = null;

function getEngine(config: DomEngineConfig) {
  if (!_engine) {
    _engine = createDomEngine(config);
  }
  return _engine;
}

// ---------------------------------------------------------------------------
// Helper: build config from plugin config + defaults
// ---------------------------------------------------------------------------

export function resolveConfig(pluginConfig?: Record<string, unknown>): DomEngineConfig {
  const cfg = pluginConfig ?? {};
  return {
    cdpUrl:
      typeof cfg.cdpUrl === "string" && cfg.cdpUrl
        ? cfg.cdpUrl
        : "http://127.0.0.1:18800",
    defaultTimeoutMs:
      typeof cfg.defaultTimeoutMs === "number" && cfg.defaultTimeoutMs > 0
        ? cfg.defaultTimeoutMs
        : 15_000,
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_navigate
// ---------------------------------------------------------------------------

export function createDomNavigateTool(config: DomEngineConfig) {
  return {
    name: "dom_navigate",
    description: [
      "Navigate the browser to a URL and optionally wait for a CSS selector to appear.",
      "Uses the existing OpenClaw browser instance (no new browser launched).",
      "The browser is already logged in if cookies/session exist in the profile.",
    ].join(" "),
    parameters: Type.Object({
      url: Type.String({ description: "The URL to navigate to." }),
      waitForSelector: Type.Optional(
        Type.String({
          description:
            "CSS selector to wait for after navigation (e.g. '.menu-list', '#main-content'). If omitted, waits for networkidle.",
        }),
      ),
      timeoutMs: Type.Optional(
        Type.Number({ description: "Timeout in ms (default: 15000)." }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const url = String(params.url ?? "").trim();
      if (!url) throw new Error("url is required");

      const waitFor =
        typeof params.waitForSelector === "string"
          ? params.waitForSelector.trim() || undefined
          : undefined;
      const timeoutMs =
        typeof params.timeoutMs === "number" ? params.timeoutMs : undefined;

      const result = await engine.navigateAndWait(url, { waitFor, timeoutMs });

      return {
        content: [
          {
            type: "text" as const,
            text: `Navigated to ${url} (targetId: ${result.targetId}). Page is ready.${
              waitFor ? ` Selector "${waitFor}" is visible.` : ""
            }`,
          },
        ],
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_click
// ---------------------------------------------------------------------------

export function createDomClickTool(config: DomEngineConfig) {
  return {
    name: "dom_click",
    description: [
      "Click an element on the page using a CSS selector, XPath expression, or visible text.",
      "Automatically waits for the element to appear before clicking.",
      "Does NOT require a snapshot or ref — works directly with selectors.",
      "Prefer CSS selectors with data-testid when available, then aria labels, then text content.",
    ].join(" "),
    parameters: Type.Object({
      selector: Type.Optional(
        Type.String({
          description:
            'CSS selector (e.g. "button.add-to-cart", "[data-testid=\'checkout\']", "#submit-btn").',
        }),
      ),
      xpath: Type.Optional(
        Type.String({
          description:
            'XPath expression (e.g. "//button[contains(@class, \'submit\')]").',
        }),
      ),
      text: Type.Optional(
        Type.String({
          description:
            'Click the first visible element containing this text (e.g. "Add to cart", "Checkout").',
        }),
      ),
      exact: Type.Optional(
        Type.Boolean({
          description:
            "If true, text match must be exact (not substring). Default: false.",
        }),
      ),
      tag: Type.Optional(
        Type.String({
          description:
            'Limit text matching to this HTML tag (e.g. "button", "a", "span"). Default: any tag.',
        }),
      ),
      timeoutMs: Type.Optional(
        Type.Number({ description: "Timeout in ms (default: 15000)." }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const selector =
        typeof params.selector === "string" ? params.selector.trim() : "";
      const xpath =
        typeof params.xpath === "string" ? params.xpath.trim() : "";
      const text =
        typeof params.text === "string" ? params.text.trim() : "";
      const exact =
        typeof params.exact === "boolean" ? params.exact : false;
      const tag =
        typeof params.tag === "string" ? params.tag.trim() : undefined;
      const timeoutMs =
        typeof params.timeoutMs === "number" ? params.timeoutMs : undefined;

      const methods = [selector, xpath, text].filter(Boolean);
      if (methods.length === 0) {
        throw new Error(
          "Provide at least one of: selector (CSS), xpath, or text.",
        );
      }
      if (methods.length > 1) {
        throw new Error(
          "Provide only one of: selector (CSS), xpath, or text.",
        );
      }

      if (selector) {
        await engine.clickSelector(selector, { timeoutMs });
        return {
          content: [
            { type: "text" as const, text: `Clicked element matching CSS selector "${selector}".` },
          ],
        };
      }
      if (xpath) {
        await engine.clickXPath(xpath);
        return {
          content: [
            { type: "text" as const, text: `Clicked element matching XPath "${xpath}".` },
          ],
        };
      }
      // text
      await engine.clickText(text, { exact, tag, timeoutMs });
      return {
        content: [
          {
            type: "text" as const,
            text: `Clicked element containing text "${text}"${tag ? ` (tag: ${tag})` : ""}.`,
          },
        ],
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_fill
// ---------------------------------------------------------------------------

export function createDomFillTool(config: DomEngineConfig) {
  return {
    name: "dom_fill",
    description: [
      "Fill a text input, textarea, or contenteditable element using a CSS selector.",
      "Automatically waits for the element to appear, clears existing value, and types the new value.",
      "Works with React/Vue inputs by using native value setter and dispatching input/change events.",
      "Optionally submits the form after filling.",
    ].join(" "),
    parameters: Type.Object({
      selector: Type.String({
        description:
          'CSS selector of the input element (e.g. "input[name=\'search\']", "#email-field", "textarea.comment").',
      }),
      value: Type.String({
        description: "The text value to fill in.",
      }),
      submit: Type.Optional(
        Type.Boolean({
          description:
            "If true, press Enter and submit the form after filling. Default: false.",
        }),
      ),
      timeoutMs: Type.Optional(
        Type.Number({ description: "Timeout in ms (default: 15000)." }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const selector = String(params.selector ?? "").trim();
      const value = String(params.value ?? "");
      const submit =
        typeof params.submit === "boolean" ? params.submit : false;
      const timeoutMs =
        typeof params.timeoutMs === "number" ? params.timeoutMs : undefined;

      if (!selector) throw new Error("selector is required");

      await engine.fillSelector(selector, value, { submit, timeoutMs });

      return {
        content: [
          {
            type: "text" as const,
            text: `Filled "${selector}" with "${value.slice(0, 50)}${value.length > 50 ? "..." : ""}"${submit ? " and submitted form" : ""}.`,
          },
        ],
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_wait
// ---------------------------------------------------------------------------

export function createDomWaitTool(config: DomEngineConfig) {
  return {
    name: "dom_wait",
    description: [
      "Wait for a condition to be met on the page before proceeding.",
      "Supports waiting for: CSS selector to appear, text to appear/disappear,",
      "URL change, page load state, or a custom JavaScript condition.",
      "Use this before interacting with dynamic content (lazy loading, animations, API calls).",
    ].join(" "),
    parameters: Type.Object({
      selector: Type.Optional(
        Type.String({
          description:
            'Wait for this CSS selector to become visible (e.g. ".product-list", "#checkout-form").',
        }),
      ),
      text: Type.Optional(
        Type.String({
          description: 'Wait for this text to appear on the page.',
        }),
      ),
      textGone: Type.Optional(
        Type.String({
          description:
            'Wait for this text to disappear (e.g. "Loading...", "Please wait").',
        }),
      ),
      url: Type.Optional(
        Type.String({
          description:
            'Wait for URL to match this pattern (e.g. "**/checkout", "https://example.com/done").',
        }),
      ),
      loadState: Type.Optional(
        Type.String({
          description:
            'Wait for page load state: "load", "domcontentloaded", or "networkidle".',
        }),
      ),
      fn: Type.Optional(
        Type.String({
          description:
            'Custom JS function to wait for. Must return truthy when condition is met. E.g. "() => document.querySelectorAll(\'.item\').length > 5".',
        }),
      ),
      timeoutMs: Type.Optional(
        Type.Number({ description: "Timeout in ms (default: 15000)." }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const timeoutMs =
        typeof params.timeoutMs === "number" ? params.timeoutMs : undefined;

      const selector =
        typeof params.selector === "string" ? params.selector.trim() : "";
      const text =
        typeof params.text === "string" ? params.text.trim() : "";
      const textGone =
        typeof params.textGone === "string" ? params.textGone.trim() : "";
      const url =
        typeof params.url === "string" ? params.url.trim() : "";
      const loadState =
        typeof params.loadState === "string" ? params.loadState.trim() : "";
      const fn =
        typeof params.fn === "string" ? params.fn.trim() : "";

      const conditions: string[] = [];

      if (selector) {
        await engine.waitForSelector(selector, { timeoutMs });
        conditions.push(`selector "${selector}" is visible`);
      }
      if (text) {
        await engine.waitForText(text, { timeoutMs });
        conditions.push(`text "${text}" appeared`);
      }
      if (textGone) {
        await engine.waitForText(textGone, { timeoutMs, gone: true });
        conditions.push(`text "${textGone}" disappeared`);
      }
      if (url) {
        await engine.waitForUrl(url, { timeoutMs });
        conditions.push(`URL matches "${url}"`);
      }
      if (loadState) {
        const valid = loadState as "load" | "domcontentloaded" | "networkidle";
        await engine.waitForLoadState(valid, { timeoutMs });
        conditions.push(`page reached "${loadState}"`);
      }
      if (fn) {
        await engine.waitForFunction(fn, { timeoutMs });
        conditions.push("custom JS condition met");
      }

      if (conditions.length === 0) {
        throw new Error(
          "Provide at least one wait condition: selector, text, textGone, url, loadState, or fn.",
        );
      }

      return {
        content: [
          {
            type: "text" as const,
            text: `Wait complete: ${conditions.join("; ")}.`,
          },
        ],
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_extract
// ---------------------------------------------------------------------------

export function createDomExtractTool(config: DomEngineConfig) {
  return {
    name: "dom_extract",
    description: [
      "Extract text content, attributes, or structured data from page elements.",
      "Use this to read prices, product names, cart contents, form values, links, etc.",
      "Can extract from a single element or multiple elements matching a selector.",
    ].join(" "),
    parameters: Type.Object({
      selector: Type.String({
        description:
          'CSS selector to extract from (e.g. ".cart-total", "h1.product-name", "a.nav-link").',
      }),
      attribute: Type.Optional(
        Type.String({
          description:
            'Attribute to extract. Default: "textContent". Other options: "href", "src", "value", "innerHTML", "data-*", etc.',
        }),
      ),
      all: Type.Optional(
        Type.Boolean({
          description:
            "If true, extract from ALL matching elements (returns array). Default: false (first match only).",
        }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const selector = String(params.selector ?? "").trim();
      const attribute =
        typeof params.attribute === "string" ? params.attribute.trim() : "textContent";
      const all =
        typeof params.all === "boolean" ? params.all : false;

      if (!selector) throw new Error("selector is required");

      const result = await engine.extract(selector, { attribute, all });

      const text = Array.isArray(result)
        ? `Found ${result.length} elements:\n${result.map((v, i) => `  [${i}] ${String(v).slice(0, 300)}`).join("\n")}`
        : `Result: ${String(result).slice(0, 1000)}`;

      return {
        content: [{ type: "text" as const, text }],
        details: { selector, attribute, all, result },
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_screenshot
// ---------------------------------------------------------------------------

export function createDomScreenshotTool(config: DomEngineConfig) {
  return {
    name: "dom_screenshot",
    description: [
      "Take a screenshot of the current page or a specific element.",
      "The screenshot is saved to disk and can be sent to the user.",
      "Use this for visual confirmation (e.g. showing the cart before checkout).",
    ].join(" "),
    parameters: Type.Object({
      element: Type.Optional(
        Type.String({
          description:
            'CSS selector to screenshot a specific element (e.g. ".checkout-summary", "#cart"). If omitted, screenshots the full visible page.',
        }),
      ),
      fullPage: Type.Optional(
        Type.Boolean({
          description:
            "If true, capture the full scrollable page. Default: false (visible viewport only).",
        }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const element =
        typeof params.element === "string" ? params.element.trim() || undefined : undefined;
      const fullPage =
        typeof params.fullPage === "boolean" ? params.fullPage : false;

      const result = await engine.screenshot({ element, fullPage });

      return {
        content: [
          {
            type: "text" as const,
            text: `Screenshot saved to: ${result.path}${element ? ` (element: "${element}")` : ""}`,
          },
        ],
        details: { path: result.path, element, fullPage },
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_query
// ---------------------------------------------------------------------------

export function createDomQueryTool(config: DomEngineConfig) {
  return {
    name: "dom_query",
    description: [
      "Query the page DOM to discover elements and their structure.",
      "Returns tag names, text content, and attributes of matching elements.",
      "Use this to understand page structure before clicking or filling.",
      "Useful when you don't know the exact selector — explore the DOM first.",
    ].join(" "),
    parameters: Type.Object({
      selector: Type.String({
        description:
          'CSS selector to query (e.g. "button", "input", "[data-testid]", ".menu-item", "a[href*=cart]"). Use broad selectors to explore.',
      }),
      limit: Type.Optional(
        Type.Number({
          description:
            "Max number of results to return. Default: 20.",
        }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const selector = String(params.selector ?? "").trim();
      const limit =
        typeof params.limit === "number" ? params.limit : 20;

      if (!selector) throw new Error("selector is required");

      const results = await engine.queryAll(selector, { limit });

      if (results.length === 0) {
        return {
          content: [
            {
              type: "text" as const,
              text: `No elements found matching "${selector}".`,
            },
          ],
        };
      }

      const lines = results.map((r, i) => {
        const attrs = Object.entries(r.attributes)
          .filter(([k]) => !["style"].includes(k))
          .map(([k, v]) => `${k}="${v.slice(0, 80)}"`)
          .join(" ");
        const text = r.text ? ` text="${r.text.slice(0, 120)}"` : "";
        return `  [${i}] <${r.tag} ${attrs}>${text}`;
      });

      return {
        content: [
          {
            type: "text" as const,
            text: `Found ${results.length} elements matching "${selector}":\n${lines.join("\n")}`,
          },
        ],
        details: { selector, count: results.length, results },
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Tool: dom_evaluate
// ---------------------------------------------------------------------------

export function createDomEvaluateTool(config: DomEngineConfig) {
  return {
    name: "dom_evaluate",
    description: [
      "Execute arbitrary JavaScript in the browser page context.",
      "Use this for complex DOM operations that other dom_* tools can't handle.",
      "The JS function runs in the page — it has access to document, window, etc.",
      "Must be a function expression that returns a value: '() => { ... return result; }'",
      "REQUIRES browser.evaluateEnabled=true in the OpenClaw config.",
    ].join(" "),
    parameters: Type.Object({
      fn: Type.String({
        description:
          'JavaScript function to execute. Must be a function expression, e.g. "() => document.title" or "() => { const el = document.querySelector(\'.price\'); return el?.textContent; }"',
      }),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const engine = getEngine(config);
      const fn = String(params.fn ?? "").trim();
      if (!fn) throw new Error("fn is required");

      const result = await engine.evaluate(fn);

      const text =
        typeof result === "object"
          ? JSON.stringify(result, null, 2)
          : String(result ?? "undefined");

      return {
        content: [
          {
            type: "text" as const,
            text: `Evaluate result:\n${text.slice(0, 2000)}`,
          },
        ],
        details: { result },
      };
    },
  };
}
