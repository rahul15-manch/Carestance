import redis
import hashlib
import json
import os
from dotenv import load_dotenv

load_dotenv()

class RedisCache:
    """
    A simple Redis cache utility for AI responses.
    """
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        try:
            # Add timeouts to prevent hanging the event loop on remote connection failures
            self.client = redis.from_url(
                self.redis_url, 
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
                retry_on_timeout=True
            )
            self.client.ping()
            self.is_available = True
        except Exception as e:
            print(f"REDIS ERROR: Connection failed. {e}")
            self.is_available = False

    def _get_hash(self, text: str) -> str:
        """Generates a SHA-256 hash of the input text."""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def get(self, prompt: str) -> str:
        """Retrieves cached content for a prompt."""
        if not self.is_available:
            return None
        
        try:
            key = f"ai_cache:{self._get_hash(prompt)}"
            return self.client.get(key)
        except Exception as e:
            print(f"REDIS GET ERROR: {e}")
            return None

    def set(self, prompt: str, response: str, ttl: int = 86400):
        """Caches a response for a prompt (default TTL 24h)."""
        if not self.is_available:
            return
        
        try:
            key = f"ai_cache:{self._get_hash(prompt)}"
            self.client.setex(key, ttl, response)
        except Exception as e:
            print(f"REDIS SET ERROR: {e}")

# Global instance
ai_cache = RedisCache()
