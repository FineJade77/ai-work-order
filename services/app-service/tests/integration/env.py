import os

DATABASE_URL = os.environ.get(
    "APP_SERVICE_DATABASE_URL", "postgresql+psycopg://app_service_app:app_service_app_dev@localhost:25432/app_service"
)
# Database 13, not 0: the compose services use database 0 for agent_tasks and agent_results.
REDIS_URL = os.environ.get("APP_SERVICE_TEST_REDIS_URL", "redis://localhost:26379/13")
TICKET_SERVICE_URL = os.environ.get("TICKET_SERVICE_URL", "http://localhost:8006")
TICKET_SERVICE_TOKEN = os.environ.get("TICKET_SERVICE_TOKEN", "dev-ticket-service-token")
# Superuser connections: bypass RLS, used only for fixtures, assertions and cleanup.
ADMIN_DB_URL = os.environ.get("APP_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/app_service")
TICKET_ADMIN_DB_URL = os.environ.get(
    "TICKET_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/ticket_service"
)
PASSWORD = "integration-password"
