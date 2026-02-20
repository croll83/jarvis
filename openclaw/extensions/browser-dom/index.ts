/**
 * Browser DOM plugin for OpenClaw.
 *
 * Registers a suite of DOM manipulation tools that talk directly to the
 * OpenClaw browser control service via HTTP. These tools use CSS selectors,
 * XPath, and text matching instead of snapshot refs — eliminating the
 * stale-ref problem on dynamic sites like Deliveroo, Amazon, etc.
 *
 * The plugin does NOT launch a separate browser. It connects to the browser
 * instance already managed by the OpenClaw gateway (the same one the built-in
 * `browser` tool uses).
 *
 * Tools registered:
 *   dom_navigate    — navigate to URL + wait for page ready
 *   dom_click       — click via CSS selector / XPath / text
 *   dom_fill        — fill input fields via CSS selector
 *   dom_wait        — wait for selector / text / URL / loadState / JS condition
 *   dom_extract     — extract text / attributes from elements
 *   dom_screenshot  — screenshot page or specific element
 *   dom_query       — discover elements on the page (inspect DOM structure)
 *   dom_evaluate    — execute arbitrary JS in page context
 */

import {
  resolveConfig,
  createDomNavigateTool,
  createDomClickTool,
  createDomFillTool,
  createDomWaitTool,
  createDomExtractTool,
  createDomScreenshotTool,
  createDomQueryTool,
  createDomEvaluateTool,
} from "./src/tools.js";

interface OpenClawPluginApi {
  id: string;
  name: string;
  config: Record<string, unknown>;
  pluginConfig?: Record<string, unknown>;
  logger: {
    debug: (msg: string) => void;
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
  registerTool: (
    tool: unknown,
    opts?: { optional?: boolean; name?: string; names?: string[] },
  ) => void;
}

export default function register(api: OpenClawPluginApi) {
  const config = resolveConfig(api.pluginConfig);

  api.logger.info(
    `[browser-dom] Registering DOM tools → CDP ${config.cdpUrl}`,
  );

  // Register all tools. They are optional so the agent can use them
  // alongside the built-in browser tool, not as a replacement.
  const tools = [
    createDomNavigateTool(config),
    createDomClickTool(config),
    createDomFillTool(config),
    createDomWaitTool(config),
    createDomExtractTool(config),
    createDomScreenshotTool(config),
    createDomQueryTool(config),
    createDomEvaluateTool(config),
  ];

  for (const tool of tools) {
    api.registerTool(tool, { optional: true });
    api.logger.debug(`[browser-dom] Registered tool: ${tool.name}`);
  }

  api.logger.info(
    `[browser-dom] ${tools.length} DOM tools registered successfully.`,
  );
}
