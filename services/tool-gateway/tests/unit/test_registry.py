from app.tools.registry import REGISTRY, validate_arguments


def test_registry_holds_the_v1_tools():
    assert set(REGISTRY) == {"search_knowledge", "create_ticket"}


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


def test_enum_violation_names_the_field_and_allowed_values():
    message = validate_arguments("create_ticket", {"summary": "打印机坏了", "category": "printer"})
    assert message.startswith("category:")
    assert "'vpn'" in message


def test_unexpected_property_is_rejected():
    assert validate_arguments("search_knowledge", {"query": "VPN", "extra": 1}) is not None


def test_missing_required_property_is_rejected():
    assert "summary" in validate_arguments("create_ticket", {"category": "vpn"})
