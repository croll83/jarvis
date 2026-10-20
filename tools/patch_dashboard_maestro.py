#!/usr/bin/env python3
"""Aggiunge al comfyui-dashboard la terza modalita' GB10: maestro.

Presuppone gia' applicate patch_dashboard_mode.py e patch_dashboard_ui.py.

Backend (app.py):
- POST /api/mode/switch accetta mode=maestro e force=true
- 409 reason=maestro_busy se Maestro sta generando e force non e' dato:
  un render dura ore e spegnerlo lo butta via

UI (static/index.html):
- il badge apre/chiude un selettore con tre pulsanti (LLM / Creator / Maestro)
  invece di commutare alla cieca fra due modalita'
- in modalita' maestro il banner mostra il link a Maestro (:7860)
- se Maestro sta generando chiede una seconda conferma prima di forzare

Idempotente. Va eseguito sul GB10.
"""
import sys

DASH = "/home/jarvis/comfyui-dashboard"
APP = f"{DASH}/app.py"
HTML = f"{DASH}/static/index.html"
MARKER = "maestro_busy"


def replace_once(text: str, old: str, new: str, what: str) -> str:
    n = text.count(old)
    if n != 1:
        sys.exit(f"ABORT: ancora '{what}' trovata {n} volte (attesa 1), file diverso dal previsto")
    return text.replace(old, new)


def between(text: str, start: str, end: str, new: str, what: str) -> str:
    i = text.find(start)
    j = text.find(end, i)
    if i < 0 or j < 0 or text.count(start) != 1:
        sys.exit(f"ABORT: blocco '{what}' non trovato in modo univoco")
    return text[:i] + new + text[j:]


# ── app.py ──────────────────────────────────────────────────────────────────

APP_OLD_SWITCH = '''    target = (payload or {}).get("mode")
    if target not in ("llm", "creator"):
        raise HTTPException(400, "mode deve essere 'llm' o 'creator'")

    current = await _mode_status()
    if current.get("busy"):
        raise HTTPException(409, "uno switch e' gia' in corso")
    if current.get("mode") == target:
        return {"accepted": False, "reason": "already-in-mode", "mode": target}

    # start_new_session: lo switch deve sopravvivere alla richiesta HTTP.
    subprocess.Popen(
        [GB10_MODE_BIN, target],'''

APP_NEW_SWITCH = '''    target = (payload or {}).get("mode")
    if target not in ("llm", "creator", "maestro"):
        raise HTTPException(400, "mode deve essere 'llm', 'creator' o 'maestro'")
    force = bool((payload or {}).get("force"))

    current = await _mode_status()
    if current.get("busy"):
        raise HTTPException(409, "uno switch e' gia' in corso")
    if current.get("mode") == target:
        return {"accepted": False, "reason": "already-in-mode", "mode": target}

    # Un render di Maestro dura ore: spegnerlo mentre genera richiede una
    # conferma esplicita (force). gb10-mode fa lo stesso controllo, ma girando
    # detached il suo rifiuto non arriverebbe al browser. Passare A maestro
    # non lo spegne, quindi li' non serve.
    maestro = (current.get("services") or {}).get("maestro") or {}
    if target != "maestro" and maestro.get("busy") and not force:
        return JSONResponse(
            status_code=409,
            content={
                "reason": "maestro_busy",
                "detail": "Maestro sta generando: spegnerlo interrompe il render in corso.",
            },
        )

    # start_new_session: lo switch deve sopravvivere alla richiesta HTTP.
    subprocess.Popen(
        [GB10_MODE_BIN, target] + (["--force"] if force else []),'''

APP_OLD_OFFLINE = '''            "detail": "ComfyUI non risponde: il GB10 e' in modalita LLM. "
                      "Passa alla modalita creator per usare questa funzione.",'''
APP_NEW_OFFLINE = '''            "detail": "ComfyUI non risponde: il GB10 non e' in modalita creator. "
                      "Passa alla modalita creator per usare questa funzione.",'''

APP_OLD_DOC = '''    """ComfyUI spento non e' un errore del server: e' la modalita LLM attiva."""'''
APP_NEW_DOC = '''    """ComfyUI spento non e' un errore del server: e' attiva un'altra modalita."""'''

