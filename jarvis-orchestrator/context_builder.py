"""
JARVIS Context Builder
- SQLite (strutturato) per memoria utente strutturata
- Memoria semantica/procedurale tramite mem0-stack (MEM0_BASE_URL) lato consumer
- Budget token gestito per routing vs reasoning
"""

import time
import logging
from typing import Dict, Any, Optional, List

from database import (
    get_user_memory_context,
    format_user_memory_for_llm,
    get_location,
    get_user_location
)
from location_memory import (
    get_all_locations_memory,
    format_locations_memory_for_llm
)
import config

logger = logging.getLogger("JARVIS_CONTEXT")


# ===========================================================================
# MAIN CONTEXT BUILDER
# ===========================================================================

async def build_full_context(
    user_id: Optional[int],
    user_name: str,
    query: str,
    target_location_id: Optional[str] = None,
    context_type: str = "reasoning"
) -> str:
    """
    Costruisce il contesto completo per una query.
    VERSIONE IBRIDA: SQLite + Vector Search.

    Args:
        user_id: ID utente
        user_name: Nome utente
        query: Query utente (per vector search)
        target_location_id: Location specifica (None = tutte)
        context_type: "routing" o "reasoning"

    Returns:
        Stringa di contesto formattata
    """
    budget = config.CONTEXT_BUDGETS.get(context_type, config.CONTEXT_BUDGETS["reasoning"])
    parts = []
    _t0 = time.time()

    # ===== 1. USER MEMORY - SQL HOT only (chat_memory raw recente) =====
    # Layer WARM/COLD/LONGTERM rimossi: long-term semantic facts vivono in mem0-stack.
    _t = time.time()
    if user_id:
        # Almeno 5 turni (10 messaggi) anche per routing; reasoning ne prende di piu'.
        max_messages = 10 if context_type == "routing" else 20
        user_memory_sql = get_user_memory_context(
            user_id=user_id,
            max_messages=max_messages,
        )

        sql_context = format_user_memory_for_llm(
            user_memory_sql,
            user_name,
            max_tokens=budget["user_memory_sql"]
        )

        if sql_context.strip():
            parts.append(sql_context)
    _sql_user_ms = (time.time() - _t) * 1000

    # NOTE: Semantica utente (messaggi/fatti) ora gestita da mem0-stack via
    # consumer plugin (hermes-plugin/mem0-selfhosted) e dal tool LLM
    # memory_search → MEM0_BASE_URL/search. Nessuna chiamata in-process qui.

    # ===== 3. LOCATION MEMORY - SQL (structured) =====
    _t = time.time()
    if target_location_id:
        location_ids = [target_location_id]
    else:
        if user_id:
            user_loc = get_user_location(user_id)
            location_ids = [user_loc.location_id] if user_loc else None
        else:
            location_ids = None

    try:
        locations_memory = await get_all_locations_memory(location_ids)
        if locations_memory:
            location_context = format_locations_memory_for_llm(
                locations_memory,
                max_tokens_per_location=budget["location_memory"] // max(len(locations_memory), 1)
            )
            if location_context.strip():
                parts.append(location_context)
    except Exception as e:
        logger.warning(f"Location memory fetch failed: {e}")
    _loc_sql_ms = (time.time() - _t) * 1000

    # NOTE: Semantica eventi-location era servita da ha_memory_service vector
    # store ora rimosso. La memoria utente cross-location e' gestita da
    # mem0-stack (semantic + graph + RB). Nessun fallback locale qui.

    _total_ms = (time.time() - _t0) * 1000
    logger.info(
        f"build_full_context timing ({context_type}): total={_total_ms:.0f}ms | "
        f"sql_user={_sql_user_ms:.0f}ms loc_sql={_loc_sql_ms:.0f}ms"
    )

    return "\n\n".join(parts)


async def build_routing_context(
    user_id: Optional[int],
    user_name: str,
    query: str,
    target_location_id: Optional[str] = None
) -> str:
    """Shortcut per contesto routing (budget ridotto)."""
    return await build_full_context(
        user_id=user_id,
        user_name=user_name,
        query=query,
        target_location_id=target_location_id,
        context_type="routing"
    )


async def build_reasoning_context(
    user_id: Optional[int],
    user_name: str,
    query: str,
    target_location_id: Optional[str] = None
) -> str:
    """Shortcut per contesto reasoning (budget pieno)."""
    return await build_full_context(
        user_id=user_id,
        user_name=user_name,
        query=query,
        target_location_id=target_location_id,
        context_type="reasoning"
    )
