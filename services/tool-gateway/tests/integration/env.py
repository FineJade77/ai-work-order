import os

GATEWAY_URL = os.environ.get("TOOL_GATEWAY_URL", "http://localhost:8003")
SERVICE_TOKEN = os.environ.get("TOOL_GATEWAY_SERVICE_TOKEN", "dev-tool-gateway-token")
# Superuser connections: bypass RLS, used only for fixtures and assertions.
# create_ticket goes through app-service, so tickets are asserted in app-service's database.
GATEWAY_ADMIN_DB_URL = os.environ.get(
    "TOOL_GATEWAY_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/tool_gateway"
)
APP_SERVICE_ADMIN_DB_URL = os.environ.get(
    "APP_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/app_service"
)
ROLE = "it_support_agent"
ONCALL_ANSWER = "夜间请联系IT值班 13812345678"
