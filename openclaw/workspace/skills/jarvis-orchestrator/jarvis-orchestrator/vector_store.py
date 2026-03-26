"""
JARVIS Vector Store
- ChromaDB per retrieval semantico
- Embedding via Ollama (nomic-embed-text) in locale, Gemini in cloud
- Hybrid search con recency boost
"""

import os
import time
import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

import chromadb
from chromadb.config import Settings

import config

logger = logging.getLogger("JARVIS_VECTOR")

# ===========================================================================
# CONFIG
# ===========================================================================

CHROMA_PATH = os.getenv("JARVIS_CHROMA_PATH", config.CHROMA_PATH)
EMBEDDING_MODEL = config.EMBEDDING_MODEL
OLLAMA_URL = config.OLLAMA_URL

# Collection names
COLLECTION_USER_MESSAGES = "user_messages"
COLLECTION_USER_FACTS = "user_facts"

# Retention
VECTOR_RETENTION_DAYS = config.VECTOR_RETENTION_DAYS
MAX_RESULTS_DEFAULT = config.MAX_VECTOR_RESULTS_DEFAULT


# ===========================================================================
# EMBEDDING FUNCTIONS
# ===========================================================================
# AI_BACKEND=local → OllamaEmbeddingFunction (nomic-embed-text, GPU locale)
# AI_BACKEND=api   → GeminiEmbeddingFunction (gemini-embedding-001, cloud)
# ===========================================================================

EMBEDDING_DIM = 768  # Dimensione comune a entrambi i provider


class OllamaEmbeddingFunction:
    """Embedding via Ollama locale (nomic-embed-text).
    Usato quando AI_BACKEND=local.
    """

    def __init__(self, model: str = EMBEDDING_MODEL, url: str = OLLAMA_URL):
        self.model = model
        self.url = f"{url}/api/embeddings"

    def name(self) -> str:
        """Required by ChromaDB >= 0.6 for embedding function identification."""
        return f"ollama-{self.model}"

    def __call__(self, input: List[str]) -> List[List[float]]:
        """Sync embedding for ChromaDB."""
        import requests
        embeddings = []
        for text in input:
            # Guard: ensure text is a string (ChromaDB may nest lists)
            if isinstance(text, list):
                text = " ".join(str(t) for t in text)
            if not isinstance(text, str):
                text = str(text)
            if not text.strip():
                embeddings.append([0.0] * EMBEDDING_DIM)
                continue
            try:
                response = requests.post(
                    self.url,
                    json={"model": self.model, "prompt": text},
                    timeout=config.TIMEOUTS["embedding"]
                )
                if response.status_code == 200:
                    embeddings.append(response.json()["embedding"])
                else:
                    logger.error(f"Embedding error: {response.status_code}")
                    embeddings.append([0.0] * EMBEDDING_DIM)
            except Exception as e:
                logger.error(f"Embedding exception: {e}")
                embeddings.append([0.0] * EMBEDDING_DIM)
        return embeddings

    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        return self(documents)

    def embed_query(self, input: str = None, query: str = None) -> List[float]:
        text = input or query
        if text is None:
            raise ValueError("Either 'input' or 'query' must be provided")
        return self([text])[0]


class GeminiEmbeddingFunction:
    """Embedding via Gemini API (gemini-embedding-001).
    Usato quando AI_BACKEND=api. Stessa dimensionalita (768) di nomic-embed-text.
    """

    GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"

    def __init__(self):
        self.api_key = config.GEMINI_API_KEY
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY required for cloud embeddings")

    def name(self) -> str:
        """Required by ChromaDB >= 0.6."""
        return "gemini-embedding-001"

    def __call__(self, input: List[str]) -> List[List[float]]:
        """Sync embedding for ChromaDB."""
        import requests
        embeddings = []
        for text in input:
            # Guard: ensure text is a string (ChromaDB may nest lists)
            if isinstance(text, list):
                text = " ".join(str(t) for t in text)
            if not isinstance(text, str):
                text = str(text)
            if not text.strip():
                embeddings.append([0.0] * EMBEDDING_DIM)
                continue
            try:
                response = requests.post(
                    self.GEMINI_EMBED_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    },
                    json={
                        "content": {"parts": [{"text": text}]},
                        "output_dimensionality": EMBEDDING_DIM,
                    },
                    timeout=config.TIMEOUTS.get("embedding", 30),
                )
                if response.status_code == 200:
                    data = response.json()
                    embeddings.append(data["embedding"]["values"])
                else:
                    logger.error(f"Gemini embedding error {response.status_code}: {response.text[:200]}")
                    embeddings.append([0.0] * EMBEDDING_DIM)
            except Exception as e:
                logger.error(f"Gemini embedding exception: {e}")
                embeddings.append([0.0] * EMBEDDING_DIM)
        return embeddings

    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        return self(documents)

    def embed_query(self, input: str = None, query: str = None) -> List[float]:
        text = input or query
        if text is None:
            raise ValueError("Either 'input' or 'query' must be provided")
        return self([text])[0]