# ── index.html ──────────────────────────────────────────────────────────────

CSS_OLD = '''.mode-badge.is-creator{color:var(--text)}
'''
CSS_NEW = '''.mode-badge.is-creator{color:var(--text)}
.mode-badge.is-maestro .mode-dot{background:#a78bfa}
.mode-badge.is-maestro{color:var(--text)}
.mode-banner .mb-actions{display:flex;gap:8px;flex-wrap:wrap}
.mode-banner button.current{background:var(--surface2);color:var(--text2);cursor:default;opacity:1}
.mode-banner .mb-text a{color:var(--accent)}
'''

CSS_COMMENT_OLD = "/* -- switch modalita GB10 (LLM <-> creator) -- */"
CSS_COMMENT_NEW = "/* -- switch modalita GB10 (LLM / creator / maestro) -- */"

BADGE_OLD = '''title="Modalita' del GB10 — clicca per commutare">'''
BADGE_NEW = '''title="Modalita' del GB10 — clicca per cambiarla">'''

BANNER_OLD = '''  <button id="mode-banner-btn" onclick="onModeBannerClick()"></button>
'''
BANNER_NEW = '''  <div class="mb-actions">
    <button data-target="llm" onclick="doSwitch('llm')">LLM</button>
    <button data-target="creator" onclick="doSwitch('creator')">Creator</button>
    <button data-target="maestro" onclick="doSwitch('maestro')">Maestro</button>
  </div>
'''

