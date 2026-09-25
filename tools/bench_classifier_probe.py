#!/usr/bin/env python3
"""Misura la fattibilita' reale di un endpoint 'classificatore' sul GB10.

Tre domande, tre misure:
  1. quanto costa DAVVERO il prefill a 5000/6500 token, a freddo e a caldo
     (a freddo = prompt mai visto; a caldo = prefix cache attiva)
  2. il trick del first-token logprob funziona? quante alternative restituisce?
  3. cosa succede se una richiesta 'classify' arriva mentre una generation
     e' in corso, con max_num_seqs=2 e chunked prefill a 2048 token

Va eseguito sul GB10 (localhost:30000) per togliere la latenza di rete.
"""
import argparse
import json
import random
import string
import threading
import time
import urllib.request

URL = "http://127.0.0.1:30000"
MODEL = "dark-jarvis"

# Vocabolario ampio: prompt unici => prefill davvero a freddo
WORDS = [
    "ticket", "cliente", "fattura", "ordine", "spedizione", "reso", "garanzia",
    "assistenza", "pagamento", "rimborso", "contratto", "scadenza", "priorita",
    "urgente", "segnalazione", "impianto", "manutenzione", "sopralluogo",
    "preventivo", "collaudo", "anomalia", "ripristino", "intervento", "verifica",
]

OPTIONS = ["assistenza_tecnica", "amministrazione", "commerciale", "logistica"]


def post(path, body, timeout=300):
    req = urllib.request.Request(
        f"{URL}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data, time.perf_counter() - t0


def make_prompt(n_tokens, seed):
    """Prompt pseudo-realistico di ~n_tokens, unico per seed."""
    rng = random.Random(seed)
    # ~1.4 token per parola italiana: genero abbastanza parole
    body = " ".join(rng.choice(WORDS) + rng.choice(["", "", "e", "il"]) for _ in range(int(n_tokens / 1.4)))
    opts = "\n".join(f"{i+1}. {o}" for i, o in enumerate(OPTIONS))
    return (
        "Sei un classificatore. Leggi il contesto e scegli UNA categoria.\n\n"
        f"CONTESTO:\n{body}\n\n"
        f"CATEGORIE:\n{opts}\n\n"
        "Rispondi SOLO con il numero della categoria.\nRisposta:"
    )


def classify_once(prompt, n_logprobs=20):
    """Un forward: 1 token + logprobs. E' il 'readout' alla Jev su un LLM AR."""
    body = {
        "model": MODEL, "prompt": prompt,
        "max_tokens": 1, "temperature": 0.0,
        "logprobs": n_logprobs,
    }
    return post("/v1/completions", body)


def measure_prefill(sizes, repeats):
    print("\n=== 1. PREFILL: freddo vs caldo (max_tokens=1, latenza di rete esclusa) ===")
    print(f"{'token':>7} {'freddo p50':>12} {'caldo p50':>12} {'prefill tok/s':>15} {'verdetto 500ms':>16}")
    seed = int(time.time())
    for n in sizes:
        cold, warm = [], []
        for r in range(repeats):
            p = make_prompt(n, seed + r * 1000 + n)      # prompt unico => freddo
            d, t = classify_once(p)
            cold.append(t)
            real_tokens = d["usage"]["prompt_tokens"]
            _, t2 = classify_once(p)                      # stesso prompt => caldo
            warm.append(t2)
        cold.sort(); warm.sort()
        c50, w50 = cold[len(cold)//2], warm[len(warm)//2]
        ok = "SI" if c50 < 0.5 else f"NO ({c50/0.5:.1f}x sopra)"
        print(f"{real_tokens:>7} {c50*1000:>10.0f}ms {w50*1000:>10.0f}ms {real_tokens/c50:>15.0f} {ok:>16}")


def probe_logprobs():
    print("\n=== 2. READOUT: il trick first-token logprob funziona? ===")
    p = make_prompt(400, 42)
    try:
        d, t = classify_once(p, n_logprobs=20)
    except Exception as e:
        print(f"  ERRORE: {type(e).__name__}: {e}")
        return
    lp = d["choices"][0].get("logprobs")
    if not lp:
        print("  il campo logprobs NON e' tornato -> il trick non e' disponibile")
        return
    top = (lp.get("top_logprobs") or [{}])[0]
    print(f"  ok, {len(top)} alternative sul primo token ({t*1000:.0f}ms)")
    import math
    tot = sum(math.exp(v) for v in top.values())
    print(f"  massa di probabilita' coperta dalle top-{len(top)}: {tot*100:.1f}%")
    ranked = sorted(top.items(), key=lambda kv: -kv[1])[:6]
    for tok, v in ranked:
        print(f"    {tok!r:>12} p={math.exp(v):.4f}")
    # quante delle opzioni valide (1..4) compaiono davvero?
    valid = [t for t in top if t.strip() in {"1", "2", "3", "4"}]
    print(f"  token-opzione validi presenti: {len(valid)}/4 -> {valid}")
    if len(valid) < len(OPTIONS):
        print("  ATTENZIONE: non tutte le opzioni compaiono nelle top-k:")
        print("  la softmax sulle sole top-k e' una rinormalizzazione parziale,")
        print("  non la distribuzione vera sulle K opzioni.")


def measure_concurrency(n_tokens):
    print("\n=== 3. CONCORRENZA: classify mentre una generation e' in corso ===")
    print("    (max_num_seqs=2, chunked prefill 2048 token)")

    # baseline: classify da solo
    p_cls = make_prompt(n_tokens, 777)
    _, base = classify_once(p_cls)
    print(f"  baseline classify da solo:        {base*1000:>7.0f}ms")

    # generation lunga in background
    gen_done = threading.Event()
    gen_time = [None]

    def long_generation():
        body = {"model": MODEL, "prompt": "Scrivi un racconto lungo e dettagliato su un faro.",
                "max_tokens": 600, "temperature": 0.7}
        try:
            _, t = post("/v1/completions", body)
            gen_time[0] = t
        finally:
            gen_done.set()

    th = threading.Thread(target=long_generation, daemon=True)
    th.start()
    time.sleep(2.0)  # lascia che la generation entri in decode

    p_cls2 = make_prompt(n_tokens, 888)
    _, during = classify_once(p_cls2)
    gen_done.wait(timeout=180)

    print(f"  classify DURANTE una generation:  {during*1000:>7.0f}ms  "
          f"({during/base:.2f}x il baseline)")
    if gen_time[0]:
        print(f"  la generation ha impiegato:       {gen_time[0]*1000:>7.0f}ms")
    verdict = "trascurabile" if during < base * 1.25 else (
        "sensibile" if during < base * 2 else "GRAVE")
    print(f"  impatto della concorrenza: {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="*", default=[1000, 5000, 6500])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--skip-concurrency", action="store_true")
    a = ap.parse_args()

    print(f"target: {URL} modello {MODEL}")
    measure_prefill(a.sizes, a.repeats)
    probe_logprobs()
    if not a.skip_concurrency:
        measure_concurrency(max(a.sizes))


if __name__ == "__main__":
    main()
