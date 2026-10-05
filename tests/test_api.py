import os
import tempfile

# 必须在导入 app 之前设置，config 在导入时读取
os.environ["ROUTER_DB"] = os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db")

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_attempts;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM circuit_breaker_settings;"
        "DELETE FROM circuit_breaks;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "DELETE FROM draft_revisions;"
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at)"
        " VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


# 一套典型规则：精确优先、域兜底、多分支、别名链、终点去重、拒收
RULES = [
    {
        "match_type": "exact",
        "match_key": "boss@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "alias", "value": "assistant@corp.example.com"},
            {"kind": "mailbox", "value": "boss-backup@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "assistant@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "alias", "value": "team@corp.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "team@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},  # 与 assistant 直发重复 → 去重
            {"kind": "mailbox", "value": "bob@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
    {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}],
    },
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def setup_published(client, rules=RULES):
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


# ---------- 草稿编辑与修订号 ----------

def test_draft_starts_empty(client):
    resp = client.get("/draft")
    assert resp.status_code == 200
    assert resp.json() == {"revision": 0, "rules": []}


def test_put_draft_and_revision_increment(client):
    resp = put_rules(client, 0, RULES)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    draft = client.get("/draft").json()
    assert draft["revision"] == 1
    assert len(draft["rules"]) == len(RULES)


def test_put_draft_stale_revision_conflict(client):
    assert put_rules(client, 0, RULES).status_code == 200
    resp = put_rules(client, 0, RULES)  # 旧修订号
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_revision"] == 1


def test_put_draft_wrong_revision_rejected(client):
    resp = put_rules(client, 5, RULES)
    assert resp.status_code == 409


# ---------- 编辑期校验：无效地址 / 重复规则 / 空目标 ----------

def test_invalid_exact_address_rejected(client):
    bad = [{"match_type": "exact", "match_key": "not-an-address", "action": "reject"}]
    resp = put_rules(client, 0, bad)
    assert resp.status_code == 422


def test_invalid_domain_rejected(client):
    bad = [{"match_type": "domain", "match_key": "corp@example.com", "action": "reject"}]
    resp = put_rules(client, 0, bad)
    assert resp.status_code == 422


def test_duplicate_rule_rejected(client):
    dup = [
        {"match_type": "exact", "match_key": "a@corp.example.com", "action": "reject"},
        {"match_type": "exact", "match_key": "A@corp.example.com", "action": "reject"},  # 大小写归一后重复
    ]
    resp = put_rules(client, 0, dup)
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["detail"]["message"]


def test_empty_targets_rejected(client):
    bad = [{"match_type": "exact", "match_key": "a@corp.example.com", "action": "route", "targets": []}]
    resp = put_rules(client, 0, bad)
    assert resp.status_code == 422
    assert "empty targets" in resp.json()["detail"]["message"]


def test_reject_rule_with_targets_rejected(client):
    bad = [
        {
            "match_type": "exact",
            "match_key": "a@corp.example.com",
            "action": "reject",
            "targets": [{"kind": "mailbox", "value": "x@mail.example.com"}],
        }
    ]
    resp = put_rules(client, 0, bad)
    assert resp.status_code == 422


def test_invalid_target_address_rejected(client):
    bad = [
        {
            "match_type": "exact",
            "match_key": "a@corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "broken"}],
        }
    ]
    resp = put_rules(client, 0, bad)
    assert resp.status_code == 422


# ---------- 发布：原子校验，失败保持旧版 ----------

def test_publish_success_creates_immutable_version(client):
    assert put_rules(client, 0, RULES).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["source_revision"] == 1
    assert body["rule_count"] == len(RULES)


def test_publish_stale_revision_conflict(client):
    assert put_rules(client, 0, RULES).status_code == 200
    resp = publish(client, 0)
    assert resp.status_code == 409
    assert client.get("/versions").json()["versions"] == []


def test_publish_unresolvable_alias_rejected(client):
    rules = [
        {
            "match_type": "exact",
            "match_key": "a@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "ghost@corp.example.com"}],  # 无任何规则
        }
    ]
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["problems"][0]["error"] == "no_route"
    assert client.get("/versions").json()["versions"] == []


def test_publish_cycle_rejected(client):
    rules = [
        {
            "match_type": "exact",
            "match_key": "a@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "b@corp.example.com"}],
        },
        {
            "match_type": "exact",
            "match_key": "b@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "a@corp.example.com"}],
        },
    ]
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["problems"][0]["error"] == "cycle"
    assert client.get("/versions").json()["versions"] == []


def test_publish_domain_fallback_cycle_rejected(client):
    rules = [
        {
            "match_type": "domain",
            "match_key": "one.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "u@two.example.com"}],
        },
        {
            "match_type": "domain",
            "match_key": "two.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "v@one.example.com"}],
        },
    ]
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["problems"][0]["error"] == "cycle"


def test_failed_publish_keeps_previous_version(client):
    version = setup_published(client)
    # 草稿改成有环，发布失败
    cyclic = [
        {
            "match_type": "exact",
            "match_key": "a@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "a@corp.example.com"}],
        }
    ]
    assert put_rules(client, 1, cyclic).status_code == 200
    assert publish(client, 2).status_code == 422
    # 旧版本仍在且可复现原结果
    versions = client.get("/versions").json()["versions"]
    assert [v["version"] for v in versions] == [version]
    resp = client.post("/resolve", json={"address": "boss@corp.example.com", "version": version})
    assert resp.status_code == 200


def test_publish_with_reject_rule_allowed(client):
    # 拒收规则是合法配置，可以发布；只在解析命中时失败
    version = setup_published(client)
    assert version == 1


# ---------- 解析：逐跳、去重、路径、整次失败 ----------

def test_resolve_expands_branches_and_dedupes(client):
    version = setup_published(client)
    resp = client.post("/resolve", json={"address": "boss@corp.example.com", "version": version})
    assert resp.status_code == 200
    endpoints = {e["mailbox"]: e["paths"] for e in resp.json()["endpoints"]}
    assert set(endpoints) == {
        "boss-backup@mail.example.com",
        "alice@mail.example.com",
        "bob@mail.example.com",
    }
    # alice 经两条路径到达，终点去重但路径都保留
    assert endpoints["alice@mail.example.com"] == [
        ["boss@corp.example.com", "assistant@corp.example.com", "alice@mail.example.com"],
        ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "alice@mail.example.com"],
    ]
    assert endpoints["boss-backup@mail.example.com"] == [
        ["boss@corp.example.com", "boss-backup@mail.example.com"]
    ]
    assert endpoints["bob@mail.example.com"] == [
        ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "bob@mail.example.com"]
    ]


