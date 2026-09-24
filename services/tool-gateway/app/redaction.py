import re
from typing import Any

MASK = "***"

SENSITIVE_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "authorization", "credential"}
)

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CN_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_CN_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def redact(value: Any, extra_keys: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, dict):
        return {
            k: MASK if (k.lower() in SENSITIVE_KEYS or k in extra_keys) else redact(v, extra_keys)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, extra_keys) for v in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _mask_text(text: str) -> str:
    # ID card before mobile: an 18-digit ID must be masked as a whole, not have an
    # 11-digit substring picked off first.
    text = _CN_ID_CARD.sub(r"\1********\2", text)
    text = _CN_MOBILE.sub(r"\1****\2", text)
    return _EMAIL.sub(r"\1***\2", text)
