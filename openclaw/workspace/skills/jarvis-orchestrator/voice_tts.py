"""
Voice TTS Handler — Multi-voice support for jarvis-orchestrator
Handles voice profile selection, TTS generation, and speaker detection
"""

import json
import subprocess
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Tuple
import asyncio

logger = logging.getLogger(__name__)

VOICE_PROFILES_PATH = Path(__file__).parent / "voice_profiles.json"
TMP_AUDIO_DIR = Path("/tmp")


class VoiceProfileManager:
    """Manages voice profiles for family members"""

    def __init__(self, config_path: str = None):
        """Initialize voice profile manager"""
        self.config_path = config_path or VOICE_PROFILES_PATH
        self.profiles = {}
        self.load_profiles()

    def load_profiles(self):
        """Load voice profiles from config"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                self.profiles = config.get('voice_profiles', {})
                self.tts_config = config.get('tts_engine_config', {})
                self.rules = config.get('voice_selection_rules', {})
                logger.info(f"Loaded {len(self.profiles)} voice profiles")
        except FileNotFoundError:
            logger.warning(f"Voice profiles config not found at {self.config_path}")
            self.profiles = {}
            self.tts_config = {}
            self.rules = {}

    def get_voice_for_speaker(self, speaker_id: str) -> Optional[Dict]:
        """Get voice profile for a speaker"""
        speaker_id_lower = speaker_id.lower()
        
        # Direct speaker mapping
        if speaker_id_lower in self.profiles:
            return self.profiles[speaker_id_lower]
        
        # Fallback to Marco (admin)
        if 'marco' in self.profiles:
            logger.warning(f"Speaker {speaker_id} not found, using Marco's voice")
            return self.profiles['marco']
        
        return None

    def get_voice_for_context(self, context: str) -> Optional[Dict]:
        """Get voice profile based on request context"""
        context_lower = context.lower()
        context_rules = self.rules.get('by_context', {})
        
        # Find matching context rule
        for key, speaker in context_rules.items():
            if key.lower() in context_lower:
                if speaker in self.profiles:
                    return self.profiles[speaker]
        
        # Default to Marco
        return self.profiles.get('marco')

    async def generate_tts(
        self,
        text: str,
        speaker_id: str = None,
        context: str = None,
        output_format: str = "ogg"
    ) -> Tuple[bool, Optional[str]]:
        """
        Generate TTS audio for text
        
        Args:
            text: Text to speak
            speaker_id: Speaker identifier (marco, ada, giorgio, sofia)
            context: Request context for auto-selection
            output_format: Output format (mp3, ogg)
        
        Returns:
            (success, file_path)
        """
        try:
            # Select voice profile
            profile = None
            if speaker_id:
                profile = self.get_voice_for_speaker(speaker_id)
            elif context:
                profile = self.get_voice_for_context(context)
            else:
                profile = self.profiles.get('marco')
            
            if not profile:
                logger.error("No voice profile available")
                return False, None
            
            voice_name = profile.get('tts_voice')
            logger.info(f"Generating TTS: speaker={profile.get('name')}, voice={voice_name}")
            
            # Generate MP3 first
            mp3_path = TMP_AUDIO_DIR / "jarvis-voice.mp3"
            cmd = [
                "edge-tts",
                "--voice", voice_name,
                "--text", text,
                "--write-media", str(mp3_path)
            ]
            
            result = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await result.communicate()
            if result.returncode != 0:
                logger.error(f"edge-tts failed: {stderr.decode()}")
                return False, None
            
            # Convert to requested format
            output_path = mp3_path
            if output_format == "ogg":
                output_path = TMP_AUDIO_DIR / "jarvis-voice.ogg"
                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-i", str(mp3_path),
                    "-c:a", "libopus",
                    "-b:a", "64k",
                    str(output_path)
                ]
                
                result = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await result.communicate()
                if result.returncode != 0:
                    logger.error(f"ffmpeg conversion failed: {stderr.decode()}")
                    # Fall back to MP3
                    output_path = mp3_path
            
            logger.info(f"TTS generated successfully: {output_path}")
            return True, str(output_path)
            
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return False, None

    def get_voice_pitch_rate(self, speaker_id: str) -> Dict[str, float]:
        """Get pitch and rate adjustments for a speaker"""
        profile = self.get_voice_for_speaker(speaker_id)
        if not profile:
            return {"rate": 1.0, "pitch": 0}
        
        return {
            "rate": profile.get('rate_adjustment', 1.0),
            "pitch": profile.get('pitch_adjustment', 0),
            "volume_db": profile.get('volume_db', 0)
        }


class SpeakerDetector:
    """Speaker detection using voice embeddings (Resemblyzer)"""
    
    def __init__(self):
        """Initialize speaker detector"""
        try:
            from resemblyzer import VoiceEncoder
            self.encoder = VoiceEncoder()
            self.speaker_embeddings = {}
            logger.info("Speaker detector initialized")
        except ImportError:
            logger.warning("resemblyzer not installed, speaker detection disabled")
            self.encoder = None
    
    def detect_speaker(self, audio_path: str) -> Tuple[Optional[str], float]:
        """
        Detect speaker from audio file
        
        Returns:
            (speaker_id, confidence)
        """
        if not self.encoder:
            return None, 0.0
        
        try:
            # Load audio and extract embedding
            from resemblyzer import preprocess_wav
            wav = preprocess_wav(audio_path)
            embedding = self.encoder.embed_utterance(wav)
            
            # Compare against known speaker embeddings
            best_match = None
            best_distance = float('inf')
            
            for speaker_id, ref_embedding in self.speaker_embeddings.items():
                distance = sum((embedding - ref_embedding) ** 2)
                if distance < best_distance:
                    best_distance = distance
                    best_match = speaker_id
            
            # Convert distance to confidence (heuristic)
            confidence = max(0, 1 - best_distance / 10)
            
            logger.info(f"Speaker detected: {best_match} (confidence: {confidence:.2f})")
            return best_match, confidence
            
        except Exception as e:
            logger.error(f"Speaker detection failed: {e}")
            return None, 0.0
    
    def register_speaker(self, speaker_id: str, audio_path: str):
        """Register a speaker by learning their voice from audio"""
        if not self.encoder:
            logger.warning("Speaker detection not available")
            return
        
        try:
            from resemblyzer import preprocess_wav
            wav = preprocess_wav(audio_path)
            embedding = self.encoder.embed_utterance(wav)
            self.speaker_embeddings[speaker_id] = embedding
            logger.info(f"Speaker registered: {speaker_id}")
        except Exception as e:
            logger.error(f"Failed to register speaker: {e}")


async def generate_voice_response(
    text: str,
    speaker_id: str = None,
    context: str = None,
    output_format: str = "ogg"
) -> Optional[str]:
    """
    Convenience function to generate TTS response
    
    Args:
        text: Text to speak
        speaker_id: Speaker identifier
        context: Request context
        output_format: Output format
    
    Returns:
        Path to audio file or None on error
    """
    manager = VoiceProfileManager()
    success, path = await manager.generate_tts(text, speaker_id, context, output_format)
    return path if success else None


def test_voice_profiles():
    """Test voice profile generation"""
    import asyncio
    
    manager = VoiceProfileManager()
    
    test_cases = [
        ("marco", "Acceso. Tutto funziona."),
        ("ada", "Ho controllato, va bene."),
        ("giorgio", "Boom! Luci accese al massimo!"),
        ("sofia", "Certo, cara! Accendo la luce per te."),
    ]
    
    for speaker_id, text in test_cases:
        print(f"\n[{speaker_id}] {text}")
        success, path = asyncio.run(
            manager.generate_tts(text, speaker_id=speaker_id)
        )
        if success:
            print(f"✓ Generated: {path}")
        else:
            print(f"✗ Failed to generate TTS")


if __name__ == "__main__":
    test_voice_profiles()
