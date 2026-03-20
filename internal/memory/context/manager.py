"""
Context Manager - Short-term working memory for session state
"""
import logging
import uuid
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

try:
    import redis.asyncio as redis
    from redis import Redis as RedisSync
except ImportError:
    redis = None


logger = logging.getLogger(__name__)


class ContextManager:
    """Redis-backed session context manager (short-term working memory)"""
    
    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        default_ttl_seconds: int = 3600,
    ):
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.default_ttl_seconds = default_ttl_seconds
        self.client = None

    async def connect(self):
        """Connect to Redis"""
        if redis is None:
            raise RuntimeError("redis not installed. Run: pip install redis")
        
        try:
            self.client = await redis.from_url(
                f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}",
                encoding="utf-8",
                decode_responses=True,
            )
            await self.client.ping()
            logger.info(f"Connected to Redis at {self.redis_host}:{self.redis_port}")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    async def disconnect(self):
        """Disconnect from Redis"""
        if self.client:
            await self.client.close()
            logger.info("Disconnected from Redis")

    async def create_session(
        self,
        session_id: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> str:
        """Create new session context"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        session_id = session_id or str(uuid.uuid4())
        ttl = ttl_seconds or self.default_ttl_seconds
        
        try:
            key = f"context:{session_id}"
            created_at = int(datetime.utcnow().timestamp() * 1000)
            
            await self.client.hset(
                key,
                mapping={
                    "created_at": created_at,
                    "last_updated": created_at,
                },
            )
            await self.client.expire(key, ttl)
            
            logger.debug(f"Created session {session_id} (TTL: {ttl}s)")
            return session_id
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    async def set_state(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
    ):
        """Set session state value"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            ttl = ttl_seconds or self.default_ttl_seconds
            
            # Store value
            await self.client.hset(session_key, key, str(value))
            await self.client.hset(session_key, "last_updated", int(datetime.utcnow().timestamp() * 1000))
            
            # Refresh TTL
            await self.client.expire(session_key, ttl)
            
            logger.debug(f"Set state {session_id}[{key}] = {value}")
        except Exception as e:
            logger.error(f"Failed to set state: {e}")
            raise

    async def get_state(
        self,
        session_id: str,
        key: str,
    ) -> Optional[str]:
        """Get session state value"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            value = await self.client.hget(session_key, key)
            return value
        except Exception as e:
            logger.error(f"Failed to get state: {e}")
            raise

    async def get_all_state(self, session_id: str) -> Dict[str, str]:
        """Get all session state"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            state = await self.client.hgetall(session_key)
            return state or {}
        except Exception as e:
            logger.error(f"Failed to get all state: {e}")
            raise

    async def delete_state(self, session_id: str, key: str):
        """Delete specific state key"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            await self.client.hdel(session_key, key)
            logger.debug(f"Deleted state {session_id}[{key}]")
        except Exception as e:
            logger.error(f"Failed to delete state: {e}")
            raise

    async def exists_session(self, session_id: str) -> bool:
        """Check if session exists"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            exists = await self.client.exists(session_key)
            return exists > 0
        except Exception as e:
            logger.error(f"Failed to check session existence: {e}")
            raise

    async def delete_session(self, session_id: str):
        """Delete entire session"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            await self.client.delete(session_key)
            logger.debug(f"Deleted session {session_id}")
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            raise

    async def extend_session_ttl(
        self,
        session_id: str,
        ttl_seconds: Optional[int] = None,
    ):
        """Extend session TTL"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        ttl = ttl_seconds or self.default_ttl_seconds
        
        try:
            session_key = f"context:{session_id}"
            await self.client.expire(session_key, ttl)
            logger.debug(f"Extended session {session_id} TTL to {ttl}s")
        except Exception as e:
            logger.error(f"Failed to extend session TTL: {e}")
            raise

    async def increment_counter(
        self,
        session_id: str,
        counter_name: str,
        amount: int = 1,
    ) -> int:
        """Increment numeric counter in session"""
        if not self.client:
            raise RuntimeError("Not connected to Redis")
        
        try:
            session_key = f"context:{session_id}"
            new_value = await self.client.hincrby(session_key, counter_name, amount)
            await self.client.hset(session_key, "last_updated", int(datetime.utcnow().timestamp() * 1000))
            return new_value
        except Exception as e:
            logger.error(f"Failed to increment counter: {e}")
            raise
