import re
from typing import Any

MASK = "***"

SENSITIVE_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "authorization", "credential"}
)

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CN_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_CN_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def mask_secret_fields(value: Any) -> Any:
    """For tool results: blank out credential-like fields, leave text untouched.

    Result text is shown to the employee, and regex PII masking here would also hide
    contact details they need (e.g. the IT on-call number in a knowledge answer).
    """
    return _walk(value, mask_pii_text=False)


def redact_for_audit(value: Any) -> Any:
    """For audit storage: credential fields plus phone/email/ID-card numbers in any text,
    since tool arguments carry free text typed by employees."""
    return _walk(value, mask_pii_text=True)


def _walk(value: Any, *, mask_pii_text: bool) -> Any:
    if isinstance(value, dict):
        return {
            k: MASK if k.lower() in SENSITIVE_KEYS else _walk(v, mask_pii_text=mask_pii_text)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_walk(v, mask_pii_text=mask_pii_text) for v in value]
    if isinstance(value, str) and mask_pii_text:
        return _mask_pii(value)
    return value


def _mask_pii(text: str) -> str:
    # ID card before mobile, so an 18-digit ID is masked whole rather than as a partial match.
    text = _CN_ID_CARD.sub(r"\1********\2", text)
    text = _CN_MOBILE.sub(r"\1****\2", text)
    return _EMAIL.sub(r"\1***\2", text)
