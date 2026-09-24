from app.domain.matching import KnowledgeItem, normalize, rank, score

VPN_CERT = KnowledgeItem("vpn-002", "vpn", "VPN 提示证书错误", "证书可能已过期，请更新证书。")
VPN_DOWN = KnowledgeItem("vpn-001", "vpn", "VPN 连不上怎么办？", "请重启客户端后重试。")
MAIL = KnowledgeItem("email-001", "email", "邮箱无法收发邮件", "请检查客户端配置。")
ITEMS = [VPN_CERT, VPN_DOWN, MAIL]


def test_normalize_ignores_case_spaces_and_punctuation():
    assert normalize(" VPN，连 不上？") == "vpn连不上"


def test_query_contained_in_question_scores_one():
    assert score("vpn 连不上", VPN_DOWN) == 1.0


def test_partial_overlap_ranks_the_closer_question_first():
    hits = rank("vpn 证书过期了", ITEMS, limit=5)
    assert [h.item.id for h in hits][0] == "vpn-002"
    assert all(h.item.id != "email-001" for h in hits)


def test_unrelated_query_returns_nothing():
    assert rank("打印机卡纸", ITEMS, limit=5) == []


def test_limit_is_respected_and_ties_break_by_id():
    hits = rank("vpn", ITEMS, limit=1)
    assert [h.item.id for h in hits] == ["vpn-001"]


def test_blank_query_scores_zero():
    assert score("？？", VPN_DOWN) == 0.0
