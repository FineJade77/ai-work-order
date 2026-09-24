"""Local demo data. Run with the owner account:
docker compose run --rm tool-gateway-migrate python scripts/seed.py
"""

import os

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import KnowledgeItem, RoleToolBinding
from app.db.session import tenant_scope

# Must match app-service's seed; there is no cross-database FK to enforce it.
DEMO_TENANT_ID = "e0e112f3-5afb-4229-869b-c0bd012324bb"
AGENT_ROLE = "it_support_agent"
ROLE_TOOLS = ["search_knowledge", "create_ticket"]

SCENARIOS: dict[str, list[tuple[str, str]]] = {
    "vpn": [
        ("VPN 连不上怎么办？", "请先确认本机网络正常，然后检查 VPN 客户端版本是否为最新版，重启客户端后重试。"),
        ("VPN 提示证书错误", "证书可能已过期，请在 VPN 客户端设置中点击“更新证书”，或重新下载安装最新客户端。"),
        ("VPN 连接后无法访问内网系统", "请确认连接的是正确的 VPN 线路（生产/办公网），并检查目标系统是否在您所在线路的访问范围内。"),
    ],
    "account": [
        ("忘记密码怎么办？", "请前往企业统一登录页面点击“忘记密码”，通过绑定的手机号或邮箱验证后重置密码。"),
        ("账号被锁定怎么解锁？", "连续输错密码超过 5 次会自动锁定 30 分钟，也可联系 IT 立即解锁。"),
        ("多次输错密码提示账号异常", "这是账号保护机制触发，请等待 30 分钟后重试，或通过“忘记密码”流程重置。"),
    ],
    "email": [
        ("邮箱无法收发邮件", "请检查网络连接与邮箱客户端账号配置是否正确，必要时重新输入授权码登录。"),
        ("邮箱提示存储空间已满", "请清理垃圾邮件和大附件邮件以释放空间，或联系 IT 申请扩容。"),
        ("如何在手机上配置公司邮箱", "在手机邮件App中选择“Exchange”账号类型，服务器地址、用户名密码与电脑端一致。"),
    ],
}


def main() -> None:
    engine = create_engine(os.environ["MIGRATION_DATABASE_URL"])
    # The owner account is subject to FORCE ROW LEVEL SECURITY too, so seed inside a tenant scope.
    with Session(engine) as session, tenant_scope(session, DEMO_TENANT_ID):
        has_knowledge = session.execute(select(KnowledgeItem.id).limit(1)).first() is not None
        if not has_knowledge:
            for tag, pairs in SCENARIOS.items():
                for question, answer in pairs:
                    session.add(KnowledgeItem(tenant_id=DEMO_TENANT_ID, question=question, answer=answer, tags=[tag]))
        for tool_name in ROLE_TOOLS:
            session.execute(
                pg_insert(RoleToolBinding)
                .values(tenant_id=DEMO_TENANT_ID, agent_role=AGENT_ROLE, tool_name=tool_name)
                .on_conflict_do_nothing(index_elements=["tenant_id", "agent_role", "tool_name"])
            )
        session.commit()
    print(f"tenant_id={DEMO_TENANT_ID} role={AGENT_ROLE} tools={ROLE_TOOLS}")


if __name__ == "__main__":
    main()
