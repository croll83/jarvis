"""
JARVIS Context Builder - Hybrid Version
- Combina SQLite (strutturato) + ChromaDB (semantico)
- Recency boost per risultati vector
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
from vector_store import search_user_context, user_vector_store
from location_memory import (
    get_all_locations_memory,
    format_locations_memory_for_llm
)
import config

logger = logging.getLogger("JARVIS_CONTEXT")

# ===========================================================================
# VECTOR CONTEXT FORMATTING
# ===========================================================================

def format_vector_messages_for_llm(
    messages: List[Dict[str, Any]],
    max_tokens: int = 500,
    user_name: str = "User"
) -> str:
    """
    Formatta messaggi da vector search per il prompt.
    Include solo i piu' rilevanti (per final_score).
    """
    if not messages:
        return ""

    lines = [f"\n[Conversazioni rilevanti con {user_name}]"]
    tokens_used = 0

    for msg in messages:
        meta = msg.get('metadata', {})
        content = meta.get('content', msg.get('content', ''))[:200]
        speaker = meta.get('speaker_name', user_name)
        score = msg.get('final_score', msg.get('similarity', 0))

        # Skip low relevance
        if score < config.VECTOR_SCORE_MIN_MESSAGES:
            continue

        line = f"- {speaker}: {content}"
        est_tokens = len(line) // 4

        if tokens_used + est_tokens > max_tokens:
            break

        lines.append(line)
        tokens_used += est_tokens

    if len(lines) == 1:  # Solo header
        return ""

    return "\n".join(lines)


def format_vector_facts_for_llm(
    facts: List[Dict[str, Any]],
    max_tokens: int = 300,
    user_name: str = "User"
) -> str:
    """
    Formatta fatti da vector search per il prompt.
    """
    if not facts:
        return ""

    lines = [f"\n[Informazioni rilevanti su {user_name}]"]
    tokens_used = 0

    for fact in facts:
        content = fact.get('content', '')
        score = fact.get('similarity', 0)

        if score < config.VECTOR_SCORE_MIN_FACTS:
            continue

        line = f"- {content}"
        est_tokens = len(line) // 4

        if tokens_used + est_tokens > max_tokens:
            break

        lines.append(line)
        tokens_used += est_tokens

    if len(lines) == 1:
        return ""

    return "\n".join(lines)


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

    # ===== 1. USER MEMORY - SQL (structured) =====
    if user_id:
        user_memory_sql = get_user_memory_context(
            user_id=user_id,
            include_hot=True,
            include_warm=context_type == "reasoning",
            include_cold=context_type == "reasoning",
            include_longterm=True,
            warm_hours=config.REASONING_WARM_HOURS if context_type == "reasoning" else config.ROUTING_WARM_HOURS,
            cold_days=config.REASONING_COLD_DAYS if context_type == "reasoning" else config.ROUTING_COLD_DAYS
        )

        sql_context = format_user_memory_for_llm(
            user_memory_sql,
            user_name,
            max_tokens=budget["user_memory_sql"]
        )

        if sql_context.strip():
            parts.append(sql_context)

    # ===== 2. USER MEMORY - VECTOR (semantic) =====
    if user_id and query:
        try:
            vector_results = search_user_context(
                query=query,
                user_id=user_id,
                n_messages=config.CONTEXT_SEARCH_LIMITS[context_type]["n_messages"],
                n_facts=config.CONTEXT_SEARCH_LIMITS[context_type]["n_facts"]
            )

            # Format messaggi rilevanti
            msg_context = format_vector_messages_for_llm(
                vector_results.get("messages", []),
                max_tokens=budget["user_memory_vector"] // 2,
                user_name=user_name
            )
            if msg_context.strip():
                parts.append(msg_context)

            # Format fatti rilevanti
            fact_context = format_vector_facts_for_llm(
                vector_results.get("facts", []),
                max_tokens=budget["user_memory_vector"] // 2,
                user_name=user_name
            )
            if fact_context.strip():
                parts.append(fact_context)

        except Exception as e:
            logger.warning(f"Vector search failed, using SQL only: {e}")

    # ===== 3. LOCATION MEMORY - SQL (structured) =====
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

    # ===== 4. LOCATION MEMORY - VECTOR (semantic) =====
    if query and location_ids:
        try:
            from location_memory import (
                search_all_locations_events,
                format_location_vector_results_for_llm
            )

            location_vector_results = await search_all_locations_events(
                query=query,
                location_ids=location_ids if isinstance(location_ids, list) else [location_ids] if location_ids else None,
                n_results_per_location=config.CONTEXT_SEARCH_LIMITS[context_type]["n_location_results"]
            )

            if location_vector_results:
                location_vector_context = format_location_vector_results_for_llm(
                    location_vector_results,
                    max_tokens=budget["location_memory"] // 2
                )
                if location_vector_context.strip():
                    parts.append(location_vector_context)

        except Exception as e:
            logger.warning(f"Location vector search failed: {e}")

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
