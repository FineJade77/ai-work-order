from app.core.redaction import mask_secret_fields, redact_for_audit

TEXT = "夜间请联系IT值班 13812345678，或发邮件到 it-help@example.com，身份证 110101199003071234"


def test_results_keep_contact_details_but_mask_secret_fields():
    masked = mask_secret_fields({"answer": TEXT, "api_key": "sk-123", "nested": [{"Password": "p"}]})
    assert masked["answer"] == TEXT
    assert masked["api_key"] == "***"
    assert masked["nested"][0]["Password"] == "***"


def test_audit_masks_phone_email_and_id_card():
    masked = redact_for_audit({"summary": TEXT})["summary"]
    assert "13812345678" not in masked and "138****5678" in masked
    assert "it-help@example.com" not in masked and "i***@example.com" in masked
    assert "110101199003071234" not in masked and "110101********1234" in masked


def test_audit_masks_secret_fields_too():
    assert redact_for_audit({"token": "abc"}) == {"token": "***"}


def test_uuid_is_not_mistaken_for_a_phone_number():
    ticket_id = "e5c07787-dc98-433b-929b-138123456789"
    assert redact_for_audit({"ticket_id": ticket_id}) == {"ticket_id": ticket_id}
