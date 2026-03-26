/**
 * video-context — OpenClaw plugin
 *
 * Injects ONLY the relevant reference content into the video-producer
 * agent's prompt, based on the current project state (project.yaml).
 *
 * - No project active → minimal capabilities summary
 * - Storyboard draft → project-guide
 * - Scene pending with type t2v → T2V section from comfyui-workflows
 * - Assembly phase → assembly reference
 * - etc.
 *
 * Only activates for agents listed in `targetAgents` config.
 */

import { readFileSync, readdirSync, existsSync, appendFileSync, statSync } from "fs";
import { join, resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CHARS_PER_TOKEN = 3.5;

interface PluginConfig {
  targetAgents: string[];
  referencesPath: string;
  projectsBasePath: string;
  maxContextTokens: number;
  logPath: string;
}

// ---------------------------------------------------------------------------
// Reference file cache
// ---------------------------------------------------------------------------

const refCache = new Map<string, string>();
let refsCachedAt = 0;
const CACHE_TTL = 5 * 60 * 1000;

function readRef(refsPath: string, name: string): string {
  const absPath = resolve(__dirname, refsPath, `${name}.md`);
  if (Date.now() - refsCachedAt > CACHE_TTL) {
    refCache.clear();
    refsCachedAt = Date.now();
  }
  if (refCache.has(name)) return refCache.get(name)!;
  if (!existsSync(absPath)) return "";
  const content = readFileSync(absPath, "utf-8");
  refCache.set(name, content);
  return content;
}

// ---------------------------------------------------------------------------
// Section extraction from comfyui-workflows.md
// ---------------------------------------------------------------------------

/**
 * Extract a specific workflow section (### N. Title) from the reference.
 * Also always includes the "API Endpoints" section and "Note importanti".
 */
function extractWorkflowSections(
  fullContent: string,
  sectionNumbers: number[]
): string {
  const lines = fullContent.split("\n");
  const parts: string[] = [];

  // Always include header + API Endpoints (up to first ### numbered section)
  const firstH3Idx = lines.findIndex((l) => /^### \d+\./.test(l));
  if (firstH3Idx > 0) {
    parts.push(lines.slice(0, firstH3Idx).join("\n").trim());
  }

  // Extract requested sections
  for (const num of sectionNumbers) {
    const pattern = new RegExp(`^### ${num}\\.\\s`);
    const startIdx = lines.findIndex((l) => pattern.test(l));
    if (startIdx === -1) continue;
    // Find next ### or ## or end
    let endIdx = lines.findIndex(
      (l, i) => i > startIdx && /^##[#]?\s/.test(l)
    );
    if (endIdx === -1) endIdx = lines.length;
    parts.push(lines.slice(startIdx, endIdx).join("\n").trim());
  }

  // Always include "Note importanti" section
  const noteIdx = lines.findIndex((l) => /^## Note importanti/.test(l));
  if (noteIdx !== -1) {
    parts.push(lines.slice(noteIdx).join("\n").trim());
  }

  return parts.join("\n\n");
}

// Map scene types to workflow section numbers
// Includes aliases for common variations the LLM might use
const TYPE_TO_SECTIONS: Record<string, number[]> = {
  t2i: [1],
  text_to_image: [1],
  edit: [1, 2],
  t2i_edit: [1, 2],
  image_edit: [1, 2],
  fusion: [1, 3],
  multi_fusion: [1, 3],
  t2v: [4],
  text_to_video: [4],
  i2v: [4, 5],
  image_to_video: [4, 5],
  v2v: [6],
  video_to_video: [6],
  vace: [6],
  faceswap: [7],
  face_swap: [7],
  reactor: [7],
  lipsync: [8],
  lip_sync: [8],
  liveportrait: [8],
  live_portrait: [8],
};

// ---------------------------------------------------------------------------
// Project state parser (simple YAML — no dependency needed)
// ---------------------------------------------------------------------------

interface ProjectState {
  slug: string;
  status: string;
  nextScene: { type: string; description: string; index: number } | null;
  allDone: boolean;
  hasAudio: boolean;
  yaml: string;
}

function parseProjectState(
  projectsBasePath: string
): ProjectState | null {
  if (!existsSync(projectsBasePath)) return null;

  const dirs = readdirSync(projectsBasePath, { withFileTypes: true }).filter(
    (d) => d.isDirectory()
  );

  // Find most recently modified project.yaml
  let best: { slug: string; yaml: string; mtime: number } | null = null;
  for (const d of dirs) {
    const yamlPath = join(projectsBasePath, d.name, "project.yaml");
    if (!existsSync(yamlPath)) continue;
    try {
      const mtime = statSync(yamlPath).mtimeMs;
      if (!best || mtime > best.mtime) {
        best = {
          slug: d.name,
          yaml: readFileSync(yamlPath, "utf-8"),
          mtime,
        };
      }
    } catch {
      continue;
    }
  }

  if (!best) return null;

  const yaml = best.yaml;

  // Extract status
  const statusMatch = yaml.match(/^status:\s*(\S+)/m);
  const status = statusMatch?.[1] ?? "draft";

  // Find first pending scene
  // Look for scene blocks: "- scene: N" followed by type/status
  const sceneBlocks = yaml.split(/(?=- scene:)/g).filter((b) =>
    b.includes("- scene:")
  );

  let nextScene: ProjectState["nextScene"] = null;
  let allDone = true;

  for (const block of sceneBlocks) {
    const sceneNum = block.match(/scene:\s*(\d+)/)?.[1];
    const sceneType = block.match(/type:\s*(\S+)/)?.[1];
    const sceneStatus = block.match(/status:\s*(\S+)/)?.[1];
    const sceneDesc = block.match(/description:\s*"?([^"\n]+)"?/)?.[1];

    if (sceneStatus && sceneStatus !== "done") {
      allDone = false;
      if (!nextScene) {
        nextScene = {
          type: sceneType ?? "t2i",
          description: sceneDesc ?? "",
          index: parseInt(sceneNum ?? "1"),
        };
      }
    }
  }

  // Check audio state
  const hasAudioSection =
    yaml.includes("narration:") || yaml.includes("music:");
  const audioNeeded =
    hasAudioSection &&
    (yaml.match(/narration:\s*null/) || yaml.match(/music:\s*null/));

  return {
    slug: best.slug,
    status,
    nextScene,
    allDone: sceneBlocks.length > 0 && allDone,
    hasAudio: !!audioNeeded,
    yaml,
  };
}

// ---------------------------------------------------------------------------
// Context builder
// ---------------------------------------------------------------------------

function buildContext(
  config: PluginConfig,
  project: ProjectState | null
): { context: string; reason: string } {
  const refsPath = config.referencesPath;

  // No active project — minimal guide
  if (!project) {
    const guide = readRef(refsPath, "project-guide");
    // Only the first section (structure + yaml schema)
    const trimmed = guide.split("## Flusso operativo")[0]?.trim() ?? guide;
    return {
      context: `[Video Context — Nessun progetto attivo]\n\n${trimmed}`,
      reason: "no-project",
    };
  }

  const parts: string[] = [];
  let reason = "";

  // Always include project.yaml
  parts.push(`[Progetto attivo: ${project.slug}]\n${project.yaml}`);

  if (project.status === "draft") {
    // Storyboard phase — project guide
    parts.push(readRef(refsPath, "project-guide"));
    reason = "draft-storyboard";
  } else if (project.allDone && project.hasAudio) {
    // All scenes done, audio pending
    parts.push(readRef(refsPath, "audio-tools"));
    reason = "audio-pending";
  } else if (project.allDone) {
    // All scenes done, assembly phase
    parts.push(readRef(refsPath, "assembly"));
    reason = "assembly";
  } else if (project.status === "review") {
    // Review/assembly
    parts.push(readRef(refsPath, "assembly"));
    reason = "review";
  } else if (project.nextScene) {
    // Scene generation — inject relevant workflow section
    const sceneType = project.nextScene.type;
    // Exact match first, then try prefix match (handles LLM-invented suffixes like "image_edit_phr00t")
    let sectionNums = TYPE_TO_SECTIONS[sceneType];
    if (!sectionNums) {
      const prefix = Object.keys(TYPE_TO_SECTIONS).find((k) => sceneType.startsWith(k));
      if (prefix) {
        sectionNums = TYPE_TO_SECTIONS[prefix];
        console.log(`[video-context] Fuzzy match: "${sceneType}" → "${prefix}"`);
      } else {
        console.warn(
          `[video-context] Unknown scene type "${sceneType}" — injecting full comfyui reference`
        );
      }
    }
    const comfyFull = readRef(refsPath, "comfyui-workflows");
    const section = sectionNums
      ? extractWorkflowSections(comfyFull, sectionNums)
      : comfyFull; // unknown type: inject everything
    parts.push(section);
    reason = `scene-${project.nextScene.index}-${sceneType}`;

    // If scene needs audio (narration for lipsync), include audio ref too
    if (sceneType === "lipsync") {
      parts.push(readRef(refsPath, "audio-tools"));
    }
  }

  const context = parts.filter(Boolean).join("\n\n");
  return { context, reason };
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function log(logPath: string, entry: Record<string, unknown>): void {
  if (!logPath) return;
  try {
    appendFileSync(
      logPath,
      JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n"
    );
  } catch {
    // silent
  }
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

export function register(api: any): void {
  api.on(
    "before_prompt_build",
    async (
      event: { prompt: string },
      ctx: { agentId?: string; sessionKey?: string }
    ) => {
      const config: PluginConfig = api.getConfig?.() ?? {
        targetAgents: ["video-producer"],
        referencesPath: "./references",
        projectsBasePath:
          "/home/jarvis/.openclaw/workspace-video-producer/projects",
        maxContextTokens: 4000,
        logPath: "",
      };

      const agentId = ctx.agentId ?? "main";

      // Only inject for target agents
      if (!config.targetAgents.includes(agentId)) {
        return {};
      }

      const prompt = event.prompt ?? "";
      if (!prompt || prompt.length < 3) {
        return {};
      }

      // Parse project state
      const project = parseProjectState(config.projectsBasePath);

      // Build context based on state
      const { context, reason } = buildContext(config, project);

      if (!context) return {};

      const approxTokens = Math.ceil(context.length / CHARS_PER_TOKEN);

      // Enforce token budget
      let finalContext = context;
      const maxChars = config.maxContextTokens * CHARS_PER_TOKEN;
      if (context.length > maxChars) {
        finalContext = context.slice(0, Math.floor(maxChars));
        console.warn(
          `[video-context] Context truncated from ${context.length} to ${Math.floor(maxChars)} chars`
        );
      }

      log(config.logPath, {
        agent: agentId,
        project: project?.slug ?? null,
        reason,
        tokens: approxTokens,
      });

      console.log(
        `[video-context] ${reason}: ~${approxTokens} tokens for ${agentId}` +
          (project ? ` (project: ${project.slug})` : "")
      );

      return { prependContext: finalContext };
    }
  );
}
