"""
JARVIS Memory Jobs
- Schedula il job notturno di habit extraction (orchestrator -> mem0).
- Pulisce chat_memory vecchia (HOT-only retention).

I layer SQL warm/cold/longterm sono stati rimossi: la memoria semantica
long-term vive in mem0-stack. L'orchestrator continua a scrivere su mem0
tramite habit_extraction (regolarita' domotica + preferenze simple_chat).
"""

import asyncio
import logging
from datetime import datetime

from database import clear_old_chat_memory
from habit_extraction import run_habit_extraction_job
import config

logger = logging.getLogger("JARVIS_MEMORY")


# ===========================================================================
# SCHEDULER
# ===========================================================================

async def memory_scheduler():
    """Background scheduler per job memoria. Avviato da main.py."""
    await asyncio.sleep(30)  # lascia il time di startup
    logger.info("Memory scheduler started")

    last_daily_run_date = None

    while True:
        now = datetime.now()

        try:
            # Daily job: habit extraction + cleanup chat_memory.
            # Esegue UNA volta al giorno all'ora configurata.
            today = now.date()
            if (
                now.hour == config.MEMORY_DAILY_TRIGGER_HOUR
                and last_daily_run_date != today
            ):
                logger.info("Running daily habit extraction -> mem0...")
                try:
                    await run_habit_extraction_job()
                except Exception as e:
                    logger.error(f"Habit extraction failed: {e}")

                logger.info("Cleaning up old chat_memory (HOT retention)...")
                try:
                    clear_old_chat_memory()
                except Exception as e:
                    logger.error(f"chat_memory cleanup failed: {e}")

                last_daily_run_date = today

        except Exception as e:
            logger.error(f"Memory scheduler error: {e}")

        await asyncio.sleep(60)  # check ogni minuto
