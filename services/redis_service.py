import redis
import json
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_client: Optional[redis.Redis] = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
    return _client


# ── Style Memory ──────────────────────────────────────────────────────────────

def save_style(user_id: str, style: dict) -> None:
    get_client().set(f"style:{user_id}", json.dumps(style))


def get_style(user_id: str) -> Optional[dict]:
    data = get_client().get(f"style:{user_id}")
    return json.loads(data) if data else None


# ── Job State ─────────────────────────────────────────────────────────────────

def create_job(job_id: str, brief: dict) -> None:
    get_client().hset(
        f"job:{job_id}",
        mapping={
            "status": "pending",
            "brief": json.dumps(brief),
            "intel": "",
            "script": "",
            "images": "",
        },
    )
    get_client().expire(f"job:{job_id}", 86400)  # 24h TTL


def update_job(job_id: str, field: str, data: dict | str) -> None:
    value = json.dumps(data) if isinstance(data, dict) else data
    get_client().hset(f"job:{job_id}", field, value)


def get_job(job_id: str) -> Optional[dict]:
    job = get_client().hgetall(f"job:{job_id}")
    if not job:
        return None
    return {k.decode(): v.decode() for k, v in job.items()}


# ── Intel Cache ───────────────────────────────────────────────────────────────

def cache_intel(topic_key: str, data: dict, ttl: int = 3600) -> None:
    """Cache intel results for 1 hour by default — same topic won't re-search."""
    get_client().setex(f"intel:{topic_key}", ttl, json.dumps(data))


def get_cached_intel(topic_key: str) -> Optional[dict]:
    cached = get_client().get(f"intel:{topic_key}")
    return json.loads(cached) if cached else None


def make_topic_key(topic: str) -> str:
    """Normalize topic string into a safe cache key."""
    return topic.lower().strip().replace(" ", "_")[:60]