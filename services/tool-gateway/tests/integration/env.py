import os

GATEWAY_URL = os.environ.get("TOOL_GATEWAY_URL", "http://localhost:8003")
SERVICE_TOKEN = os.environ.get("TOOL_GATEWAY_SERVICE_TOKEN", "dev-tool-gateway-token")
ADMIN_DB_URL = os.environ.get("ADMIN_DATABASE_URL", "postgresql://work_order:work_order@localhost:25432/work_order")
ROLE = "it_support_agent"
ONCALL_ANSWER = "夜间请联系IT值班 13812345678"
