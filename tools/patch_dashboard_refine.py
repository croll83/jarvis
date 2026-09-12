#!/usr/bin/env python3
"""Sposta il prompt refinement della dashboard su Claude Sonnet 5 via billing proxy.

Prima: Gemini (se GEMINI_API_KEY) altrimenti llama.cpp locale su :30000 col
modello "dark-opus" — che pero' non esiste piu' (vllm serve "dark-jarvis"), e
in modalita' creator vllm e' spento comunque.
Dopo: Sonnet 5 attraverso hermes-billing-proxy, che funziona in entrambe le
modalita' perche' e' fuori dal GB10.

Idempotente. Va eseguito sul GB10.
"""
import ast
import re
import sys

PATH = "/home/jarvis/comfyui-dashboard/app.py"
MARKER = "REFINE_MODEL"

OLD_CONSTS = '''# Prompt refinement: uses Gemini if GEMINI_API_KEY is set, otherwise local llama.cpp
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LLAMA_URL = "http://127.0.0.1:30000/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"'''

NEW_CONSTS = '''# Prompt refinement: Claude Sonnet 5 attraverso hermes-billing-proxy.
# Il proxy fa token swap (sostituisce l'auth con l'OAuth di Claude Code), quindi
# la api_key passata qui e' un placeholder, non un segreto. Gira fuori dal GB10,
# percio' il refinement funziona anche in modalita' creator con vllm spento.
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://100.116.99.9:18801")
REFINE_MODEL = os.environ.get("REFINE_MODEL", "claude-sonnet-5")
REFINE_MAX_TOKENS = 1024'''

NEW_FUNC = '''_refine_client_singleton: anthropic.AsyncAnthropic | None = None


def _refine_client() -> anthropic.AsyncAnthropic:
    """Client riusato tra le richieste (tiene aperto il pool di connessioni)."""
    global _refine_client_singleton
    if _refine_client_singleton is None:
        _refine_client_singleton = anthropic.AsyncAnthropic(
            base_url=ANTHROPIC_BASE_URL,
            api_key=os.environ.get("ANTHROPIC_API_KEY", "placeholder-swapped-by-proxy"),
            timeout=60.0,
        )
    return _refine_client_singleton


async def refine_prompt(prompt: str) -> str:
    """Riscrive il prompt utente in inglese descrittivo con Sonnet 5.

    Su qualunque errore restituisce il prompt originale: il refinement e' un
    miglioramento opzionale e non deve mai impedire una generazione.
    """
    try:
        msg = await _refine_client().messages.create(
            model=REFINE_MODEL,
            max_tokens=REFINE_MAX_TOKENS,
            system=REFINE_SYSTEM,
            # Riscrittura breve e interattiva: ragionare aggiungerebbe solo
            # latenza. "disabled" e' accettato su Sonnet 5.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return text or prompt
    except anthropic.APIStatusError as e:
        print(f"[refine_prompt] errore API {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        print(f"[refine_prompt] billing proxy non raggiungibile: {e}")
    except Exception as e:  # non far mai fallire la generazione per il refine
        print(f"[refine_prompt] fallito: {e}")
    return prompt


'''


def main() -> int:
    src = open(PATH).read()
    if MARKER in src:
        print("gia' patchato, nulla da fare")
        return 0

    out = src

    # 1. import dell'SDK ufficiale (terze parti, prima di httpx)
    if "import anthropic" not in out:
        if "\nimport httpx\n" not in out:
            print("ERRORE: anchor 'import httpx' non trovato", file=sys.stderr)
            return 1
        out = out.replace("\nimport httpx\n", "\nimport anthropic\nimport httpx\n", 1)

    # 2. costanti
    if OLD_CONSTS not in out:
        print("ERRORE: blocco costanti Gemini/LLAMA non trovato", file=sys.stderr)
        return 1
    out = out.replace(OLD_CONSTS, NEW_CONSTS, 1)

    # 3. sostituisci l'intera refine_prompt (dalla def fino a 'app = FastAPI(')
    m = re.search(
        r"async def refine_prompt\(prompt: str\) -> str:.*?\n(?=app = FastAPI\()",
        out, re.DOTALL,
    )
    if not m:
        print("ERRORE: corpo di refine_prompt non individuato", file=sys.stderr)
        return 1
    out = out[:m.start()] + NEW_FUNC + out[m.end():]

    # 4. i simboli rimossi non devono essere usati altrove
    for dead in ("GEMINI_API_KEY", "GEMINI_URL", "LLAMA_URL"):
        if dead in out:
            hits = [l for l in out.splitlines() if dead in l]
            print(f"ERRORE: {dead} ancora usato: {hits[:3]}", file=sys.stderr)
            return 1

    try:
        ast.parse(out)
    except SyntaxError as e:
        print(f"ERRORE: il risultato non compila: {e}", file=sys.stderr)
        return 1

    open(PATH, "w").write(out)
    print("refine_prompt spostato su Sonnet 5 via billing proxy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