def get_embedding_function():
    """Factory: seleziona embedding function in base a AI_BACKEND."""
    if config.AI_BACKEND == "api":
        logger.info("🌐 Using Gemini embeddings (cloud mode)")
        return GeminiEmbeddingFunction()
    else:
        logger.info("🖥️ Using Ollama embeddings (local mode)")
        return OllamaEmbeddingFunction()


# ===========================================================================
# VECTOR STORE MANAGER
# ===========================================================================

class UserVectorStore:
    """
    Vector store per user memory.
    Gira sull'orchestrator.
    """

    def __init__(self):
        self.client = None
        self.collections: Dict[str, Any] = {}
        self._initialized = False

    def initialize(self):
        """Inizializza ChromaDB e collections."""
        if self._initialized:
            return

        self.client = chromadb.PersistentClient(
            path=CHROMA_PATH,
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True
            )
        )

        embedding_fn = get_embedding_function()

        # Collection per messaggi utente (conversazioni)
        self.collections[COLLECTION_USER_MESSAGES] = self.client.get_or_create_collection(
            name=COLLECTION_USER_MESSAGES,
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        # Collection per fatti long-term utente
        self.collections[COLLECTION_USER_FACTS] = self.client.get_or_create_collection(
            name=COLLECTION_USER_FACTS,
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        self._initialized = True
        logger.info(f"Vector store initialized at {CHROMA_PATH}")

    def ensure_initialized(self):
        if not self._initialized:
            self.initialize()

    # ===== WRITE OPERATIONS =====

    def add_message(
        self,
        user_id: int,
        role: str,
        content: str,
        speaker_name: str,
        timestamp: float,
        source: str = "unknown"
    ):
        """
        Aggiunge un messaggio al vector store.
        Chiamato in parallelo a save_chat_message().
        """
        self.ensure_initialized()

        doc_id = f"msg_{user_id}_{int(timestamp * 1000)}"

        # Testo per embedding: include contesto
        embed_text = f"{speaker_name}: {content}"

        try:
            self.collections[COLLECTION_USER_MESSAGES].add(
                documents=[embed_text],
                metadatas=[{
                    "user_id": user_id,
                    "role": role,
                    "speaker_name": speaker_name,
                    "timestamp": timestamp,
                    "source": source,
                    "content": content[:config.VECTOR_METADATA_TRUNCATE]  # Truncate for metadata
                }],
                ids=[doc_id]
            )
        except Exception as e:
            # Duplicate ID - already exists
            if "already exists" in str(e).lower():
                pass
            else:
                logger.error(f"Error adding message to vector store: {e}")

    def add_user_fact(
        self,
        user_id: int,
        fact: str,
        category: str = "general",
        confidence: float = 0.8,
        source: str = "extracted"
    ):
        """
        Aggiunge un fatto utente al vector store.
        Chiamato in parallelo a save_user_longterm_fact().
        """
        self.ensure_initialized()

        doc_id = f"fact_{user_id}_{hash(fact) % 10**10}"

        try:
            # Upsert: se esiste aggiorna, altrimenti crea
            self.collections[COLLECTION_USER_FACTS].upsert(
                documents=[fact],
                metadatas=[{
                    "user_id": user_id,
                    "category": category,
                    "confidence": confidence,
                    "source": source,
                    "timestamp": time.time()
                }],
                ids=[doc_id]
            )
        except Exception as e:
            logger.error(f"Error adding fact to vector store: {e}")

    # ===== READ OPERATIONS =====

    def search_messages(
        self,
        query: str,
        user_id: int,
        n_results: int = MAX_RESULTS_DEFAULT,
        min_timestamp: Optional[float] = None,
        include_all_users: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Cerca messaggi semanticamente simili alla query.

        Args:
            query: Testo della query
            user_id: ID utente (filtra per questo utente)
            n_results: Numero massimo risultati
            min_timestamp: Timestamp minimo (default: 7 giorni fa)
            include_all_users: Se True, cerca in tutti gli utenti (per contesto globale)

        Returns:
            Lista di messaggi con score e metadata
        """
        self.ensure_initialized()

        if min_timestamp is None:
            min_timestamp = time.time() - (VECTOR_RETENTION_DAYS * 86400)

        # Build where filter
        where_filter = {"timestamp": {"$gte": min_timestamp}}
        if not include_all_users:
            where_filter = {
                "$and": [
                    {"user_id": {"$eq": user_id}},
                    {"timestamp": {"$gte": min_timestamp}}
                ]
            }

        try:
            results = self.collections[COLLECTION_USER_MESSAGES].query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )

            return self._format_results_with_recency(results)

        except Exception as e:
            logger.error(f"Error searching messages: {e}")
            return []

    def search_user_facts(
        self,
        query: str,
        user_id: int,
        n_results: int = 20,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Cerca fatti utente semanticamente simili.
        """
        self.ensure_initialized()

        where_filter = {"user_id": {"$eq": user_id}}
        if category:
            where_filter = {
                "$and": [
                    {"user_id": {"$eq": user_id}},
                    {"category": {"$eq": category}}
                ]
            }

        try:
            results = self.collections[COLLECTION_USER_FACTS].query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )

            return self._format_results(results)

        except Exception as e:
            logger.error(f"Error searching facts: {e}")
            return []

    def _format_results(self, results: Dict) -> List[Dict[str, Any]]:
        """Formatta risultati ChromaDB."""
        formatted = []

        if not results or not results.get('documents'):
            return formatted

        docs = results['documents'][0] if results['documents'] else []
        metas = results['metadatas'][0] if results['metadatas'] else []
        distances = results['distances'][0] if results['distances'] else []

        for doc, meta, dist in zip(docs, metas, distances):
            formatted.append({
                "content": doc,
                "metadata": meta,
                "similarity": 1 - dist,  # ChromaDB returns distance, not similarity
                "score": 1 - dist
            })

        return formatted

    def _format_results_with_recency(self, results: Dict) -> List[Dict[str, Any]]:
        """
        Formatta risultati con recency boost.
        Score finale = similarity * recency_factor
        """
        formatted = self._format_results(results)

        now = time.time()
        for item in formatted:
            timestamp = item['metadata'].get('timestamp', now)
            age_hours = (now - timestamp) / 3600

            # Recency decay: more recent = higher boost
            # Formula: recency_factor = 1 / (1 + age_hours * 0.05)
            # - 0 ore fa: 1.0
            # - 24 ore fa: 0.45
            # - 48 ore fa: 0.29
            # - 168 ore (7gg) fa: 0.11
            recency_factor = 1 / (1 + age_hours * config.VECTOR_RECENCY_DECAY)

            item['recency_factor'] = recency_factor
            item['final_score'] = item['similarity'] * recency_factor

        # Riordina per final_score
        formatted.sort(key=lambda x: x['final_score'], reverse=True)

        return formatted

    # ===== MAINTENANCE =====

    def cleanup_old_vectors(self, max_age_days: int = VECTOR_RETENTION_DAYS):
        """
        Rimuove vettori piu' vecchi di max_age_days.
        Da chiamare periodicamente (es: daily job).
        """
        self.ensure_initialized()

        cutoff = time.time() - (max_age_days * 86400)

        for collection_name in [COLLECTION_USER_MESSAGES]:
            try:
                collection = self.collections[collection_name]

                # ChromaDB non supporta delete by where direttamente
                # Workaround: query tutti i vecchi e delete per ID
                old_results = collection.get(
                    where={"timestamp": {"$lt": cutoff}},
                    include=[]
                )

                if old_results and old_results.get('ids'):
                    collection.delete(ids=old_results['ids'])
                    logger.info(f"Cleaned {len(old_results['ids'])} old vectors from {collection_name}")

            except Exception as e:
                logger.error(f"Error cleaning {collection_name}: {e}")


# ===========================================================================
# GLOBAL INSTANCE
# ===========================================================================

user_vector_store = UserVectorStore()


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def init_vector_store():
    """Inizializza vector store. Chiamare in startup.
    Se Ollama non è raggiungibile (es. cloud mode), logga warning e continua.
    """
    try:
        user_vector_store.initialize()
    except Exception as e:
        logger.warning(f"⚠️ Vector store init failed (embeddings disabled): {e}")
        logger.warning("Memory search will be unavailable. This is normal in cloud/API mode without Ollama.")


def search_user_context(
    query: str,
    user_id: int,
    n_messages: int = 30,
    n_facts: int = 20
) -> Dict[str, List[Dict]]:
    """
    Ricerca ibrida per contesto utente.
    Restituisce messaggi e fatti rilevanti.
    """
    messages = user_vector_store.search_messages(query, user_id, n_results=n_messages)
    facts = user_vector_store.search_user_facts(query, user_id, n_results=n_facts)

    return {
        "messages": messages,
        "facts": facts
    }
