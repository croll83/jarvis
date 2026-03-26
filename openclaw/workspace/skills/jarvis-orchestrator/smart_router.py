"""
Smart Model Router for jarvis-orchestrator
Routes requests to optimal LLM models based on task type and complexity
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)


class TaskClassifier:
    """Classifies tasks into categories for model routing"""
    
    # Task type patterns — matches user intent
    TASK_PATTERNS = {
        'smart_home_simple': {
            'keywords': [
                'accendi', 'spegni', 'illumina', 'luce', 'luci',
                'temperatura', 'riscaldamento', 'aria',
                'apri', 'chiudi', 'garage', 'porta', 'finestra',
                'musica', 'tv', 'volume', 'canale',
                'accensione', 'spegnimento', 'toggle'
            ],
            'patterns': [
                r'(accendi|spegni|illumina|alza|abbassa)\s+(la|il|le|i)\s+\w+',
                r'(luce|luci)\s+(accesa?|spenta?)',
                r'(temperatura|termostato)\s+(a|di)\s+\d+',
                r'(musica|tv|audio)\s+(accesa?|spenta?|su)',
                r'(apri|chiudi)\s+(garage|porta|finestra)',
            ],
            'tier': 'SIMPLE',
            'model_suggestion': 'qwen7b',
            'description': 'Simple home automation commands'
        },
        'quick_query': {
            'keywords': [
                'che ore sono', 'ora', 'time',
                'meteo', 'tempo', 'pioggia', 'sole', 'temperatura',
                'data', 'oggi', 'domani',
                'news', 'notizie', 'quotazioni',
                'risultati', 'punteggio', 'gol'
            ],
            'patterns': [
                r'(che|qual)\s+ora\s+è',
                r'che\s+(tempo|meteo)\s+fa',
                r'(temperatura|umidità)\s+esterna',
                r'ultim[aei]\s+notizie',
                r'(quotazione|prezzo)\s+\w+',
                r'(risultato|punteggio)\s+(partita|gara)',
            ],
            'tier': 'SIMPLE',
            'model_suggestion': 'gemini_flash',
            'description': 'Quick factual queries and real-time info'
        },
        'recipe_cooking': {
            'keywords': [
                'ricetta', 'ricette', 'cook', 'cooking',
                'ingredienti', 'preparazione', 'come fare',
                'piatto', 'piatti', 'cibo', 'cucina',
                'forno', 'pentola', 'minuti'
            ],
            'patterns': [
                r'ricetta\s+\w+',
                r'come\s+(si\s+)?fa\s+\w+',
                r'ingredienti\s+per\s+\w+',
                r'(prepara|cucina)\s+\w+',
            ],
            'tier': 'SIMPLE',
            'model_suggestion': 'gemini_flash',
            'description': 'Recipe and cooking instructions'
        },
        'complex_reasoning': {
            'keywords': [
                'analizza', 'spiega', 'riassumi', 'confronta', 'valuta',
                'strategia', 'decisione', 'implicazioni', 'conseguenze',
                'progettazione', 'architetto', 'sviluppo', 'implementa',
                'debug', 'risolvi', 'problema', 'errore',
                'complesso', 'difficile', 'articolato'
            ],
            'patterns': [
                r'(analizza|spiega|riassumi)\s+(\w+\s+){2,}',
                r'qual\s+è\s+la\s+miglior[ae]\s+\w+',
                r'(pro|contro|vantaggio|svantaggio)',
                r'come\s+(risolvere|debuggare|correggere)',
                r'(strategia|strategico|tattico)',
                r'(complesso|difficile|articolato)\s+\w+',
            ],
            'tier': 'COMPLEX',
            'model_suggestion': 'sonnet',
            'description': 'Complex reasoning, analysis, debugging'
        },
        'story_creative': {
            'keywords': [
                'racconta', 'storia', 'fiaba', 'favola', 'fantasy',
                'creativo', 'originale', 'immaginazione', 'inventa',
                'poesia', 'canzone', 'script', 'scena',
                'personaggio', 'trama', 'colpo di scena'
            ],
            'patterns': [
                r'racconta\s+un[a]?\s+\w+',
                r'(storia|fiaba|favola|leggenda)',
                r'inventa\s+un[a]?\s+\w+',
                r'scrivi\s+(una\s+)?(poesia|canzone|scena)',
            ],
            'tier': 'COMPLEX',
            'model_suggestion': 'sonnet',
            'description': 'Creative content, storytelling, writing'
        },
        'code_development': {
            'keywords': [
                'codice', 'programma', 'script', 'funzione', 'classe',
                'bug', 'errore', 'debug', 'fix', 'refactor',
                'python', 'javascript', 'java', 'rust', 'go',
                'api', 'endpoint', 'database', 'query',
                'test', 'test unitari', 'coverage'
            ],
            'patterns': [
                r'scrivi\s+\w+\s+(codice|programma|script|funzione)',
                r'(debug|correggi|risolvi)\s+(questo|il|il mio)\s+(codice|bug|errore)',
                r'(refactor|ottimizza|migliora)\s+(il|questo)\s+codice',
                r'(test|unittest)\s+per\s+\w+',
            ],
            'tier': 'COMPLEX',
            'model_suggestion': 'sonnet',
            'description': 'Code development, debugging, refactoring'
        },
        'calendar_scheduling': {
            'keywords': [
                'calendario', 'riunione', 'appuntamento', 'evento',
                'quando', 'orario', 'ora', 'giorno',
                'ricordi', 'promemoria', 'sveglia',
                'agenda', 'impegni'
            ],
            'patterns': [
                r'(quale|qual|quando)\s+è\s+\w+',
                r'prossim[o|a|i|e]\s+\w+',
                r'(ho|abbiamo)\s+(una|un)\s+(riunione|appuntamento)',
                r'(ricordami|impostare)\s+(una|un)\s+(riunione|evento)',
            ],
            'tier': 'SIMPLE',
            'model_suggestion': 'gemini_flash',
            'description': 'Calendar, scheduling, reminders'
        },
    }
    
    def classify(self, text: str) -> Dict:
        """
        Classify a task into a category
        
        Returns:
            {
                'task_type': str,
                'category': str,
                'tier': str,
                'model_suggestion': str,
                'confidence': float,
                'reasoning': str
            }
        """
        text_lower = text.lower()
        text_len = len(text.split())
        
        scores = {}
        
        for task_type, config in self.TASK_PATTERNS.items():
            score = 0
            
            # Keyword matching (0-40 points)
            keyword_matches = sum(1 for kw in config['keywords'] if kw in text_lower)
            if keyword_matches > 0:
                score += min(40, keyword_matches * 10)
            
            # Pattern matching (0-40 points)
            pattern_matches = sum(1 for pattern in config['patterns'] 
                                 if re.search(pattern, text_lower))
            if pattern_matches > 0:
                score += min(40, pattern_matches * 10)
            
            # Length consideration
            if task_type == 'smart_home_simple':
                # Simple commands are short (5-15 words)
                if 5 <= text_len <= 15:
                    score += 10
            elif task_type == 'quick_query':
                # Quick queries are short (3-10 words)
                if 3 <= text_len <= 10:
                    score += 10
            elif task_type in ['complex_reasoning', 'code_development', 'story_creative']:
                # Complex tasks are longer (15+ words)
                if text_len >= 15:
                    score += 10
            
            if score > 0:
                scores[task_type] = score
        
        # Select best match
        if not scores:
            return {
                'task_type': 'unknown',
                'category': 'General',
                'tier': 'SIMPLE',
                'model_suggestion': 'qwen7b',
                'confidence': 0.0,
                'reasoning': 'No clear pattern match, falling back to simple model'
            }
        
        best_task = max(scores, key=scores.get)
        config = self.TASK_PATTERNS[best_task]
        confidence = min(1.0, scores[best_task] / 100)
        
        return {
            'task_type': best_task,
            'category': config['description'],
            'tier': config['tier'],
            'model_suggestion': config['model_suggestion'],
            'confidence': confidence,
            'reasoning': f"Pattern match: {best_task} (score: {scores[best_task]}/100)"
        }


class ModelRouter:
    """Routes tasks to appropriate LLM models based on classification"""
    
    # Model mapping for jarvis-orchestrator
    MODEL_MAPPING = {
        'qwen7b': {
            'provider': 'nvidia-nim',
            'model_id': 'nvidia-nim/qwen/qwen2.5-7b-instruct',
            'alias': 'NIM-Model1',
            'tier': 'SIMPLE',
            'cost_per_m': 0.15,
            'use_for': 'Simple home automation, straightforward commands',
            'latency_ms': 500,  # estimated
        },
        'gemini_flash': {
            'provider': 'google',
            'model_id': 'google-gemini-cli/gemini-3-pro-preview',
            'alias': 'Gemini3-Pro',
            'tier': 'COMPLEX',
            'cost_per_m': 2.0,
            'use_for': 'Quick queries, real-time info, light reasoning',
            'latency_ms': 800,
        },
        'sonnet': {
            'provider': 'anthropic',
            'model_id': 'anthropic-proxy-2/claude-sonnet-4-5',
            'alias': 'Sonnet-Proxy2',
            'tier': 'COMPLEX',
            'cost_per_m': 3.0,
            'use_for': 'Complex reasoning, creative tasks, debugging',
            'latency_ms': 2000,
        },
        'deepseek_v3': {
            'provider': 'nvidia-nim',
            'model_id': 'nvidia-nim/deepseek-ai/deepseek-v3.2',
            'alias': 'NIM-DeepSeekV3',
            'tier': 'MEDIUM',
            'cost_per_m': 0.40,
            'use_for': 'Code, complex tasks, good fallback',
            'latency_ms': 1500,
        },
    }
    
    def route(self, classification: Dict) -> Dict:
        """
        Select optimal model based on classification
        
        Returns:
            {
                'model': str,
                'provider': str,
                'model_id': str,
                'routing_reason': str,
                'fallback_chain': [str],
                'estimated_latency_ms': int,
                'cost_per_m': float
            }
        """
        suggested_model = classification['model_suggestion']
        
        if suggested_model not in self.MODEL_MAPPING:
            # Fallback to Qwen7B if unknown
            suggested_model = 'qwen7b'
        
        model_config = self.MODEL_MAPPING[suggested_model]
        
        # Define fallback chains based on tier
        fallback_chains = {
            'SIMPLE': ['qwen7b', 'deepseek_v3', 'sonnet'],
            'COMPLEX': ['sonnet', 'deepseek_v3', 'qwen7b'],
        }
        
        fallback_chain = fallback_chains.get(model_config['tier'], [suggested_model])
        
        return {
            'model': suggested_model,
            'provider': model_config['provider'],
            'model_id': model_config['model_id'],
            'alias': model_config['alias'],
            'tier': model_config['tier'],
            'routing_reason': f"Task classified as {classification['task_type']} (confidence: {classification['confidence']:.1%})",
            'fallback_chain': fallback_chain,
            'estimated_latency_ms': model_config['latency_ms'],
            'cost_per_m': model_config['cost_per_m'],
            'use_for': model_config['use_for']
        }


class SmartRouter:
    """Unified router combining classification and routing"""
    
    def __init__(self):
        self.classifier = TaskClassifier()
        self.router = ModelRouter()
    
    def process(self, text: str) -> Dict:
        """
        Process a request from text to model recommendation
        
        Returns:
            {
                'classification': {...},
                'routing': {...},
                'decision': {
                    'model': str,
                    'fallback': [str],
                    'rationale': str
                }
            }
        """
        classification = self.classifier.classify(text)
        routing = self.router.route(classification)
        
        return {
            'classification': classification,
            'routing': routing,
            'decision': {
                'model': routing['model'],
                'model_id': routing['model_id'],
                'fallback': routing['fallback_chain'][1:] if len(routing['fallback_chain']) > 1 else [],
                'rationale': routing['routing_reason']
            }
        }


# Example usage and testing
def test_routing():
    """Test the smart router with example inputs"""
    router = SmartRouter()
    
    test_cases = [
        "Accendi la luce della cucina",
        "Che ore sono?",
        "Che meteo fa oggi?",
        "Analizza la strategia di marketing per il lancio del nuovo prodotto",
        "Racconta una storia di fantasia",
        "Scrivi il codice per una funzione di autenticazione",
        "Ricetta per il risotto ai funghi",
        "Quando è la prossima riunione?",
    ]
    
    print("=" * 80)
    print("SMART ROUTER TEST")
    print("=" * 80)
    
    for test_input in test_cases:
        result = router.process(test_input)
        print(f"\n[INPUT] {test_input}")
        print(f"[TASK]  {result['classification']['task_type'].upper()}")
        print(f"[MODEL] {result['routing']['model']}")
        print(f"[CONF]  {result['classification']['confidence']:.1%} confidence")
        print(f"[REASON] {result['routing']['routing_reason']}")
        print(f"[FALLBACK] {' → '.join(result['decision']['fallback'][:2])}")


if __name__ == "__main__":
    test_routing()
