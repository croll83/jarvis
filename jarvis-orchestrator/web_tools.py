"""
JARVIS Web Tools — Tool calling per Qwen 2.5 3B via Ollama

4 tool disponibili:
  1. web_search  — Cerca sul web via Brave Search API
  2. web_fetch   — Scarica e leggi il contenuto di una pagina web
  3. memory_search — Cerca nella memoria semantica (conversazioni + fatti)
  4. home_status — Stato dei dispositivi domotici da Home Assistant

Usato da:
  - get_quick_response() quando AI Agent e' down (fallback locale)
  - Cronjob autonomi che girano su Qwen locale
  - Qualsiasi flusso che necessiti di tool calling locale
"""

import re
import json
import logging
import aiohttp
from typing import List, Dict, Any, Optional

import config

logger = logging.getLogger("JARVIS_TOOLS")


# ===========================================================================
# TOOL DEFINITIONS (formato Ollama /api/chat tools)
# ===========================================================================

TOOL_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Cerca informazioni sul web. Usa questo strumento quando hai bisogno di "
            "informazioni aggiornate, notizie, prezzi, meteo, eventi, orari, "
            "o qualsiasi dato che non hai in memoria."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "La query di ricerca (in italiano o inglese)"
                }
            },
            "required": ["query"]
        }
    }
}

TOOL_WEB_FETCH = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Scarica e leggi il contenuto testuale di una pagina web. "
            "Usa questo strumento per leggere articoli, pagine di dettaglio, "
            "risultati di ricerca, o qualsiasi URL specifico."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "L'URL completo della pagina da leggere"
                }
            },
            "required": ["url"]
        }
    }
}

TOOL_MEMORY_SEARCH = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "Cerca nella memoria delle conversazioni passate e nei fatti noti "
            "sull'utente. Usa questo strumento per ricordare preferenze, "
            "conversazioni precedenti, o informazioni personali dell'utente."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Cosa cercare nella memoria (es. 'preferenze musicali', 'ultima conversazione sulla spesa')"
                }
            },
            "required": ["query"]
        }
    }
}

TOOL_HOME_STATUS = {
    "type": "function",
    "function": {
        "name": "home_status",
        "description": (
            "Ottieni lo stato attuale dei dispositivi domotici della casa. "
            "Usa questo strumento per sapere quali luci sono accese, "
            "la temperatura dei sensori, lo stato delle tapparelle, ecc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device_type": {
                    "type": "string",
                    "description": "Tipo di dispositivo da controllare: 'light', 'sensor', 'switch', 'cover', 'climate', 'all'. Default: 'all'"
                }
            },
            "required": []
        }
    }
}

# Lista completa dei tool disponibili
ALL_TOOLS = [TOOL_WEB_SEARCH, TOOL_WEB_FETCH, TOOL_MEMORY_SEARCH, TOOL_HOME_STATUS]


# ===========================================================================
# TOOL IMPLEMENTATIONS
# ===========================================================================

async def execute_web_search(query: str, count: int = 5) -> str:
    """Cerca sul web via Brave Search API."""
    if not config.BRAVE_API_KEY:
        return "Errore: Brave Search API key non configurata."

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": config.BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": count,
        "search_lang": "it",
        "ui_lang": "it-IT",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                config.BRAVE_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Brave Search error {resp.status}: {error_text[:200]}")
                    return f"Errore ricerca web: HTTP {resp.status}"

                data = await resp.json()

        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            title = item.get("title", "")
            url = item.get("url", "")
            description = item.get("description", "")
            results.append(f"- {title}\n  URL: {url}\n  {description}")

        if results:
            return f"Risultati per '{query}':\n\n" + "\n\n".join(results)
        return f"Nessun risultato trovato per '{query}'."

    except asyncio.TimeoutError:
        logger.warning(f"Brave Search timeout for query: {query}")
        return "Errore: timeout nella ricerca web."
    except Exception as e:
        logger.error(f"Brave Search exception: {e}")
        return f"Errore ricerca web: {e}"


