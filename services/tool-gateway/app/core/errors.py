class CallerError(Exception):
    """Agent Runtime broke the gateway contract. Returned as an HTTP error, never shown to the model."""

    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
