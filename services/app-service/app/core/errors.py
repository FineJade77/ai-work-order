class ApiError(Exception):
    """A request the service refuses, with the HTTP status and a stable machine-readable code."""

    def __init__(self, http_status: int, code: str, message: str, *, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.headers = headers  # e.g. Retry-After on a 429
