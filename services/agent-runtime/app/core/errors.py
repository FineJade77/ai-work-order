class RetryLater(Exception):
    """The task cannot make progress right now. It stays unacknowledged and is redelivered
    once it has been idle for claim_idle_seconds."""
