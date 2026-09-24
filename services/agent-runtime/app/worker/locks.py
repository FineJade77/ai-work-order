import uuid

from redis.asyncio import Redis

_RELEASE_IF_OWNER = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class ConversationLock:
    """One run per conversation at a time: two runs on one thread would race on its checkpoint.

    app-service is expected to refuse a new message while one is still being processed; this
    is the backstop. A killed holder never releases it; the TTL frees it (see Settings).
    """

    def __init__(self, redis: Redis, conversation_id: uuid.UUID, ttl_seconds: float):
        self._redis = redis
        self._key = f"lock:conversation:{conversation_id}"
        self._token = uuid.uuid4().hex
        self._ttl_ms = int(ttl_seconds * 1000)

    async def acquire(self) -> bool:
        return bool(await self._redis.set(self._key, self._token, nx=True, px=self._ttl_ms))

    async def release(self) -> None:
        await self._redis.eval(_RELEASE_IF_OWNER, 1, self._key, self._token)