def test_resolve_domain_fallback(client):
    version = setup_published(client)
    resp = client.post("/resolve", json={"address": "nobody@corp.example.com", "version": version})
    assert resp.status_code == 200
    assert resp.json()["endpoints"] == [
        {"mailbox": "catchall@mail.example.com", "paths": [["nobody@corp.example.com", "catchall@mail.example.com"]]}
    ]


def test_resolve_exact_takes_precedence_over_domain(client):
    version = setup_published(client)
    resp = client.post("/resolve", json={"address": "team@corp.example.com", "version": version})
    assert resp.status_code == 200
    mailboxes = {e["mailbox"] for e in resp.json()["endpoints"]}
    assert mailboxes == {"alice@mail.example.com", "bob@mail.example.com"}  # 未落到域兜底


def test_resolve_rejected_branch_fails_whole_request(client):
    version = setup_published(client)
    resp = client.post("/resolve", json={"address": "blocked@corp.example.com", "version": version})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "rejected"
    assert "endpoints" not in resp.json()


def test_resolve_invalid_address(client):
    version = setup_published(client)
    resp = client.post("/resolve", json={"address": "nope", "version": version})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"


def test_resolve_unknown_version_404(client):
    resp = client.post("/resolve", json={"address": "a@corp.example.com", "version": 99})
    assert resp.status_code == 404


# ---------- 历史版本可查询、可复现 ----------

def test_historical_versions_reproduce_original_results(client):
    v1 = setup_published(client)
    # 修改草稿并发布 v2：boss 直接指向新终点
    new_rules = [
        {
            "match_type": "exact",
            "match_key": "boss@corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "new-boss@mail.example.com"}],
        }
    ]
    assert put_rules(client, 1, new_rules).status_code == 200
    resp = publish(client, 2)
    assert resp.status_code == 200
    v2 = resp.json()["version"]
    assert v2 == v1 + 1

    # v1 仍是旧规则的结果
    r1 = client.post("/resolve", json={"address": "boss@corp.example.com", "version": v1}).json()
    assert {e["mailbox"] for e in r1["endpoints"]} == {
        "boss-backup@mail.example.com",
        "alice@mail.example.com",
        "bob@mail.example.com",
    }
    # v2 是新规则的结果
    r2 = client.post("/resolve", json={"address": "boss@corp.example.com", "version": v2}).json()
    assert {e["mailbox"] for e in r2["endpoints"]} == {"new-boss@mail.example.com"}

    # 历史版本内容可查询
    v1_doc = client.get(f"/versions/{v1}").json()
    assert v1_doc["source_revision"] == 1
    assert len(v1_doc["rules"]) == len(RULES)
    listing = client.get("/versions").json()["versions"]
    assert [v["version"] for v in listing] == [v1, v2]


def test_get_unknown_version_404(client):
    assert client.get("/versions/42").status_code == 404
