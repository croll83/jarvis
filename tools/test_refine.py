#!/usr/bin/env python3
"""Test del prompt refinement della dashboard (Sonnet 5 via billing proxy).

Verifica il percorso felice e il degrado quando il proxy non risponde.
Va eseguito sul GB10 col python del venv, da /home/jarvis/comfyui-dashboard.
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/jarvis/comfyui-dashboard")


async def main() -> int:
    import app

    print(f"base_url = {app.ANTHROPIC_BASE_URL}")
    print(f"model    = {app.REFINE_MODEL}")

    original = "un gatto rosso su una motocicletta al tramonto"
    print(f"\n--- percorso felice ---\ninput:  {original}")
    out = await app.refine_prompt(original)
    ok = out != original and len(out) > len(original)
    print(f"output: {out[:400]}")
    print(f"=> refinement {'OK' if ok else 'NON avvenuto (ha restituito l originale)'}")

    # Degrado: proxy inesistente -> deve restituire il prompt originale, non alzare
    print("\n--- degrado con proxy irraggiungibile ---")
    app._refine_client_singleton = None
    app.ANTHROPIC_BASE_URL = "http://127.0.0.1:9  # porta morta".split("#")[0].strip()
    try:
        out2 = await app.refine_prompt(original)
        degraded_ok = out2 == original
        print(f"output: {out2}")
        print(f"=> degrado {'OK (prompt originale)' if degraded_ok else 'SBAGLIATO'}")
    except Exception as e:
        print(f"=> FALLITO: ha propagato l eccezione {type(e).__name__}: {e}")
        degraded_ok = False

    return 0 if (ok and degraded_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
