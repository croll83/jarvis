#!/usr/bin/env python3
"""Aggiunge al comfyui-dashboard il pannello di switch modalita' GB10 (UI).

- badge di stato nell'header (modalita' + salute servizi), cliccabile
- banner con il toggle quando la pipeline creator e' offline
- disabilita i pulsanti .btn-generate quando ComfyUI non risponde

Idempotente. Va eseguito sul GB10.
"""
import sys

PATH = "/home/jarvis/comfyui-dashboard/static/index.html"
MARKER = "mode-badge"

CSS_ANCHOR = '.tabs{display:flex;gap:4px;margin-left:auto}'
CSS_ADD = CSS_ANCHOR + """
/* -- switch modalita GB10 (LLM <-> creator) -- */
.mode-badge{margin-left:12px;display:flex;align-items:center;gap:8px;padding:7px 12px;
  border-radius:8px;background:var(--surface2);border:1px solid var(--border);
  font-size:13px;font-weight:600;color:var(--text2);cursor:pointer;transition:.2s;white-space:nowrap}
.mode-badge:hover{border-color:var(--accent)}
.mode-badge[disabled]{cursor:wait;opacity:.7}
.mode-dot{width:8px;height:8px;border-radius:50%;background:var(--text2);flex:none}
.mode-badge.is-llm .mode-dot{background:var(--accent)}
.mode-badge.is-llm{color:var(--text)}
.mode-badge.is-creator .mode-dot{background:var(--success)}
.mode-badge.is-creator{color:var(--text)}
.mode-badge.is-mixed .mode-dot{background:#fbbf24}
.mode-badge.is-busy .mode-dot{background:#fbbf24;animation:mode-pulse 1s infinite}
@keyframes mode-pulse{0%,100%{opacity:1}50%{opacity:.25}}
.mode-banner{display:none;margin:0 0 16px;padding:14px 16px;border-radius:var(--radius);
  background:var(--surface);border:1px solid var(--border);border-left:3px solid #fbbf24;
  align-items:center;gap:14px;flex-wrap:wrap}
.mode-banner.show{display:flex}
.mode-banner .mb-text{flex:1;min-width:240px;font-size:14px;color:var(--text2);line-height:1.5}
.mode-banner .mb-text b{color:var(--text)}
.mode-banner button{padding:9px 16px;border-radius:8px;border:none;background:var(--accent);
  color:#fff;font-size:13px;font-weight:600;cursor:pointer;transition:.2s}
.mode-banner button:hover{background:var(--accent2)}
.mode-banner button[disabled]{opacity:.6;cursor:wait}
.btn-generate[disabled]{opacity:.45;cursor:not-allowed;filter:grayscale(.6)}"""

HDR_ANCHOR = """    <button class="tab" data-page="gallery">Galleria</button>
  </div>
</div>"""
HDR_ADD = """    <button class="tab" data-page="gallery">Galleria</button>
  </div>
  <button class="mode-badge" id="mode-badge" onclick="onModeBadgeClick()"
          title="Modalita' del GB10 — clicca per commutare">
    <span class="mode-dot"></span><span id="mode-label">...</span>
  </button>
</div>"""

CONT_ANCHOR = '<div class="container">'
CONT_ADD = """<div class="container">

<div class="mode-banner" id="mode-banner">
  <div class="mb-text" id="mode-banner-text"></div>
  <button id="mode-banner-btn" onclick="onModeBannerClick()"></button>
</div>"""

