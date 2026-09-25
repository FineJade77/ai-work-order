import os

GATEWAY_URL = os.environ.get("TOOL_GATEWAY_URL", "http://localhost:8003")
SERVICE_TOKEN = os.environ.get("TOOL_GATEWAY_SERVICE_TOKEN", "dev-tool-gateway-token")
# Superuser connections: bypass RLS, used only for fixtures and assertions.
GATEWAY_ADMIN_DB_URL = os.environ.get(
    "TOOL_GATEWAY_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/tool_gateway"
)
TICKET_ADMIN_DB_URL = os.environ.get(
    "TICKET_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/ticket_service"
)
ROLE = "service_agent"
# Tenant with static data in knowledge-service (services/knowledge-service/app/data/knowledge.json).
DEMO_TENANT = "e0e112f3-5afb-4229-869b-c0bd012324bb"
ONCALL_ANSWER = "夜间请拨打 IT 值班电话 400-800-1234，或在企业微信搜索“IT 值班”。"
REDIS_URL = os.environ.get("TOOL_GATEWAY_TEST_REDIS_URL", "redis://localhost:26379/0")  # the stream the gateway publishes to
