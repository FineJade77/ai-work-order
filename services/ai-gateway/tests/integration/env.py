import os

# The runtime account: not the owner, so RLS applies as it does in production.
DATABASE_URL = os.environ.get(
    "AI_GATEWAY_DATABASE_URL", "postgresql+psycopg://ai_gateway_app:ai_gateway_app_dev@localhost:25432/ai_gateway"
)
# Superuser on the ai_gateway database: bypasses RLS, used only for fixtures and cleanup.
ADMIN_DB_URL = os.environ.get("AI_GATEWAY_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/ai_gateway")
