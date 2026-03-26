"""
Screensaver Controller for AtomS3R device
Handles idle detection and cycles through various display content
"""

import asyncio
import logging
import json
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from enum import Enum
import random

logger = logging.getLogger(__name__)


class DisplayMode(Enum):
    """Display content types"""
    QUOTE = "quote"
    WEATHER = "weather"
    CRYPTO = "crypto"
    EVENTS = "events"
    CLOCK = "clock"
    CUSTOM = "custom"


class ScreensaverController:
    """Controls AtomS3R screensaver with idle detection"""
    
    # Configuration
    IDLE_THRESHOLD_SECONDS = 5 * 60  # 5 minutes
    SCREEN_OFF_THRESHOLD_SECONDS = 30 * 60  # 30 minutes
    REFRESH_INTERVAL_SECONDS = 30  # Update display every 30s
    
    # Sample content pools
    QUOTES = [
        "La vita è bella. — Italian saying",
        "Chi ha fretta vada piano. — Italian proverb",
        "Meglio tardi che mai. — Italian proverb",
        "La dolce vita. — Italian lifestyle",
        "Fatto bene è fatto due volte. — Italian proverb",
        "L'arte di vivere bene. — Italian wisdom",
        "Quando la gatta non c'è, i topi ballano. — Italian proverb",
        "Non è tutt'oro quel che luccica. — Italian proverb",
    ]
    
    DISPLAY_MODES = [
        DisplayMode.QUOTE,
        DisplayMode.WEATHER,
        DisplayMode.CRYPTO,
        DisplayMode.EVENTS,
        DisplayMode.CLOCK,
    ]
    
    # Required Home Assistant entities
    HA_ENTITIES = {
        'weather': {
            'entity_id': 'weather.home',
            'description': 'Main weather entity',
        },
        'crypto_btc': {
            'entity_id': 'sensor.cryptocurrency_bitcoin',
            'description': 'Bitcoin price sensor',
        },
        'crypto_eth': {
            'entity_id': 'sensor.cryptocurrency_ethereum',
            'description': 'Ethereum price sensor',
        },
        'calendar_events': {
            'entity_id': 'calendar.family_calendar',
            'description': 'Family calendar for upcoming events',
        },
        'display': {
            'entity_id': 'display.atoms3r_screen',
            'description': 'AtomS3R display entity',
        },
    }
    
    def __init__(self, location_id: str = "wagmi"):
        """Initialize screensaver controller"""
        self.location_id = location_id
        self.is_active = False
        self.is_screen_on = False
        self.last_activity_time = datetime.now()
        self.current_mode = DisplayMode.CLOCK
        self.mode_index = 0
        self.activity_callback = None
        logger.info("ScreensaverController initialized")
    
    async def start(self):
        """Start the screensaver monitoring loop"""
        logger.info("Starting screensaver monitor")
        self.is_active = True
        self.is_screen_on = True
        
        try:
            while self.is_active:
                await self._update_display()
                await asyncio.sleep(self.REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Screensaver monitor cancelled")
            await self.stop()
    
    async def stop(self):
        """Stop the screensaver"""
        logger.info("Stopping screensaver")
        self.is_active = False
        if self.is_screen_on:
            await self._turn_off_screen()
    
    async def register_activity(self):
        """Register user activity to reset idle timer"""
        was_idle = self._is_idle()
        self.last_activity_time = datetime.now()
        
        if was_idle:
            logger.info("Activity detected, resuming normal operation")
            await self._exit_screensaver()
    
    async def _update_display(self):
        """Update the display based on idle state"""
        time_since_activity = self._get_idle_time()
        
        # Check idle thresholds
        if time_since_activity >= self.SCREEN_OFF_THRESHOLD_SECONDS:
            if self.is_screen_on:
                logger.info(f"Device idle for {self.SCREEN_OFF_THRESHOLD_SECONDS}s, turning off screen")
                await self._turn_off_screen()
            return
        
        if time_since_activity >= self.IDLE_THRESHOLD_SECONDS:
            if not self.is_screen_on:
                logger.info("Activity resumed, turning on screen")
                await self._turn_on_screen()
            
            # Update screensaver content
            await self._cycle_display()
        else:
            # Normal operation - show clock or last content
            if self.is_screen_on:
                await self._show_clock()
    
    async def _cycle_display(self):
        """Cycle through screensaver display modes"""
        self.current_mode = self.DISPLAY_MODES[self.mode_index % len(self.DISPLAY_MODES)]
        self.mode_index += 1
        
        content = await self._get_content_for_mode(self.current_mode)
        await self._render_to_display(content)
    
    async def _get_content_for_mode(self, mode: DisplayMode) -> Dict:
        """Get content to display for a given mode"""
        if mode == DisplayMode.QUOTE:
            return self._get_quote()
        elif mode == DisplayMode.WEATHER:
            return await self._get_weather()
        elif mode == DisplayMode.CRYPTO:
            return await self._get_crypto_prices()
        elif mode == DisplayMode.EVENTS:
            return await self._get_upcoming_events()
        elif mode == DisplayMode.CLOCK:
            return self._get_clock()
        else:
            return self._get_clock()
    
    def _get_quote(self) -> Dict:
        """Get a random quote"""
        quote = random.choice(self.QUOTES)
        return {
            'type': 'quote',
            'content': quote,
            'icon': '💭',
            'timestamp': datetime.now().isoformat()
        }
    
    async def _get_weather(self) -> Dict:
        """Get current weather (mock, would call HA)"""
        # In real implementation, would fetch from Home Assistant
        return {
            'type': 'weather',
            'content': '☀️ 22°C - Soleggiato',
            'icon': '🌤️',
            'details': 'Umidità: 60% | Vento: 10 km/h',
            'timestamp': datetime.now().isoformat()
        }
    
    async def _get_crypto_prices(self) -> Dict:
        """Get cryptocurrency prices (mock, would call HA)"""
        # In real implementation, would fetch from sensors
        btc_price = f"${random.randint(40000, 50000)}"
        eth_price = f"${random.randint(2000, 3000)}"
        
        return {
            'type': 'crypto',
            'content': f"₿ {btc_price} | Ξ {eth_price}",
            'icon': '💰',
            'timestamp': datetime.now().isoformat()
        }
    
    async def _get_upcoming_events(self) -> Dict:
        """Get upcoming calendar events (mock, would call HA)"""
        # In real implementation, would fetch from calendar
        events = [
            "Riunione Marco 14:00",
            "Cena famiglia 19:00",
            "Film con Ada 21:00",
        ]
        
        return {
            'type': 'events',
            'content': random.choice(events),
            'icon': '📅',
            'timestamp': datetime.now().isoformat()
        }
    
    def _get_clock(self) -> Dict:
        """Get current time"""
        now = datetime.now()
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%d %b").upper()
        
        return {
            'type': 'clock',
            'time': time_str,
            'date': date_str,
            'icon': '🕐',
            'timestamp': now.isoformat()
        }
    
    async def _render_to_display(self, content: Dict):
        """Render content to AtomS3R display"""
        try:
            # In real implementation, would call:
            # curl -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/display" \
            #   -d '{"location": "atoms3r", "content": content}'
            
            logger.info(f"Rendering to display: {content['type']}")
            
            # Mock: just log the content
            if content['type'] == 'clock':
                display_text = f"{content['time']}\n{content['date']}"
            elif content['type'] == 'quote':
                display_text = content['content']
            elif content['type'] == 'weather':
                display_text = content['content']
            elif content['type'] == 'crypto':
                display_text = content['content']
            elif content['type'] == 'events':
                display_text = content['content']
            else:
                display_text = str(content)
            
            logger.debug(f"Display output: {display_text}")
            
        except Exception as e:
            logger.error(f"Failed to render to display: {e}")
    
    async def _show_clock(self):
        """Show clock during normal operation"""
        content = self._get_clock()
        await self._render_to_display(content)
    
    async def _turn_on_screen(self):
        """Turn on the display"""
        self.is_screen_on = True
        logger.info("Screen turned ON")
        # Would call HA: homeassistant.turn_on(entity_id='display.atoms3r_screen')
    
    async def _turn_off_screen(self):
        """Turn off the display"""
        self.is_screen_on = False
        logger.info("Screen turned OFF")
        # Would call HA: homeassistant.turn_off(entity_id='display.atoms3r_screen')
    
    async def _exit_screensaver(self):
        """Exit screensaver and return to normal operation"""
        logger.info("Exiting screensaver")
        self.current_mode = DisplayMode.CLOCK
        self.mode_index = 0
    
    def _is_idle(self) -> bool:
        """Check if device is idle"""
        return self._get_idle_time() >= self.IDLE_THRESHOLD_SECONDS
    
    def _get_idle_time(self) -> int:
        """Get seconds since last activity"""
        return int((datetime.now() - self.last_activity_time).total_seconds())
    
    def get_status(self) -> Dict:
        """Get screensaver status"""
        idle_time = self._get_idle_time()
        
        return {
            'is_active': self.is_active,
            'is_screen_on': self.is_screen_on,
            'is_idle': self._is_idle(),
            'idle_time_seconds': idle_time,
            'idle_threshold_seconds': self.IDLE_THRESHOLD_SECONDS,
            'screen_off_threshold_seconds': self.SCREEN_OFF_THRESHOLD_SECONDS,
            'current_mode': self.current_mode.value if self._is_idle() else 'normal',
            'last_activity': self.last_activity_time.isoformat(),
        }


class ScreensaverManager:
    """Manages screensaver instance and activity tracking"""
    
    def __init__(self, location_id: str = "wagmi"):
        self.screensaver = ScreensaverController(location_id)
        self.monitor_task = None
    
    async def start_monitor(self):
        """Start the screensaver monitor"""
        if self.monitor_task is None:
            self.monitor_task = asyncio.create_task(self.screensaver.start())
            logger.info("Screensaver monitor started")
    
    async def stop_monitor(self):
        """Stop the screensaver monitor"""
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
            logger.info("Screensaver monitor stopped")
    
    async def on_user_activity(self):
        """Called when user activity is detected"""
        await self.screensaver.register_activity()
    
    def get_status(self) -> Dict:
        """Get screensaver status"""
        return self.screensaver.get_status()


async def test_screensaver():
    """Test the screensaver"""
    manager = ScreensaverManager()
    
    print("=" * 80)
    print("SCREENSAVER ATOMSR TEST")
    print("=" * 80)
    
    # Start screensaver
    await manager.start_monitor()
    
    # Simulate some activity
    print("\n[0s] Device started, registering activity...")
    await manager.on_user_activity()
    print(f"Status: {manager.get_status()}")
    
    # Wait 3 seconds
    await asyncio.sleep(3)
    
    # Show status
    print("\n[3s] Waiting for idle...")
    status = manager.get_status()
    print(f"Idle time: {status['idle_time_seconds']}s")
    
    # Stop monitor
    await manager.stop_monitor()
    print("\n[DONE] Screensaver stopped")


if __name__ == "__main__":
    asyncio.run(test_screensaver())
