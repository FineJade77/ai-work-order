import os

APP_SERVICE_URL = os.environ.get("APP_SERVICE_URL", "http://localhost:8001")
INTERNAL_TOKEN = os.environ.get("APP_SERVICE_INTERNAL_TOKEN", "dev-app-service-token")
# Superuser on the app_service database: bypasses RLS, used only for fixtures and assertions.
ADMIN_DB_URL = os.environ.get("APP_SERVICE_ADMIN_DB_URL", "postgresql://work_order:work_order@localhost:25432/app_service")
