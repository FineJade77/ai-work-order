import os

TICKET_SERVICE_URL = os.environ.get("TICKET_SERVICE_URL", "http://localhost:8006")
SERVICE_TOKEN = os.environ.get("TICKET_SERVICE_TOKEN", "dev-ticket-service-token")
# Superuser on the ticket_service database: bypasses RLS, used only for cleanup and assertions.
ADMIN_DB_URL = os.environ.get(
    "TICKET_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/ticket_service"
)
