import uuid

from sqlalchemy import text

from shared.db.models import Tenant
from shared.db.session import admin_session_factory, app_session_factory, tenant_scope

TENANT_NAME = "Demo Tenant"


def main() -> None:
    AdminSession = admin_session_factory()
    with AdminSession() as session:
        tenant = session.query(Tenant).filter_by(name=TENANT_NAME).one()
        tenant_id = tenant.id

    AppSession = app_session_factory()

    with AppSession() as session, tenant_scope(session, str(tenant_id)):
        count_own = session.execute(text("SELECT count(*) FROM knowledge_items")).scalar_one()

    other_tenant_id = uuid.uuid4()
    with AppSession() as session, tenant_scope(session, str(other_tenant_id)):
        count_other = session.execute(text("SELECT count(*) FROM knowledge_items")).scalar_one()

    print(f"own tenant ({tenant_id}) knowledge_items visible: {count_own}")
    print(f"other tenant ({other_tenant_id}) knowledge_items visible: {count_other}")

    assert count_own > 0, "expected seeded tenant to see its own knowledge_items"
    assert count_other == 0, "RLS leak: a different tenant_id should see zero rows"
    print("RLS verification PASSED")


if __name__ == "__main__":
    main()
