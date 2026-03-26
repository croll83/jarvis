"""
Sub-Agent Orchestrator for jarvis-orchestrator
Spawns specialized sub-agents for complex tasks and manages their lifecycle
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SubAgentTask:
    """Represents a sub-agent task"""
    task_id: str
    task_type: str  # 'summarize', 'analyze', 'generate'
    content: str
    user_id: str = "marco"
    priority: str = "normal"  # normal, high, critical
    timeout_seconds: int = 300
    fallback_on_failure: bool = True
    created_at: str = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()


class SubAgentSpawner:
    """Spawns and manages sub-agents for delegated tasks"""
    
    AGENT_TEMPLATES = {
        'summarize_long': {
            'label': 'summarizer-{task_id}',
            'model': 'anthropic-proxy-2/claude-sonnet-4-5',
            'instruction': 'You are a concise summarizer. Extract key points and create a brief summary.',
            'max_tokens': 2000,
            'timeout_seconds': 120,
        },
        'analyze_data': {
            'label': 'analyzer-{task_id}',
            'model': 'nvidia-nim/deepseek-ai/deepseek-v3.2',
            'instruction': 'You are a data analyst. Analyze the provided data and generate insights.',
            'max_tokens': 3000,
            'timeout_seconds': 180,
        },
        'generate_content': {
            'label': 'generator-{task_id}',
            'model': 'anthropic-proxy-2/claude-sonnet-4-5',
            'instruction': 'You are a creative content generator. Create engaging, original content.',
            'max_tokens': 4000,
            'timeout_seconds': 240,
        },
        'extract_insights': {
            'label': 'extractor-{task_id}',
            'model': 'nvidia-nim/deepseek-ai/deepseek-v3.2',
            'instruction': 'You are an insight extractor. Find patterns and meaningful connections.',
            'max_tokens': 2500,
            'timeout_seconds': 150,
        },
    }
    
    def __init__(self):
        """Initialize sub-agent spawner"""
        self.active_agents: Dict[str, Dict] = {}
        self.completed_tasks: Dict[str, Dict] = {}
        self.failed_tasks: Dict[str, Dict] = {}
        logger.info("Sub-Agent Spawner initialized")
    
    async def spawn(
        self,
        task: SubAgentTask,
        task_type: str,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Spawn a sub-agent for a task
        
        Args:
            task: SubAgentTask instance
            task_type: Type of sub-agent ('summarize_long', 'analyze_data', etc.)
        
        Returns:
            (success, session_key, error_message)
        """
        try:
            template = self.AGENT_TEMPLATES.get(task_type)
            if not template:
                logger.error(f"Unknown task type: {task_type}")
                return False, "", f"Unknown task type: {task_type}"
            
            # Build task prompt
            task_prompt = self._build_task_prompt(task, task_type, template)
            
            logger.info(f"Spawning sub-agent for {task_type}: {task.task_id}")
            
            # In real implementation, this would call sessions_spawn
            # For now, we'll mock it and return a session key
            session_key = f"subagent:{task_type}:{task.task_id}"
            
            self.active_agents[task.task_id] = {
                'session_key': session_key,
                'task_type': task_type,
                'status': 'spawned',
                'created_at': datetime.now(timezone.utc).isoformat(),
                'timeout_seconds': task.timeout_seconds,
            }
            
            logger.info(f"Sub-agent spawned: {session_key}")
            return True, session_key, None
            
        except Exception as e:
            logger.error(f"Failed to spawn sub-agent: {e}")
            return False, "", str(e)
    
    def _build_task_prompt(
        self,
        task: SubAgentTask,
        task_type: str,
        template: Dict
    ) -> str:
        """Build the task prompt for the sub-agent"""
        instruction = template['instruction']
        
        prompts = {
            'summarize_long': f"""
{instruction}

Content to summarize:
---
{task.content}
---

Provide a concise summary of the key points. Keep it under 500 words.
""",
            'analyze_data': f"""
{instruction}

Data to analyze:
---
{task.content}
---

Analyze this data and provide:
1. Key insights
2. Patterns and trends
3. Recommendations
""",
            'generate_content': f"""
{instruction}

Content request:
---
{task.content}
---

Generate creative, original content based on the request above.
""",
            'extract_insights': f"""
{instruction}

Content to analyze:
---
{task.content}
---

Extract meaningful insights, patterns, and connections from this content.
""",
        }
        
        return prompts.get(task_type, task.content)
    
    async def wait_completion(
        self,
        task_id: str,
        timeout_seconds: Optional[int] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Wait for sub-agent completion
        
        Returns:
            (success, result, error)
        """
        if task_id not in self.active_agents:
            return False, None, f"Task {task_id} not found"
        
        agent_info = self.active_agents[task_id]
        timeout = timeout_seconds or agent_info.get('timeout_seconds', 300)
        
        start_time = time.time()
        poll_interval = 1  # seconds
        
        while time.time() - start_time < timeout:
            # In real implementation, would poll sessions_list
            # For now, mock a completion after 2 seconds
            if time.time() - start_time > 2:
                result = f"[Mock Result for {task_id}] Task completed successfully"
                self.active_agents[task_id]['status'] = 'completed'
                self.completed_tasks[task_id] = {
                    'task': agent_info,
                    'result': result,
                    'completed_at': datetime.now(timezone.utc).isoformat()
                }
                return True, result, None
            
            await asyncio.sleep(poll_interval)
        
        # Timeout
        error = f"Sub-agent task {task_id} timed out after {timeout}s"
        self.active_agents[task_id]['status'] = 'timeout'
        self.failed_tasks[task_id] = {
            'task': agent_info,
            'error': error,
            'failed_at': datetime.now(timezone.utc).isoformat()
        }
        logger.error(error)
        return False, None, error


class ComplexTaskHandler:
    """Handles complex tasks by delegating to sub-agents with fallback"""
    
    TASK_ROUTING = {
        'summarize_newsletter': {
            'sub_agent_type': 'summarize_long',
            'priority': 'normal',
            'timeout_seconds': 120,
            'fallback_strategy': 'degraded_summary',
        },
        'summarize_article': {
            'sub_agent_type': 'summarize_long',
            'priority': 'normal',
            'timeout_seconds': 120,
            'fallback_strategy': 'extractive_summary',
        },
        'analyze_energy_consumption': {
            'sub_agent_type': 'analyze_data',
            'priority': 'high',
            'timeout_seconds': 180,
            'fallback_strategy': 'basic_stats',
        },
        'analyze_spending_patterns': {
            'sub_agent_type': 'analyze_data',
            'priority': 'high',
            'timeout_seconds': 180,
            'fallback_strategy': 'simple_report',
        },
        'generate_story': {
            'sub_agent_type': 'generate_content',
            'priority': 'normal',
            'timeout_seconds': 240,
            'fallback_strategy': 'story_outline',
        },
        'generate_script': {
            'sub_agent_type': 'generate_content',
            'priority': 'normal',
            'timeout_seconds': 240,
            'fallback_strategy': 'script_outline',
        },
        'extract_trends': {
            'sub_agent_type': 'extract_insights',
            'priority': 'high',
            'timeout_seconds': 150,
            'fallback_strategy': 'keyword_extraction',
        },
    }
    
    def __init__(self):
        """Initialize complex task handler"""
        self.spawner = SubAgentSpawner()
        self.fallback_handlers = {
            'degraded_summary': self._fallback_degraded_summary,
            'extractive_summary': self._fallback_extractive_summary,
            'basic_stats': self._fallback_basic_stats,
            'simple_report': self._fallback_simple_report,
            'story_outline': self._fallback_story_outline,
            'script_outline': self._fallback_script_outline,
            'keyword_extraction': self._fallback_keyword_extraction,
        }
    
    async def handle(
        self,
        task_type: str,
        content: str,
        user_id: str = "marco"
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Handle a complex task with sub-agent + fallback
        
        Args:
            task_type: Type of complex task
            content: Task content
            user_id: User requesting the task
        
        Returns:
            (success, result, error)
        """
        if task_type not in self.TASK_ROUTING:
            return False, None, f"Unknown task type: {task_type}"
        
        routing = self.TASK_ROUTING[task_type]
        
        # Create task
        import uuid
        task_id = f"{task_type}-{str(uuid.uuid4())[:8]}"
        task = SubAgentTask(
            task_id=task_id,
            task_type=task_type,
            content=content,
            user_id=user_id,
            priority=routing['priority'],
            timeout_seconds=routing['timeout_seconds'],
        )
        
        logger.info(f"[{task_id}] Handling complex task: {task_type}")
        
        # Spawn sub-agent
        success, session_key, error = await self.spawner.spawn(
            task,
            routing['sub_agent_type']
        )
        
        if not success:
            logger.warning(f"[{task_id}] Failed to spawn sub-agent, falling back")
            return await self._apply_fallback(
                task_type,
                content,
                routing['fallback_strategy'],
                error
            )
        
        # Wait for completion with timeout
        success, result, error = await self.spawner.wait_completion(
            task_id,
            timeout_seconds=routing['timeout_seconds']
        )
        
        if not success:
            logger.warning(f"[{task_id}] Sub-agent task failed, applying fallback")
            return await self._apply_fallback(
                task_type,
                content,
                routing['fallback_strategy'],
                error
            )
        
        logger.info(f"[{task_id}] Task completed successfully")
        return True, result, None
    
    async def _apply_fallback(
        self,
        task_type: str,
        content: str,
        strategy: str,
        error: Optional[str]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Apply fallback strategy when sub-agent fails"""
        logger.info(f"Applying fallback strategy: {strategy}")
        
        handler = self.fallback_handlers.get(strategy)
        if not handler:
            error_msg = f"No fallback handler for {strategy}"
            logger.error(error_msg)
            return False, None, error_msg
        
        result = await handler(content)
        return True, result, f"Using fallback ({strategy})"
    
    # Fallback handlers
    async def _fallback_degraded_summary(self, content: str) -> str:
        """Return first 500 chars as degraded summary"""
        return f"[Degraded Summary]\n{content[:500]}..."
    
    async def _fallback_extractive_summary(self, content: str) -> str:
        """Extract first sentences from content"""
        sentences = content.split('.')[:3]
        return ". ".join(sentences) + "."
    
    async def _fallback_basic_stats(self, content: str) -> str:
        """Return basic statistics about content"""
        lines = content.split('\n')
        return f"[Basic Stats]\nTotal lines: {len(lines)}\nTotal chars: {len(content)}"
    
    async def _fallback_simple_report(self, content: str) -> str:
        """Return simple report of content"""
        words = len(content.split())
        return f"[Simple Report]\nContent contains {words} words. Manual analysis recommended."
    
    async def _fallback_story_outline(self, content: str) -> str:
        """Return story outline placeholder"""
        return f"[Story Outline]\nBeginning: Introduce characters and setting\nMiddle: Develop conflict\nEnd: Resolve and conclude"
    
    async def _fallback_script_outline(self, content: str) -> str:
        """Return script outline placeholder"""
        return f"[Script Outline]\nScene 1: Setup\nScene 2: Conflict\nScene 3: Resolution"
    
    async def _fallback_keyword_extraction(self, content: str) -> str:
        """Extract keywords from content"""
        words = content.split()[:10]
        return f"[Keywords]\n" + ", ".join(words)


async def test_sub_agent_orchestration():
    """Test sub-agent orchestration"""
    handler = ComplexTaskHandler()
    
    test_cases = [
        ('summarize_article', 'Questo è un articolo molto lungo che parla di ' * 20 + '...'),
        ('analyze_energy_consumption', 'Gennaio: 150 kWh, Febbraio: 140 kWh, Marzo: 130 kWh'),
        ('generate_story', 'Una storia su un avventuriero in una foresta misteriosa'),
    ]
    
    print("=" * 80)
    print("SUB-AGENT ORCHESTRATOR TEST")
    print("=" * 80)
    
    for task_type, content in test_cases:
        print(f"\n[TASK] {task_type}")
        success, result, error = await handler.handle(task_type, content)
        print(f"[SUCCESS] {success}")
        if result:
            print(f"[RESULT] {result[:200]}...")
        if error:
            print(f"[ERROR] {error}")


if __name__ == "__main__":
    asyncio.run(test_sub_agent_orchestration())
