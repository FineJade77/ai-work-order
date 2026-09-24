from typing import Any

from jsonschema import Draft202012Validator

from app.tools.base import ToolSpec
from app.tools.knowledge import SEARCH_KNOWLEDGE
from app.tools.tickets import CREATE_TICKET

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in (SEARCH_KNOWLEDGE, CREATE_TICKET)}

_VALIDATORS: dict[str, Draft202012Validator] = {}
for _spec in REGISTRY.values():
    Draft202012Validator.check_schema(_spec.parameters)
    _VALIDATORS[_spec.name] = Draft202012Validator(_spec.parameters)


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Returns a model-readable description of the first schema violation, or None if valid."""
    errors = sorted(_VALIDATORS[tool_name].iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    first = errors[0]
    path = "/".join(str(p) for p in first.absolute_path) or "(root)"
    return f"{path}: {first.message}"