JS_START = "// ==================== Modalita GB10 (LLM <-> creator) ===================="
JS_END = "refreshMode();\n</script>"
JS_NEW = r"""// ==================== Modalita GB10 (LLM / creator / maestro) ====================
// Il GB10 ha una pool di memoria unificata: vllm, la pipeline ComfyUI e Maestro
// non ci stanno insieme, quindi le modalita sono mutuamente esclusive. Lo stato
// viene dedotto dai servizi reali da /api/mode; lo switch e delegato a gb10-mode.

const MODE_LABELS = {llm: 'Modalita LLM', creator: 'Modalita Creator', maestro: 'Modalita Maestro',
                     mixed: 'Stato misto', idle: 'Tutto spento'};
const MODE_NAMES = {llm: 'LLM', creator: 'Creator', maestro: 'Maestro'};
// stesso host della dashboard: images.mintwork.it risolve all'IP Tailscale del GB10
const MAESTRO_URL = 'http://' + location.hostname + ':7860/';
const SWITCH_WARN = {
  llm: 'Passare a modalita LLM spegne ComfyUI, AceStep e Maestro.\n\nIl caricamento di vllm ' +
       'richiede circa 10 minuti, durante i quali il modello non risponde.',
  creator: 'Passare a modalita CREATOR spegne vllm e Maestro.\n\ndark-jarvis diventera ' +
           'irraggiungibile (redteam e hermes inclusi) finche non torni a LLM.',
  maestro: 'Passare a modalita MAESTRO spegne vllm, ComfyUI e AceStep.\n\ndark-jarvis diventera ' +
           'irraggiungibile (redteam e hermes inclusi) finche non torni a LLM.',
};
let modeState = null;
let modePollTimer = null;
let modePickerOpen = false;

function creatorUsable(d) {
  return d && d.services && d.services.comfyui && d.services.comfyui.ready === true;
}

function modeDescription(d) {
  const m = (d.services && d.services.maestro) || {};
  switch (d.mode) {
    case 'llm':
      return '<b>Modalita LLM.</b> Il GB10 serve il modello LLM (dark-jarvis) a redteam e hermes. ' +
             'ComfyUI, AceStep e Maestro sono spenti.';
    case 'creator':
      return '<b>Modalita Creator.</b> ComfyUI e AceStep sono attivi: generazione immagini, ' +
             'video e audio disponibile da questa dashboard.';
    case 'maestro':
      return '<b>Modalita Maestro.</b> Maestro e attivo su <a href="' + MAESTRO_URL +
             '" target="_blank" rel="noopener">' + MAESTRO_URL + '</a>' +
             (m.busy ? ' e <b>sta generando</b>' : '') + '.';
    case 'mixed':
      return '<b>Stato misto.</b> Sono attivi pezzi di piu modalita insieme: scegline una ' +
             'per riportare il GB10 in uno stato pulito.';
    default:
      return '<b>Tutto spento.</b> Nessuna pipeline pesante attiva: scegli una modalita.';
  }
}

function renderMode(d) {
  const badge = document.getElementById('mode-badge');
  const label = document.getElementById('mode-label');
  const banner = document.getElementById('mode-banner');
  const bText = document.getElementById('mode-banner-text');
  const buttons = banner ? banner.querySelectorAll('.mb-actions button') : [];
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
      '<br>Puo richiedere diversi minuti (vllm circa 10).';
    buttons.forEach(b => { b.disabled = true; b.classList.remove('current'); });
    return;
  }

  buttons.forEach(b => {
    const t = b.dataset.target;
    const cur = t === d.mode;
    b.classList.toggle('current', cur);
    b.disabled = cur;
    b.textContent = cur ? MODE_NAMES[t] + ' (attiva)' : 'Passa a ' + MODE_NAMES[t];
  });
  const last = d.last_switch;
  const failed = !!(last && last.phase === 'failed');
  // visibile quando da qui non si puo generare, se l'ultimo switch e fallito,
  // oppure se aperto dal badge
  banner.classList.toggle('show', !usable || failed || modePickerOpen);
  bText.innerHTML = modeDescription(d) + (usable ? '' :
    '<br><small>La generazione da questa dashboard richiede la modalita Creator.</small>') +
    (failed ? '<br><small style="color:#f87171">Ultimo switch fallito: ' + (last.message || '') +
              '</small>' : '');
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

async function doSwitch(target, force) {
  if (!modeState || modeState.busy) return;
  if (!force && !confirm(SWITCH_WARN[target] + '\n\nProcedere?')) return;
  try {
    const resp = await fetch('/api/mode/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: target, force: !!force}),
    });
    const d = await resp.json();
    if (resp.status === 409 && d.reason === 'maestro_busy') {
      if (confirm('Maestro sta generando: spegnerlo ora interrompe il render in corso, ' +
                  'che va perso.\n\nInterrompere e passare comunque a ' + MODE_NAMES[target] + '?')) {
        return doSwitch(target, true);
      }
      return;
    }
    if (!resp.ok) { alert('Switch rifiutato: ' + (d.detail || resp.status)); return; }
    if (d.accepted === false) { alert('Gia in modalita ' + target); }
    modePickerOpen = false;
  } catch (e) {
    alert('Errore nel richiedere lo switch: ' + e);
  }
  // gb10-mode parte detached: dargli il tempo di prendere il lock prima di leggere lo stato
  clearTimeout(modePollTimer);
  modePollTimer = setTimeout(refreshMode, 2000);
}

function onModeBadgeClick() {
  if (!modeState || modeState.busy) return;
  modePickerOpen = !modePickerOpen;
  renderMode(modeState);
}

"""


def main() -> None:
    app = open(APP, encoding="utf-8").read()
    html = open(HTML, encoding="utf-8").read()
    if MARKER in app and "mb-actions" in html:
        print("gia' applicata, niente da fare")
        return

    app = replace_once(app, APP_OLD_SWITCH, APP_NEW_SWITCH, "switch_mode")
    app = replace_once(app, APP_OLD_OFFLINE, APP_NEW_OFFLINE, "creator_offline detail")
    app = replace_once(app, APP_OLD_DOC, APP_NEW_DOC, "creator_offline docstring")

    html = replace_once(html, CSS_OLD, CSS_NEW, "css is-creator")
    html = replace_once(html, CSS_COMMENT_OLD, CSS_COMMENT_NEW, "css comment")
    html = replace_once(html, BADGE_OLD, BADGE_NEW, "badge title")
    html = replace_once(html, BANNER_OLD, BANNER_NEW, "banner button")
    html = between(html, JS_START, JS_END, JS_NEW, "js modalita")

    # si scrive solo se tutte le sostituzioni sono riuscite
    open(APP, "w", encoding="utf-8").write(app)
    open(HTML, "w", encoding="utf-8").write(html)
    print("patch applicata")


if __name__ == "__main__":
    main()
