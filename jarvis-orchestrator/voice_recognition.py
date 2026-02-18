"""
JARVIS Voice Recognition Module
- Speaker identification usando Resemblyzer
- Enrollment e training voci
- Identificazione speaker da audio
"""

import os
import json
import logging
import numpy as np
from pathlib import Path
from typing import Optional, Tuple, List
from dataclasses import dataclass

import config

logger = logging.getLogger("JARVIS_VOICE")

# Path per i modelli vocali
VOICE_MODELS_DIR = Path(config.VOICE_MODELS_DIR)
VOICE_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# Soglia di similarità per riconoscimento (0.0 - 1.0)
SIMILARITY_THRESHOLD = config.VOICE_SIMILARITY_THRESHOLD

# Numero minimo di campioni per enrollment
MIN_ENROLLMENT_SAMPLES = config.VOICE_MIN_ENROLLMENT_SAMPLES


@dataclass
class SpeakerMatch:
    """Risultato del riconoscimento speaker."""
    user_id: Optional[int]
    user_name: Optional[str]
    confidence: float
    is_known: bool


class VoiceRecognizer:
    """
    Gestisce il riconoscimento vocale degli speaker.
    Usa Resemblyzer per creare embeddings vocali.
    """
    
    def __init__(self):
        self.encoder = None
        self.speaker_embeddings = {}  # user_id -> embedding array
        self._load_encoder()
        self._load_all_embeddings()
    
    def _load_encoder(self):
        """Carica il modello Resemblyzer e fa warm-up per eliminare JIT overhead."""
        try:
            import time as _t
            from resemblyzer import VoiceEncoder
            self.encoder = VoiceEncoder()
            # Warm-up: il primo embed_utterance è ~2.5s (PyTorch JIT/alloc),
            # i successivi sono ~100-200ms. Facciamolo ora al boot.
            t0 = _t.monotonic()
            _dummy = np.random.randn(16000 * 3).astype(np.float32) * 0.01
            self.encoder.embed_utterance(_dummy)
            t1 = _t.monotonic()
            logger.info(f"Voice encoder loaded + warm-up in {t1-t0:.2f}s")
        except ImportError:
            logger.warning("Resemblyzer not installed. Voice recognition disabled.")
            self.encoder = None
        except Exception as e:
            logger.error(f"Failed to load voice encoder: {e}")
            self.encoder = None
    
    def _load_all_embeddings(self):
        """Carica tutti gli embeddings salvati."""
        if not VOICE_MODELS_DIR.exists():
            return
        
        for model_file in VOICE_MODELS_DIR.glob("*.npy"):
            try:
                # Formato atteso: speaker_<user_id>.npy (es: speaker_123.npy)
                parts = model_file.stem.split("_")
                if len(parts) < 2:
                    logger.warning(f"Invalid voice model filename format: {model_file.name}")
                    continue
                user_id = int(parts[1])
                embedding = np.load(model_file)
                self.speaker_embeddings[user_id] = embedding
                logger.info(f"Loaded voice model for user {user_id}")
            except (ValueError, IndexError) as e:
                logger.warning(f"Could not parse user_id from voice model {model_file.name}: {e}")
            except Exception as e:
                logger.error(f"Failed to load voice model {model_file}: {e}")
    
    @staticmethod
    def _fast_preprocess(audio_data: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
        """Preprocessing veloce: normalizza + trim silenzio.

        Sostituisce resemblyzer.preprocess_wav (che usa librosa, ~20s su CPU!)
        con operazioni numpy pure (~3ms). Il nostro audio è già 16kHz mono.
        """
        # Assicura float32
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32) / 32768.0

        # Normalizza picco
        peak = np.max(np.abs(audio_data))
        if peak > 0:
            audio_data = audio_data / peak

        # Energy-based VAD trim (rimuovi silenzio iniziale/finale)
        frame_len = int(0.01 * sample_rate)  # 10ms frames
        n_frames = len(audio_data) // frame_len
        if n_frames > 0:
            frames = audio_data[:n_frames * frame_len].reshape(n_frames, frame_len)
            energy = np.sum(frames ** 2, axis=1)
            threshold = np.mean(energy) * 0.1
            active = np.where(energy > threshold)[0]
            if len(active) > 0:
                margin = int(0.1 * sample_rate)  # 100ms margin
                start = max(0, active[0] * frame_len - margin)
                end = min(len(audio_data), (active[-1] + 1) * frame_len + margin)
                audio_data = audio_data[start:end]

        return audio_data

    def _audio_to_embedding(self, audio_data: np.ndarray, sample_rate: int = 16000,
                             max_duration_s: float = 0) -> Optional[np.ndarray]:
        """Converte audio in embedding vocale.

        Args:
            max_duration_s: Se > 0, taglia l'audio a max N secondi (prende la parte centrale).
                           Utile per speaker ID dove bastano pochi secondi.
        """
        if self.encoder is None:
            return None

        try:
            import time as _t

            # Taglia audio se richiesto (per speaker ID bastano pochi secondi)
            if max_duration_s > 0:
                max_samples = int(max_duration_s * sample_rate)
                if len(audio_data) > max_samples:
                    # Prendi la parte centrale (più probabile che contenga speech)
                    start = (len(audio_data) - max_samples) // 2
                    audio_data = audio_data[start:start + max_samples]

            # Preprocessing veloce (numpy, ~3ms) — NO librosa/preprocess_wav (~20s!)
            t0 = _t.monotonic()
            wav = self._fast_preprocess(audio_data, sample_rate)
            t1 = _t.monotonic()

            # Genera embedding (dopo warm-up: ~100-200ms)
            embedding = self.encoder.embed_utterance(wav)
            t2 = _t.monotonic()

            logger.info(f"Embedding timing: preprocess={((t1-t0)*1000):.0f}ms, "
                        f"inference={((t2-t1)*1000):.0f}ms, "
                        f"audio={len(wav)/sample_rate:.1f}s")
            return embedding

        except Exception as e:
            logger.error(f"Failed to create embedding: {e}")
            return None
    
    def identify_speaker(self, audio_bytes: bytes, sample_rate: int = 16000) -> SpeakerMatch:
        """
        Identifica lo speaker da un campione audio.

        Returns:
            SpeakerMatch con user_id, nome, confidence e flag is_known
        """
        if self.encoder is None or not self.speaker_embeddings:
            logger.warning(f"Speaker ID skipped: encoder={self.encoder is not None}, models={len(self.speaker_embeddings)}")
            return SpeakerMatch(
                user_id=None,
                user_name=None,
                confidence=0.0,
                is_known=False
            )

        # Converti bytes in numpy array
        audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
        logger.debug(f"Speaker ID: audio {len(audio_data)} samples, dtype={audio_data.dtype}")

        # Genera embedding (trimma a max 8s — bastano per speaker ID)
        query_embedding = self._audio_to_embedding(audio_data, sample_rate, max_duration_s=8.0)
        if query_embedding is None:
            logger.warning("Speaker ID: failed to generate embedding from audio")
            return SpeakerMatch(None, None, 0.0, False)

        # Confronta con tutti gli speaker registrati
        best_match_id = None
        best_similarity = 0.0
        all_scores = {}

        for user_id, stored_embedding in self.speaker_embeddings.items():
            similarity = self._cosine_similarity(query_embedding, stored_embedding)
            all_scores[user_id] = round(similarity, 4)
            if similarity > best_similarity:
                best_similarity = similarity
                best_match_id = user_id

        logger.info(f"Speaker ID: scores={all_scores}, best={best_match_id} ({best_similarity:.4f}), threshold={SIMILARITY_THRESHOLD}")

        # Verifica soglia
        if best_similarity >= SIMILARITY_THRESHOLD and best_match_id is not None:
            from database import get_user_by_id
            user = get_user_by_id(best_match_id)
            logger.info(f"Speaker identified: user_id={best_match_id}, name={user.name if user else 'Unknown'}, confidence={best_similarity:.4f}")
            return SpeakerMatch(
                user_id=best_match_id,
                user_name=user.name if user else "Unknown",
                confidence=float(best_similarity),
                is_known=True
            )

        logger.info(f"Speaker NOT identified: best similarity {best_similarity:.4f} < threshold {SIMILARITY_THRESHOLD}")
        return SpeakerMatch(
            user_id=None,
            user_name=None,
            confidence=float(best_similarity),
            is_known=False
        )
    
    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calcola la similarità coseno tra due vettori."""
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    
    def start_enrollment(self, user_id: int) -> dict:
        """
        Inizia il processo di enrollment per un utente.
        Ritorna le istruzioni per l'utente.
        """
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        enrollment_dir.mkdir(exist_ok=True)
        
        # Pulisci eventuali enrollment precedenti incompleti
        for f in enrollment_dir.glob("*.npy"):
            f.unlink()
        
        return {
            "status": "started",
            "user_id": user_id,
            "samples_required": MIN_ENROLLMENT_SAMPLES,
            "samples_collected": 0,
            "instructions": [
                "Leggi ad alta voce le frasi che ti verranno mostrate.",
                "Parla normalmente, come faresti con Jarvis.",
                f"Sono necessari almeno {MIN_ENROLLMENT_SAMPLES} campioni vocali.",
                "Cerca di registrare in ambienti diversi se possibile."
            ],
            "phrases": [
                # Frasi corte (1-3 parole) — simili a comandi reali rapidi
                "Chi sono?",
                "Jarvis!",
                "Ciao!",
                "Spegni tutto.",
                "Ancora.",
                "Continua.",
                "Stop.",
                "Grazie Jarvis.",
                "Buongiorno!",
                # Frasi medie (4-7 parole) — comandi tipici
                "Accendi le luci del salotto.",
                "Che tempo fa oggi?",
                "Spegni tutte le luci.",
                "Alza il volume.",
                "Apri le tapparelle della cucina.",
                "Quanto manca alla riunione?",
                "Dimmi le notizie di oggi.",
                "Qual è la temperatura?",
                # Frasi lunghe (8+ parole) — domande articolate
                "Ricordami di chiamare il dentista domani mattina.",
                "Imposta una sveglia per domani alle sette.",
                "Che programmi ci sono stasera in televisione?",
                "Abbassa il volume della musica in soggiorno.",
                "Jarvis, qual è il meteo per il weekend?",
                "Aggiungi il latte alla lista della spesa.",
                "Chiudi la porta del garage per favore.",
            ]
        }
    
    def add_enrollment_sample(self, user_id: int, audio_bytes: bytes, sample_rate: int = 16000) -> dict:
        """
        Aggiunge un campione audio all'enrollment.
        """
        if self.encoder is None:
            return {"status": "error", "message": "Voice encoder not available"}
        
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        if not enrollment_dir.exists():
            return {"status": "error", "message": "Enrollment not started"}
        
        # Converti e genera embedding
        audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
        embedding = self._audio_to_embedding(audio_data, sample_rate)
        
        if embedding is None:
            return {"status": "error", "message": "Failed to process audio"}
        
        # Salva il campione
        existing_samples = list(enrollment_dir.glob("sample_*.npy"))
        sample_num = len(existing_samples)
        np.save(enrollment_dir / f"sample_{sample_num}.npy", embedding)
        
        samples_collected = sample_num + 1
        
        return {
            "status": "sample_added",
            "samples_collected": samples_collected,
            "samples_required": MIN_ENROLLMENT_SAMPLES,
            "ready_to_complete": samples_collected >= MIN_ENROLLMENT_SAMPLES
        }
    
    def complete_enrollment(self, user_id: int) -> dict:
        """
        Completa l'enrollment combinando tutti i campioni in un modello.
        """
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        if not enrollment_dir.exists():
            return {"status": "error", "message": "Enrollment not started"}
        
        samples = list(enrollment_dir.glob("sample_*.npy"))
        if len(samples) < MIN_ENROLLMENT_SAMPLES:
            return {
                "status": "error",
                "message": f"Not enough samples. Have {len(samples)}, need {MIN_ENROLLMENT_SAMPLES}"
            }
        
        # Carica tutti gli embeddings e fai la media
        embeddings = [np.load(s) for s in samples]
        combined_embedding = np.mean(embeddings, axis=0)
        
        # Normalizza
        combined_embedding = combined_embedding / np.linalg.norm(combined_embedding)
        
        # Salva il modello finale
        model_path = VOICE_MODELS_DIR / f"user_{user_id}.npy"
        np.save(model_path, combined_embedding)
        
        # Aggiorna cache in memoria
        self.speaker_embeddings[user_id] = combined_embedding
        
        # Aggiorna database
        from database import update_user_voice_enrollment
        update_user_voice_enrollment(user_id, True, str(model_path))
        
        # Pulisci directory enrollment
        for f in samples:
            f.unlink()
        enrollment_dir.rmdir()
        
        logger.info(f"Voice enrollment completed for user {user_id}")
        
        return {
            "status": "completed",
            "user_id": user_id,
            "model_path": str(model_path)
        }
    
    def cancel_enrollment(self, user_id: int) -> dict:
        """Annulla un enrollment in corso."""
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        if enrollment_dir.exists():
            for f in enrollment_dir.glob("*.npy"):
                f.unlink()
            enrollment_dir.rmdir()
        
        return {"status": "cancelled", "user_id": user_id}
    
    def delete_voice_model(self, user_id: int) -> dict:
        """Elimina il modello vocale di un utente."""
        model_path = VOICE_MODELS_DIR / f"user_{user_id}.npy"
        
        if model_path.exists():
            model_path.unlink()
        
        if user_id in self.speaker_embeddings:
            del self.speaker_embeddings[user_id]
        
        from database import update_user_voice_enrollment
        update_user_voice_enrollment(user_id, False, None)
        
        return {"status": "deleted", "user_id": user_id}
    
    def get_enrollment_status(self, user_id: int) -> dict:
        """Ritorna lo stato dell'enrollment per un utente."""
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        model_path = VOICE_MODELS_DIR / f"user_{user_id}.npy"
        
        if model_path.exists():
            return {
                "status": "enrolled",
                "user_id": user_id,
                "has_model": True
            }
        
        if enrollment_dir.exists():
            samples = list(enrollment_dir.glob("sample_*.npy"))
            return {
                "status": "in_progress",
                "user_id": user_id,
                "samples_collected": len(samples),
                "samples_required": MIN_ENROLLMENT_SAMPLES
            }
        
        return {
            "status": "not_enrolled",
            "user_id": user_id,
            "has_model": False
        }

    def quick_enroll_from_session(self, user_id: int, audio_bytes: bytes,
                                   sample_rate: int = 16000) -> dict:
        """
        Aggiunge un campione di enrollment da una sessione audio live.

        Pensato per auto-enrollment dall'AtomS3R: ogni volta che l'utente parla
        durante un enrollment in corso, l'audio della sessione viene usato come
        campione. Skippa audio troppo corto (< 0.5s). Auto-completa quando
        raggiunge MIN_ENROLLMENT_SAMPLES.

        Args:
            user_id: ID utente con enrollment in corso
            audio_bytes: PCM 16-bit mono bytes
            sample_rate: Sample rate (default 16000)

        Returns:
            dict con status, samples_collected, auto_completed flag
        """
        if self.encoder is None:
            return {"status": "error", "message": "Voice encoder not available"}

        # Verifica che ci sia enrollment in corso per questo utente
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        if not enrollment_dir.exists():
            # Auto-crea directory enrollment se non esiste
            enrollment_dir.mkdir(exist_ok=True)
            logger.info(f"Quick enroll: auto-created enrollment dir for user {user_id}")

        # Skippa audio troppo corto (< 0.5s)
        audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
        duration_s = len(audio_data) / sample_rate
        if duration_s < 0.5:
            logger.debug(f"Quick enroll: audio too short ({duration_s:.2f}s < 0.5s), skipping")
            return {"status": "skipped", "reason": "audio_too_short",
                    "duration": round(duration_s, 2)}

        # Genera embedding
        embedding = self._audio_to_embedding(audio_data, sample_rate)
        if embedding is None:
            return {"status": "error", "message": "Failed to process audio"}

        # Salva il campione
        existing_samples = list(enrollment_dir.glob("sample_*.npy"))
        sample_num = len(existing_samples)
        np.save(enrollment_dir / f"sample_{sample_num}.npy", embedding)
        samples_collected = sample_num + 1

        logger.info(f"Quick enroll: user {user_id} sample {samples_collected}/{MIN_ENROLLMENT_SAMPLES} "
                     f"({duration_s:.1f}s)")

        # Auto-completa se abbiamo abbastanza campioni
        if samples_collected >= MIN_ENROLLMENT_SAMPLES:
            logger.info(f"Quick enroll: auto-completing enrollment for user {user_id}")
            result = self.complete_enrollment(user_id)
            result["auto_completed"] = True
            result["samples_collected"] = samples_collected
            return result

        return {
            "status": "sample_added",
            "samples_collected": samples_collected,
            "samples_required": MIN_ENROLLMENT_SAMPLES,
            "auto_completed": False,
            "duration": round(duration_s, 2)
        }

    def has_active_enrollment(self, user_id: int) -> bool:
        """Controlla se un utente ha un enrollment in corso."""
        enrollment_dir = VOICE_MODELS_DIR / f"enrollment_{user_id}"
        return enrollment_dir.exists()

    def get_all_active_enrollments(self) -> list:
        """Ritorna tutti gli user_id con enrollment in corso."""
        active = []
        if VOICE_MODELS_DIR.exists():
            for d in VOICE_MODELS_DIR.iterdir():
                if d.is_dir() and d.name.startswith("enrollment_"):
                    try:
                        uid = int(d.name.split("_", 1)[1])
                        active.append(uid)
                    except (ValueError, IndexError):
                        pass
        return active


# Istanza globale
voice_recognizer = VoiceRecognizer()


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def identify_speaker_from_audio(audio_bytes: bytes) -> SpeakerMatch:
    """Wrapper per identificazione speaker."""
    return voice_recognizer.identify_speaker(audio_bytes)


def get_speaker_context(audio_bytes: bytes) -> dict:
    """
    Identifica lo speaker e ritorna il contesto completo.
    Usato dall'orchestratore per arricchire il context.
    """
    match = voice_recognizer.identify_speaker(audio_bytes)
    
    if match.is_known and match.user_id:
        from database import get_user_by_id
        user = get_user_by_id(match.user_id)
        
        return {
            "speaker_identified": True,
            "speaker_id": match.user_id,
            "speaker_name": match.user_name,
            "speaker_confidence": match.confidence,
            "is_admin": user.is_admin if user else False
        }
    
    return {
        "speaker_identified": False,
        "speaker_id": None,
        "speaker_name": "Sconosciuto",
        "speaker_confidence": match.confidence,
        "is_admin": False
    }
