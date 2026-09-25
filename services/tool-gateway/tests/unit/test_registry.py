from app.tools.registry import REGISTRY, validate_arguments


def test_registry_holds_the_v1_tools():
    assert set(REGISTRY) == {"search_knowledge", "create_ticket", "grant_access"}


def test_openai_tool_exposes_only_name_description_parameters():
    for spec in REGISTRY.values():
        tool = spec.to_openai_tool()
        assert tool["type"] == "function"
        assert set(tool["function"]) == {"name", "description", "parameters"}


def test_create_ticket_schema_hides_runtime_context_from_the_model():
    props = REGISTRY["create_ticket"].parameters["properties"]
    assert set(props) == {"summary", "category"}


def test_valid_arguments_pass():
    assert validate_arguments("create_ticket", {"summary": "VPN 连不上", "category": "vpn"}) is None


def test_category_is_any_tenants_own_label_within_bounds():
    # Not a fixed list: an HR tenant's categories are as valid as an IT tenant's.
    for category in ("vpn", "leave", "expense", "other"):
        assert validate_arguments("create_ticket", {"summary": "s", "category": category}) is None
    for category in ("", "x" * 51):
        assert validate_arguments("create_ticket", {"summary": "s", "category": category}).startswith("category:")


def test_unexpected_property_is_rejected():
    assert validate_arguments("search_knowledge", {"query": "VPN", "extra": 1}) is not None


def test_missing_required_property_is_rejected():
    assert "summary" in validate_arguments("create_ticket", {"category": "vpn"})