JS_ANCHOR = "</script>\n</body>"
JS_ADD = """
// ==================== Modalita GB10 (LLM <-> creator) ====================
// Il GB10 ha una pool di memoria unificata: vllm e la pipeline video non ci
// stanno insieme, quindi le modalita sono mutuamente esclusive. Lo stato viene
// dedotto dai servizi reali da /api/mode; lo switch e delegato a gb10-mode.

const MODE_LABELS = {llm: 'Modalita LLM', creator: 'Modalita Creator', mixed: 'Stato misto', idle: 'Tutto spento'};
let modeState = null;
let modePollTimer = null;

function creatorUsable(d) {
  return d && d.services && d.services.comfyui && d.services.comfyui.ready === true;
}

function renderMode(d) {
  const badge = document.getElementById('mode-badge');
  const label = document.getElementById('mode-label');
  const banner = document.getElementById('mode-banner');
  const bText = document.getElementById('mode-banner-text');
  const bBtn = document.getElementById('mode-banner-btn');
  if (!badge) return;

  const usable = creatorUsable(d);
  badge.className = 'mode-badge ' + (d.busy ? 'is-busy' : 'is-' + (d.mode || 'idle'));
  badge.disabled = !!d.busy;
  label.textContent = d.busy
    ? (d.phase || 'switching').replace(/^waiting-/, 'attendo ').replace(/-/g, ' ')
    : (MODE_LABELS[d.mode] || d.mode || '?');

  // pulsanti di generazione: senza ComfyUI non c'e nulla da generare
  document.querySelectorAll('.btn-generate').forEach(b => {
    b.disabled = !usable;
    b.title = usable ? '' : 'Pipeline creator offline: attiva la modalita Creator';
  });

  if (d.busy) {
    banner.classList.add('show');
    bText.innerHTML = '<b>Switch in corso…</b> ' + (d.message || d.phase || '') +
      '<br>Il caricamento di vllm richiede diversi minuti.';
    bBtn.textContent = 'attendere…';
    bBtn.disabled = true;
    return;
  }
  bBtn.disabled = false;

  if (!usable) {
    banner.classList.add('show');
    bText.innerHTML = '<b>Pipeline creator offline.</b> ComfyUI e AceStep sono spenti: ' +
      'il GB10 sta servendo il modello LLM (dark-jarvis). Generazione immagini, video e audio non disponibili.' +
      '<br><small>Attivando il creator, vllm viene spento: dark-jarvis diventa irraggiungibile ' +
      'per redteam e hermes finche non torni in modalita LLM.</small>';
    bBtn.textContent = 'Attiva Creator (spegne vllm)';
  } else {
    banner.classList.remove('show');
    bText.innerHTML = '';
    bBtn.textContent = 'Torna a LLM';
  }
}

async function refreshMode() {
  clearTimeout(modePollTimer);
  try {
    const resp = await fetch('/api/mode');
    const d = await resp.json();
    modeState = d;
    renderMode(d);
    modePollTimer = setTimeout(refreshMode, d.busy ? 4000 : 30000);
  } catch (e) {
    modePollTimer = setTimeout(refreshMode, 15000);
  }
}

async function doSwitch(target) {
  const warn = target === 'creator'
    ? 'Passare a modalita CREATOR spegne vllm.\\n\\ndark-jarvis diventera irraggiungibile ' +
      '(redteam e hermes inclusi) finche non torni a LLM.\\n\\nProcedere?'
    : 'Tornare a modalita LLM spegne ComfyUI e AceStep.\\n\\nIl caricamento di vllm richiede ' +
      'diversi minuti, durante i quali il modello non risponde.\\n\\nProcedere?';
  if (!confirm(warn)) return;
  try {
    const resp = await fetch('/api/mode/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: target}),
    });
    const d = await resp.json();
    if (!resp.ok) { alert('Switch rifiutato: ' + (d.detail || resp.status)); return; }
    if (d.accepted === false) { alert('Gia in modalita ' + target); }
  } catch (e) {
    alert('Errore nel richiedere lo switch: ' + e);
  }
  refreshMode();
}

function onModeBadgeClick() {
  if (!modeState || modeState.busy) return;
  // dal badge: commuta verso l'altra modalita
  doSwitch(creatorUsable(modeState) ? 'llm' : 'creator');
}

function onModeBannerClick() {
  if (!modeState || modeState.busy) return;
  doSwitch(creatorUsable(modeState) ? 'llm' : 'creator');
}

refreshMode();
</script>
</body>"""


def main() -> int:
    src = open(PATH).read()
    if MARKER in src:
        print("UI gia' patchata, nulla da fare")
        return 0

    out = src
    for name, anchor, repl in (
        ("css", CSS_ANCHOR, CSS_ADD),
        ("header", HDR_ANCHOR, HDR_ADD),
        ("container", CONT_ANCHOR, CONT_ADD),
        ("js", JS_ANCHOR, JS_ADD),
    ):
        n = out.count(anchor)
        if n != 1:
            print(f"ERRORE: anchor {name} trovato {n} volte, atteso 1", file=sys.stderr)
            return 1
        out = out.replace(anchor, repl, 1)

    # sanita' minima: tag bilanciati per quello che abbiamo aggiunto
    if out.count("mode-banner") < 3 or out.count("mode-badge") < 3:
        print("ERRORE: inserimento incompleto", file=sys.stderr)
        return 1

    open(PATH, "w").write(out)
    print("index.html patchato")
    return 0


if __name__ == "__main__":
    sys.exit(main())
