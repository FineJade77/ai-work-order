import os

# Database 15, not 0: a compose agent-runtime worker consumes agent_tasks on database 0.
REDIS_URL = os.environ.get("AGENT_RUNTIME_TEST_REDIS_URL", "redis://localhost:26379/15")
CHECKPOINT_DB_URL = os.environ.get(
    "AGENT_RUNTIME_CHECKPOINT_DB_URL", "postgresql://agent_runtime_app:agent_runtime_app_dev@localhost:25432/agent_runtime"
)
TOOL_GATEWAY_URL = os.environ.get("TOOL_GATEWAY_URL", "http://localhost:8003")
TOOL_GATEWAY_TOKEN = os.environ.get("TOOL_GATEWAY_SERVICE_TOKEN", "dev-tool-gateway-token")
# Superuser connections: bypass RLS, used only for assertions and cleanup.
_ADMIN = "postgresql://work_order:work_order@localhost:25432"
CHECKPOINT_ADMIN_DB_URL = os.environ.get("AGENT_RUNTIME_ADMIN_DB_URL", f"{_ADMIN}/agent_runtime")
GATEWAY_ADMIN_DB_URL = os.environ.get("TOOL_GATEWAY_ADMIN_DB_URL", f"{_ADMIN}/tool_gateway")
TICKET_ADMIN_DB_URL = os.environ.get("TICKET_SERVICE_ADMIN_DB_URL", f"{_ADMIN}/ticket_service")
