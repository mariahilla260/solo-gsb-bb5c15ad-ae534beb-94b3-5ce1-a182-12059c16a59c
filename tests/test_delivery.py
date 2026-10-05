"""默认投递策略（PUT /policy、POST /deliver）：稳定灰度分桶与失败不切换。"""
import os
import tempfile

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.traffic import bucket_for, select_version


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


BASE_RULES = [
    {
        "match_type": "exact",
        "match_key": "shared@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "base@mail.example.com"}],
    },
    {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "base-catchall@mail.example.com"}],
    },
]

CANDIDATE_RULES = [
    {
        "match_type": "exact",
        "match_key": "shared@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "cand@mail.example.com"}],
    },
    # 候选里这些地址命中拒收 / 无路由（无域兜底 → 其他地址 no_route）
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def publish_two_versions(client):
    assert put_rules(client, 0, BASE_RULES).status_code == 200
    assert publish(client, 1).status_code == 200
    assert put_rules(client, 1, CANDIDATE_RULES).status_code == 200
    resp = publish(client, 2)
    assert resp.status_code == 200, resp.text
    return 1, 2


def set_policy(client, revision, baseline, candidate, percent):
    return client.put("/policy", json={
        "revision": revision,
        "baseline_version": baseline,
        "candidate_version": candidate,
        "candidate_percent": percent,
    })


# ---------- 分桶本身：稳定、确定、边界 ----------

def test_bucket_is_stable_and_in_range():
    for addr in ["a@corp.example.com", "A@Corp.Example.COM ", "x@y.example.com"]:
        normalized = addr.strip().lower()
        assert 0 <= bucket_for(normalized) < 100
        assert bucket_for(normalized) == bucket_for(normalized)


def test_select_zero_and_hundred_percent():
    assert select_version("a@x.example.com", 0) == "baseline"
    assert select_version("a@x.example.com", 100) == "candidate"


def test_select_is_monotonic_in_percent():
    # 比例上升时选择只能从基线变成候选，不会来回抖动
    addr = "someone@corp.example.com"
    selections = [select_version(addr, p) for p in range(101)]
    assert selections.count("baseline") + selections.count("candidate") == 101
    flip = next(p for p in range(101) if selections[p] == "candidate")
    assert all(s == "candidate" for s in selections[flip:])


# ---------- 策略配置 ----------

def test_policy_get_before_configured_is_404(client):
    resp = client.get("/policy")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    assert resp.json()["detail"]["revision"] == 0


def test_put_policy_creates_with_revision_one(client):
    v1, v2 = publish_two_versions(client)
    resp = set_policy(client, 0, v1, v2, 50)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["baseline_version"] == v1
    assert body["candidate_version"] == v2
    assert body["candidate_percent"] == 50
    assert "updated_at" in body

    got = client.get("/policy").json()
    assert got["revision"] == 1
    assert got["baseline_version"] == v1


def test_put_policy_requires_published_versions_and_keeps_old(client):
    v1, v2 = publish_two_versions(client)
    assert set_policy(client, 0, v1, v2, 10).status_code == 200
    # 引用不存在的版本 → 422，原策略不变
    resp = set_policy(client, 1, v1, 999, 50)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "version_not_published"
    assert resp.json()["detail"]["version"] == 999
    remaining = client.get("/policy").json()
    assert remaining["candidate_version"] == v2
    assert remaining["candidate_percent"] == 10
    assert remaining["revision"] == 1


def test_put_policy_stale_revision_conflict_and_no_overwrite(client):
    v1, v2 = publish_two_versions(client)
    assert set_policy(client, 0, v1, v2, 10).status_code == 200
    resp = set_policy(client, 0, v1, v2, 50)  # 仍带初始修订号
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 1
    # 原策略未被覆盖
    assert client.get("/policy").json()["candidate_percent"] == 10


def test_policy_percent_must_be_0_to_100(client):
    v1, v2 = publish_two_versions(client)
    assert set_policy(client, 0, v1, v2, 101).status_code == 422
    assert set_policy(client, 0, v1, v2, -1).status_code == 422
    assert client.get("/policy").status_code == 404


def test_policy_updates_revision_increments(client):
    v1, v2 = publish_two_versions(client)
    assert set_policy(client, 0, v1, v2, 0).json()["revision"] == 1
    assert set_policy(client, 1, v2, v1, 100).json()["revision"] == 2
    body = client.get("/policy").json()
    assert body["revision"] == 2
    assert body["baseline_version"] == v2
    assert body["candidate_version"] == v1
    assert body["candidate_percent"] == 100


def test_policy_revision_independent_of_draft_revision(client):
    # 策略变更不改动草稿与历史版本
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    draft = client.get("/draft").json()
    assert draft["revision"] == 2  # 未因策略更新而自增
    versions = [v["version"] for v in client.get("/versions").json()["versions"]]
    assert versions == [v1, v2]
    assert client.get(f"/versions/{v1}").status_code == 200


# ---------- 默认投递 ----------

def test_deliver_without_policy_is_error_not_fallback(client):
    publish_two_versions(client)
    resp = client.post("/deliver", json={"address": "shared@corp.example.com"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    # 不使用草稿或最新版本：草稿此刻是 CANDIDATE_RULES，绝不能按它/按 v2 成功返回
    assert "endpoints" not in resp.json()


def test_deliver_zero_percent_always_baseline(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    for addr in ["shared@corp.example.com", "nobody@corp.example.com", "Z@corp.example.com"]:
        resp = client.post("/deliver", json={"address": addr})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["selection"] == "baseline"
        assert body["version"] == v1
        assert body["policy_revision"] == 1
        assert body["baseline_version"] == v1 and body["candidate_version"] == v2


def test_deliver_hundred_percent_always_candidate(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    resp = client.post("/deliver", json={"address": "shared@corp.example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["selection"] == "candidate"
    assert body["version"] == v2
    assert [e["mailbox"] for e in body["endpoints"]] == ["cand@mail.example.com"]


def test_deliver_stable_same_address_same_version(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    first = client.post("/deliver", json={"address": "shared@corp.example.com"})
    assert first.status_code == 200
    for _ in range(5):
        again = client.post("/deliver", json={"address": "shared@corp.example.com"})
        assert again.status_code == 200
        assert again.json()["version"] == first.json()["version"]
        assert again.json()["selection"] == first.json()["selection"]


def test_deliver_address_normalized_before_bucketing(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    ref = client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    variant = client.post("/deliver", json={"address": "  SHARED@CORP.EXAMPLE.COM  "}).json()
    assert variant["address"] == "shared@corp.example.com"
    assert variant["version"] == ref["version"]
    assert variant["selection"] == ref["selection"]
    # 终点与路径为所选版本的原始结果
    assert variant["endpoints"] == ref["endpoints"]


def test_deliver_50_actually_splits_traffic(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    picks = {"baseline": 0, "candidate": 0}
    # sharedN@ 两侧都无精确规则：基线命中域兜底成功，候选无域规则 no_route
    for i in range(300):
        resp = client.post("/deliver", json={"address": f"shared{i}@corp.example.com"})
        if resp.status_code == 200:
            picks[resp.json()["selection"]] += 1
        else:
            assert resp.status_code == 422
            assert resp.json()["detail"]["selection"] == "candidate"
            picks["candidate"] += 1
    assert picks["candidate"] > 50
    assert picks["baseline"] > 50


def test_deliver_candidate_failure_does_not_fallback_to_baseline(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    # 拒收：候选 blocked@ 拒收；基线本可按域兜底成功，但不得自动改用基线
    resp = client.post("/deliver", json={"address": "blocked@corp.example.com"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "rejected"
    assert detail["version"] == v2
    assert detail["selection"] == "candidate"
    assert detail["policy_revision"] == 1
    # 无路由：候选没有域兜底
    resp2 = client.post("/deliver", json={"address": "stranger@corp.example.com"})
    assert resp2.status_code == 422
    detail2 = resp2.json()["detail"]
    assert detail2["error"] == "no_route"
    assert detail2["version"] == v2
    assert "endpoints" not in resp2.json()


def test_deliver_baseline_failure_does_not_switch_to_candidate(client):
    v1, v2 = publish_two_versions(client)
    # 基线 v1、候选 v2、0%：blocked@ 在基线上命中域兜底正常，不构成失败；
    # 改为基线 v1 的 shared 正常。构造一个基线失败地址：v1 对 unknown 域无规则
    set_policy(client, 0, v1, v2, 0)
    resp = client.post("/deliver", json={"address": "ghost@other.example.net"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "no_route"
    assert detail["version"] == v1
    assert detail["selection"] == "baseline"


def test_deliver_invalid_address(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    resp = client.post("/deliver", json={"address": "not-an-address"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"


def test_deliver_uses_policy_snapshot_stable_after_policy_change(client):
    # 同一地址同一策略稳定；改比例后允许按新策略重选（“同一策略下”才要求稳定）
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    before = client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    assert before["version"] == v1
    set_policy(client, 1, v1, v2, 100)
    after = client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    assert after["version"] == v2
    assert after["policy_revision"] == 2


def test_explicit_resolve_still_compatible(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    # 显式版本解析不受策略影响：仍需传 version，按指定版本返回
    resp = client.post("/resolve", json={"address": "shared@corp.example.com", "version": v1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == v1
    assert [e["mailbox"] for e in body["endpoints"]] == ["base@mail.example.com"]
    # 显式传不存在版本仍是 404 原约定
    assert client.post("/resolve", json={"address": "shared@corp.example.com", "version": 99}).status_code == 404
