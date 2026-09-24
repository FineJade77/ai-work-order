import os

import redis

redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1.0, socket_connect_timeout=1.0)
