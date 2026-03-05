#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# JARVIS - Setup Script
# Esegui dopo docker-compose up
# ═══════════════════════════════════════════════════════════════════════════════

set -e

echo "JARVIS Setup Script"
echo "==============================================================================="

# Attendi che Ollama sia pronto
echo "Waiting for Ollama to be ready..."
until curl -s http://localhost:11434/api/tags > /dev/null 2>&1; do
    sleep 2
done
echo "Ollama is ready"

# Download modelli
echo ""
echo "Downloading Qwen3.5 4B (Router + Pre-routing model)..."
docker exec jarvis_ollama ollama pull qwen3.5:4b

echo ""
echo "Models downloaded"
echo "(Embeddings handled by fastembed container — no nomic-embed-text on Ollama)"

# Warmup models
echo ""
echo "Warming up models..."
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3.5:4b",
  "prompt": "test",
  "options": {"num_predict": 1}
}' > /dev/null

echo "Models warmed up"

# Verifica
echo ""
echo "==============================================================================="
echo "Installed models:"
docker exec jarvis_ollama ollama list

echo ""
echo "==============================================================================="
echo "JARVIS Setup Complete!"
echo ""
echo "Next steps:"
echo "  1. Configure .env with your tokens"
echo "  2. Configure OpenClaw (Gemini API key + Telegram bot)"
echo "  3. Configure Home Assistant integration"
echo "  4. Setup AtomS3R microphones"
echo "  5. Open admin UI to enroll voices: http://localhost:5000/admin"
echo "  6. Add family members and record their voices"
echo ""
echo "Services:"
echo "  - Orchestrator + Admin UI: http://localhost:5000"
echo "  - OpenClaw (Gemini):       http://localhost:18789"
echo "  - Ollama API:              http://localhost:11434"
echo ""
