/**
 * XTTS OpenAI-Compatible TTS Proxy
 * 
 * Translates OpenAI /v1/audio/speech API calls to XTTS /tts_to_audio/ calls.
 * Allows OpenClaw (which only speaks OpenAI TTS API) to use the local XTTS server.
 *
 * OpenAI format:  POST /v1/audio/speech { model, input, voice, response_format }
 * XTTS format:    POST /tts_to_audio/   { text, speaker_wav, language }
 *
 * Voice/speaker: uses XTTS_SPEAKER env var as default (e.g. "sample-bytedance"
 * references /app/speakers/sample-bytedance.mp3 in the XTTS container).
 * Can be overridden per-request via the "voice" field.
 */

const http = require("http");

const XTTS_URL = process.env.XTTS_URL || "http://100.88.84.81:8890";
const PORT = parseInt(process.env.PORT || "8891", 10);
const DEFAULT_LANGUAGE = process.env.DEFAULT_LANGUAGE || "it";
const DEFAULT_SPEAKER = process.env.XTTS_SPEAKER || "jarvis";

function mapVoice(voice) {
  if (!voice) return DEFAULT_SPEAKER;
  const trimmed = voice.trim();
  // "default" means use the configured default speaker
  if (trimmed.toLowerCase() === "default") return DEFAULT_SPEAKER;
  // Otherwise pass through as-is (allows OpenClaw config to pick any speaker)
  return trimmed;
}

const server = http.createServer(async (req, res) => {
  // Health check
  if (req.method === "GET" && (req.url === "/health" || req.url === "/")) {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", upstream: XTTS_URL, defaultSpeaker: DEFAULT_SPEAKER }));
    return;
  }

  // OpenAI TTS endpoint
  if (req.method === "POST" && req.url === "/v1/audio/speech") {
    let body = "";
    for await (const chunk of req) body += chunk;

    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    const text = parsed.input || parsed.text || "";
    const voice = mapVoice(parsed.voice);
    const language = req.headers["x-language"] || DEFAULT_LANGUAGE;

    if (!text.trim()) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "No text/input provided" }));
      return;
    }

    const xttsPayload = JSON.stringify({
      text,
      speaker_wav: voice,
      language,
    });

    try {
      const xttsRes = await fetch(`${XTTS_URL}/tts_to_audio/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: xttsPayload,
      });

      if (!xttsRes.ok) {
        const errText = await xttsRes.text();
        console.error(`[xtts-proxy] XTTS error ${xttsRes.status}: ${errText}`);
        res.writeHead(xttsRes.status, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: `XTTS error: ${xttsRes.status}`, details: errText }));
        return;
      }

      // Stream the audio response back
      const contentType = xttsRes.headers.get("content-type") || "audio/wav";
      res.writeHead(200, {
        "Content-Type": contentType,
        "Transfer-Encoding": "chunked",
      });

      const reader = xttsRes.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(value);
      }
      res.end();

      console.log(`[xtts-proxy] OK: speaker=${voice} lang=${language} text="${text.slice(0, 60)}..."`);
    } catch (err) {
      console.error(`[xtts-proxy] Fetch error:`, err.message);
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "XTTS upstream error", details: err.message }));
    }
    return;
  }

  // 404 for anything else
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found. Use POST /v1/audio/speech" }));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[xtts-proxy] Listening on 127.0.0.1:${PORT} → ${XTTS_URL} (speaker: ${DEFAULT_SPEAKER})`);
});
