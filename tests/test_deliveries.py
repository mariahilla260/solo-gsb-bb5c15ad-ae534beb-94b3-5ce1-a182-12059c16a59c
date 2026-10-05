"""幂等投递判定（POST /deliveries、GET /deliveries/{id}）：

首次受理按当前策略判定并持久化；相同编号 + 相同规范化地址的并发 / 后续请求
原样重放首次判定（含原错误状态），即使策略随后改变；编号相同而地址不同 → 409；
无效地址与未配置策略不占用编号；选中版本失败整次失败，不切换版本。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

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
    # 候选里 blocked@ 命中拒收；候选无域兜底 → 其他地址 no_route
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


def submit(client, delivery_id, address):
    return client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})


# ---------- 首次受理：判定并持久化 ----------

def test_first_submit_returns_and_persists_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    resp = submit(client, "msg-1", "  Shared@Corp.Example.COM ")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_id"] == "msg-1"
    assert body["address"] == "shared@corp.example.com"  # 规范化地址
    assert body["policy_revision"] == 1
    assert body["baseline_version"] == v1 and body["candidate_version"] == v2
    assert body["candidate_percent"] == 100
    assert body["selection"] == "candidate"
    assert body["version"] == v2
    assert [e["mailbox"] for e in body["endpoints"]] == ["cand@mail.example.com"]
    # 与 /deliver 的判定一致（同一策略、同一地址）
    deliver = client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    assert body["endpoints"] == deliver["endpoints"]
    assert body["version"] == deliver["version"]
    # 判定已持久化，可按编号查询
    stored = client.get("/deliveries/msg-1").json()
    assert stored["status"] == "ok"
    assert stored["endpoints"] == body["endpoints"]
    assert stored["created_at"]


def test_replay_returns_identical_decision_even_after_policy_change(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit(client, "msg-2", "shared@corp.example.com")
    assert first.status_code == 200
    assert first.json()["version"] == v2
    # 策略随后改变：同一地址按新策略 /deliver 会落到基线
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    assert client.post("/deliver", json={"address": "shared@corp.example.com"}).json()["version"] == v1
    # 重放（地址写法不同、规范化后相同）仍取得完全相同的首次判定
    replay = submit(client, "msg-2", "SHARED@CORP.EXAMPLE.COM")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["version"] == v2
    assert replay.json()["policy_revision"] == 1


def test_failed_decision_persisted_and_replayed_with_original_error(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全部走候选；候选对 blocked@ 拒收
    first = submit(client, "msg-3", "blocked@corp.example.com")
    assert first.status_code == 422
    detail = first.json()["detail"]
    assert detail["error"] == "rejected"
    assert detail["delivery_id"] == "msg-3"
    assert detail["version"] == v2
    assert detail["selection"] == "candidate"
    assert detail["policy_revision"] == 1
    # 策略改为 0%：/deliver 按基线可成功，但重放仍是首次的失败判定与原错误状态
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    assert client.post("/deliver", json={"address": "blocked@corp.example.com"}).status_code == 200
    replay = submit(client, "msg-3", "Blocked@Corp.Example.Com")
    assert replay.status_code == 422
    assert replay.json() == first.json()
    # 持久化的失败判定可查
    stored = client.get("/deliveries/msg-3").json()
    assert stored["status"] == "failed"
    assert stored["error"]["error"] == "rejected"
    assert stored["endpoints"] is None


def test_no_route_failure_persisted_and_replayed(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 候选无域兜底 → 未知地址 no_route
    first = submit(client, "msg-4", "stranger@corp.example.com")
    assert first.status_code == 422
    assert first.json()["detail"]["error"] == "no_route"
    replay = submit(client, "msg-4", "stranger@corp.example.com")
    assert replay.status_code == 422
    assert replay.json() == first.json()


def test_selected_version_failure_does_not_switch_version(client):
    # 基线本可成功（域兜底），但选中候选失败时整次失败，不切换版本
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    resp = submit(client, "msg-5", "blocked@corp.example.com")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["version"] == v2
    assert detail["selection"] == "candidate"
    assert "endpoints" not in resp.json()


# ---------- 编号冲突 / 编号不被无效请求占用 ----------

def test_same_id_different_address_conflict(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    assert submit(client, "msg-6", "shared@corp.example.com").status_code == 200
    resp = submit(client, "msg-6", "other@corp.example.com")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "delivery_id_conflict"
    assert detail["delivery_id"] == "msg-6"
    # 冲突不改写原判定：原地址重放仍是首次结果
    again = submit(client, "msg-6", "shared@corp.example.com")
    assert again.status_code == 200
    assert again.json()["address"] == "shared@corp.example.com"


def test_conflict_also_applies_to_failed_decisions(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert submit(client, "msg-7", "blocked@corp.example.com").status_code == 422
    resp = submit(client, "msg-7", "shared@corp.example.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "delivery_id_conflict"


def test_invalid_address_does_not_consume_id(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    resp = submit(client, "msg-8", "not-an-address")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"
    assert client.get("/deliveries/msg-8").status_code == 404  # 编号未被占用
    ok = submit(client, "msg-8", "shared@corp.example.com")
    assert ok.status_code == 200
    assert ok.json()["delivery_id"] == "msg-8"


def test_missing_policy_does_not_consume_id(client):
    v1, v2 = publish_two_versions(client)
    resp = submit(client, "msg-9", "shared@corp.example.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    assert client.get("/deliveries/msg-9").status_code == 404  # 编号未被占用
    set_policy(client, 0, v1, v2, 100)
    ok = submit(client, "msg-9", "shared@corp.example.com")
    assert ok.status_code == 200
    assert ok.json()["version"] == v2


def test_empty_or_missing_delivery_id_rejected(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    assert submit(client, "", "shared@corp.example.com").status_code == 422
    assert client.post("/deliveries", json={"address": "shared@corp.example.com"}).status_code == 422
    assert client.post("/deliveries", json={"delivery_id": "msg-x"}).status_code == 422


# ---------- 并发：同一编号只产生一份判定 ----------

def test_concurrent_same_id_same_address_single_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: submit(client, "msg-10", "shared@corp.example.com"), range(16)
        ))
    assert all(r.status_code == 200 for r in responses)
    assert {r.text for r in responses} == {responses[0].text}  # 完全相同的首次判定


def test_concurrent_same_id_different_address_exactly_one_wins(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    addresses = ["shared@corp.example.com", "blocked@corp.example.com"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda a: submit(client, "msg-11", a), addresses))
    codes = sorted(r.status_code for r in responses)
    assert codes == [409, 422] or codes == [200, 409]  # 先受理者占号，后到者冲突
    # 最终只有一份判定，且与胜出的请求一致
    stored = client.get("/deliveries/msg-11")
    assert stored.status_code == 200
    winner = next(r for r in responses if r.status_code != 409)
    if winner.status_code == 200:
        assert stored.json()["endpoints"] == winner.json()["endpoints"]
    else:
        assert stored.json()["error"] == winner.json()["detail"]


# ---------- 查询与既有接口兼容 ----------

def test_get_delivery_unknown_id_404(client):
    resp = client.get("/deliveries/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "delivery_not_found"


def test_distinct_ids_have_independent_decisions(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit(client, "msg-12a", "shared@corp.example.com")
    second = submit(client, "msg-12b", "shared@corp.example.com")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["delivery_id"] != second.json()["delivery_id"]
    assert client.get("/deliveries/msg-12a").status_code == 200
    assert client.get("/deliveries/msg-12b").status_code == 200


def test_deliver_does_not_create_decision(client):
    # 既有 /deliver 不产生持久化判定，行为不变
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    resp = client.post("/deliver", json={"address": "shared@corp.example.com"})
    assert resp.status_code == 200
    assert "delivery_id" not in resp.json()
    assert client.get("/deliveries/shared@corp.example.com").status_code == 404


def test_explicit_resolve_unaffected_by_decisions(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "msg-13", "shared@corp.example.com")
    resp = client.post("/resolve", json={"address": "shared@corp.example.com", "version": v1})
    assert resp.status_code == 200
    assert [e["mailbox"] for e in resp.json()["endpoints"]] == ["base@mail.example.com"]