async def execute_web_fetch(url: str, max_chars: int = 5000) -> str:
    """Scarica e leggi il contenuto testuale di una pagina web."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0; +https://jarvis.local)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return f"Errore: la pagina ha risposto con HTTP {resp.status}"

                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return f"Errore: contenuto non testuale ({content_type})"

                html = await resp.text(errors="replace")

        # Estrai testo da HTML (semplice, senza dipendenze esterne)
        text = _html_to_text(html)

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... troncato a {max_chars} caratteri]"

        if not text.strip():
            return "La pagina non contiene testo leggibile."

        return f"Contenuto di {url}:\n\n{text}"

    except asyncio.TimeoutError:
        return f"Errore: timeout nel caricamento di {url}"
    except Exception as e:
        logger.error(f"Web fetch exception for {url}: {e}")
        return f"Errore nel caricamento della pagina: {e}"


def _html_to_text(html: str) -> str:
    """Estrai testo pulito da HTML (senza BeautifulSoup)."""
    # Rimuovi script e style
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<noscript[^>]*>.*?</noscript>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Converti br e p in newline
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
    # Rimuovi tutti i tag rimanenti
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Pulisci whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    return text.strip()


async def execute_memory_search(query: str, user_id: int = None) -> str:
    """Cerca nella memoria semantica via mem0-stack (MEM0_BASE_URL/search)."""
    try:
        import aiohttp
        import config

        if user_id is None:
            user_id = 1  # Default user

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.MEM0_BASE_URL}/search",
                json={"query": query, "user_id": str(user_id), "limit": 10},
                timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS.get("mem0", 10))
            ) as resp:
                if resp.status != 200:
                    logger.error(f"mem0 search error: {resp.status}")
                    return f"Errore nella ricerca in memoria (HTTP {resp.status})."
                data = await resp.json()

        items = data.get("results") or data.get("memories") or []
        if not items:
            return f"Nessun ricordo trovato per '{query}'."

        output_parts = ["Ricordi rilevanti:"]
        for item in items[:10]:
            text = item.get("memory") or item.get("content") or item.get("text", "")
            score = item.get("score") or item.get("similarity") or 0
            output_parts.append(f"  - {text} (rilevanza: {score:.2f})")
        return "\n".join(output_parts)

    except Exception as e:
        logger.error(f"Memory search error: {e}")
        return f"Errore nella ricerca in memoria: {e}"


async def execute_home_status(device_type: str = "all", location_id: str = None) -> str:
    """Ottieni lo stato dei dispositivi domotici da Home Assistant."""
    try:
        from multi_ha import ha_manager
        from database import get_all_locations

        # Determina location
        if not location_id:
            locations = get_all_locations(enabled_only=True)
            if not locations:
                return "Nessuna location configurata."
            location_id = locations[0].id

        # Fetch tutti gli stati
        states = await ha_manager.get_states_bulk(location_id)
        if not states:
            return f"Impossibile ottenere stati da Home Assistant per location '{location_id}'."

        # Filtra per tipo
        type_prefixes = {
            "light": ["light."],
            "sensor": ["sensor."],
            "switch": ["switch."],
            "cover": ["cover."],
            "climate": ["climate."],
            "all": ["light.", "sensor.", "switch.", "cover.", "climate.", "binary_sensor."],
        }
        prefixes = type_prefixes.get(device_type, type_prefixes["all"])

        filtered = {}
        for entity_id, state_data in states.items():
            if any(entity_id.startswith(p) for p in prefixes):
                filtered[entity_id] = state_data

        if not filtered:
            return f"Nessun dispositivo di tipo '{device_type}' trovato."

        # Formatta output
        output_parts = [f"Stato dispositivi ({device_type}) - {len(filtered)} entita':"]
        for entity_id, state_data in sorted(filtered.items()):
            state = state_data.get("state", "unknown")
            attrs = state_data.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            detail = f"  - {friendly_name}: {state}"
            # Aggiungi dettagli rilevanti
            if "temperature" in attrs:
                detail += f" ({attrs['temperature']}°)"
            if "brightness" in attrs:
                detail += f" (brightness: {attrs['brightness']})"
            if "current_temperature" in attrs:
                detail += f" (attuale: {attrs['current_temperature']}°)"

            output_parts.append(detail)

        return "\n".join(output_parts)

    except Exception as e:
        logger.error(f"Home status error: {e}")
        return f"Errore nel recupero stato casa: {e}"


# ===========================================================================
# TOOL EXECUTION DISPATCHER
# ===========================================================================

async def execute_tool(
    tool_name: str,
    arguments: dict,
    user_id: int = None,
    location_id: str = None
) -> str:
    """Esegui un tool e restituisci il risultato come stringa."""
    logger.info(f"Executing tool: {tool_name} with args: {arguments}")

    try:
        if tool_name == "web_search":
            return await execute_web_search(
                query=arguments.get("query", ""),
                count=arguments.get("count", 5)
            )
        elif tool_name == "web_fetch":
            return await execute_web_fetch(
                url=arguments.get("url", ""),
                max_chars=arguments.get("max_chars", 5000)
            )
        elif tool_name == "memory_search":
            return await execute_memory_search(
                query=arguments.get("query", ""),
                user_id=user_id
            )
        elif tool_name == "home_status":
            return await execute_home_status(
                device_type=arguments.get("device_type", "all"),
                location_id=location_id
            )
        else:
            return f"Tool sconosciuto: {tool_name}"
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return f"Errore nell'esecuzione di {tool_name}: {e}"


# ===========================================================================
# TOOL CALLING LOOP (Ollama /api/chat con tools)
# ===========================================================================

async def call_qwen_with_tools(
    messages: List[Dict[str, Any]],
    tools: List[dict] = None,
    max_iterations: int = 3,
    user_id: int = None,
    location_id: str = None,
    temperature: float = 0.3,
    max_tokens: int = 1000,
) -> str:
    """
    Chiama Qwen 2.5 3B con tool support via Ollama /api/chat.
    Gestisce il loop: request → tool_calls → execute → send results → repeat.

    Args:
        messages: Lista messaggi (system + user)
        tools: Lista tool definitions (default: ALL_TOOLS)
        max_iterations: Max cicli tool call (evita loop infiniti)
        user_id: ID utente per memory_search
        location_id: Location per home_status
        temperature: Temperatura generazione
        max_tokens: Max token output

    Returns:
        Risposta finale del modello (stringa)
    """
    if tools is None:
        tools = ALL_TOOLS

    # Copia messages per non mutare l'originale
    msgs = list(messages)

    for iteration in range(max_iterations):
        if config.ROUTER_ENGINE == "llamacpp":
            # llama-server: OpenAI-compatible format
            payload = {
                "model": config.ROUTER_MODEL,
                "messages": msgs,
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            url = f"{config.ROUTER_URL}/v1/chat/completions"
        else:
            # Ollama format
            payload = {
                "model": config.ROUTER_MODEL,
                "messages": msgs,
                "tools": tools,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                    "num_ctx": config.OLLAMA_NUM_CTX,
                },
                "stream": False,
            }
            url = config.OLLAMA_CHAT_URL

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        logger.error(f"LLM tool call error HTTP {resp.status}: {error[:200]}")
                        return "Mi dispiace, c'e' stato un problema con la risposta."

                    data = await resp.json()

        except asyncio.TimeoutError:
            logger.error(f"LLM tool call timeout (iteration {iteration})")
            return "Mi dispiace, la risposta ha impiegato troppo tempo."
        except Exception as e:
            logger.error(f"LLM tool call exception: {e}")
            return "Mi dispiace, c'e' stato un errore."

        if config.ROUTER_ENGINE == "llamacpp":
            choice = data.get("choices", [{}])[0].get("message", {})
            content = choice.get("content", "") or ""
            tool_calls = choice.get("tool_calls", [])
            # Normalize OpenAI tool_calls to Ollama format for downstream
            message = {"role": "assistant", "content": content}
            if tool_calls:
                message["tool_calls"] = [
                    {"function": {"name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]
                                  if isinstance(tc["function"]["arguments"], dict)
                                  else json.loads(tc["function"]["arguments"])}}
                    for tc in tool_calls
                ]
                tool_calls = message["tool_calls"]
        else:
            message = data.get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

        # Se non ci sono tool calls, abbiamo la risposta finale
        if not tool_calls:
            logger.info(f"Tool calling completed after {iteration} tool iterations")
            return content.strip() if content else "Non ho una risposta."

        # Aggiungi il messaggio dell'assistente (con tool_calls) alla storia
        msgs.append(message)

        # Esegui ogni tool call
        for tc in tool_calls:
            func = tc.get("function", {})
            func_name = func.get("name", "")
            func_args = func.get("arguments", {})

            # Ollama puo' restituire arguments come stringa JSON
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except json.JSONDecodeError:
                    func_args = {}

            # Esegui il tool
            result = await execute_tool(
                tool_name=func_name,
                arguments=func_args,
                user_id=user_id,
                location_id=location_id
            )

            # Aggiungi il risultato come messaggio "tool"
            msgs.append({
                "role": "tool",
                "content": result,
            })

            logger.info(f"Tool {func_name} result: {result[:200]}...")

    # Max iterations raggiunte
    logger.warning(f"Tool calling hit max iterations ({max_iterations})")
    # Prova a restituire l'ultimo contenuto, altrimenti messaggio generico
    if content:
        return content.strip()
    return "Ho cercato di trovare le informazioni ma ho raggiunto il limite di ricerche. Puoi riformulare la domanda?"


# Import asyncio per i timeout
import asyncio
