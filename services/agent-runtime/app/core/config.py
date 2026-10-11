import socket
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str
    # Plain psycopg conninfo (postgresql://...), as the non-owner runtime account.
    checkpoint_database_url: str
    tool_gateway_url: str
    tool_gateway_service_token: str
    ai_gateway_url: str
    ai_gateway_service_token: str

    # Must match the role bound to the tools in tool-gateway's role_tool_bindings.
    agent_role: str = "service_agent"
    # Tasks run concurrently within one process; they spend nearly all their time waiting on I/O.
    worker_concurrency: int = 8
    # Must be task_timeout < conversation_lock < claim_idle (checked below):
    # - the lock outlives any live run, so two runs never share a conversation;
    # - a task reclaimed from a killed worker finds that worker's lock already expired, instead of
    #   bouncing off it and burning one of its max_deliveries.
    # claim_idle also exceeds tool-gateway's STALE_RUNNING_AFTER (60s), so a reclaimed task finds an
    # orphaned 'running' tool call already stale and re-runs it instead of getting IN_PROGRESS.
    task_timeout_seconds: float = 60
    conversation_lock_seconds: float = 75
    claim_idle_seconds: float = 90
    max_deliveries: int = 3
    # Tenants are served in turn (worker/queue.py). At most this many tasks of one tenant run at
    # once, across all replicas, so a tenant whose tasks are slow cannot occupy every worker; 0 for
    # no limit. A tenant's own limit and weight are set in Redis (agent_sched:max_concurrency,
    # agent_sched:weight). A slot is a lease: it must outlive any run (checked below), and it is how
    # the slots of a killed worker come back.
    tenant_max_concurrency: int = Field(default=8, ge=0)
    tenant_lease_seconds: float = 75
    # On SIGTERM, how long the runs in flight may take to finish before they are cut off and handed
    # back. Above task_timeout_seconds, so a run is only ever cut off if it is stuck past its own
    # timeout. The orchestrator must wait longer than this before SIGKILL (compose
    # stop_grace_period, k8s terminationGracePeriodSeconds: both 90s).
    shutdown_drain_seconds: float = 70
    consumer_name: str = Field(default_factory=socket.gethostname)

    @model_validator(mode="after")
    def _check_timing(self) -> "Settings":
        if not self.task_timeout_seconds < self.conversation_lock_seconds < self.claim_idle_seconds:
            raise ValueError("need task_timeout_seconds < conversation_lock_seconds < claim_idle_seconds")
        if not self.task_timeout_seconds < self.tenant_lease_seconds:
            raise ValueError("need task_timeout_seconds < tenant_lease_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
