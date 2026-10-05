"""批量投递判定（POST /deliveries/batch）：

一次提交多笔收件判定（非空、单批至多 100 笔），返回与输入顺序一致的逐项判定，
成功含终点路径，路由失败含原错误详情及状态。整批先校验：无效地址、批内重复编号、
既有编号绑定其他规范化地址、尚无默认策略且存在新编号——任一不满足整批拒绝且不写入。
已有同编号同地址的判定原样重放；新判定在同一事务中共用受理时的策略修订号与比例，
分别稳定分桶、解析并持久化（路由失败仍保存）；全部写入后仅评估一次该修订号的熔断。
"""
import os
import tempfile

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


def submit_batch(client, items):
    return client.post("/deliveries/batch", json={"items": items})


def item(delivery_id, address):
    return {"delivery_id": delivery_id, "address": address}


# ---------- 首次受理：逐项判定、顺序一致、失败含原错误详情 ----------

def test_batch_mixed_results_in_input_order(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全部走候选
    resp = submit_batch(client, [
        item("b-1", "  Shared@Corp.Example.COM "),   # 候选成功
        item("b-2", "blocked@corp.example.com"),     # 候选拒收
        item("b-3", "stranger@corp.example.com"),    # 候选无域兜底 → no_route
    ])
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["delivery_id"] for r in results] == ["b-1", "b-2", "b-3"]  # 与输入顺序一致

    ok = results[0]
    assert ok["status"] == "ok"
    assert ok["address"] == "shared@corp.example.com"  # 规范化地址
    assert ok["selection"] == "candidate" and ok["version"] == v2
    assert ok["policy_revision"] == 1 and ok["candidate_percent"] == 100
    assert [e["mailbox"] for e in ok["endpoints"]] == ["cand@mail.example.com"]
    assert ok["error"] is None

    rejected = results[1]
    assert rejected["status"] == "failed"
    assert rejected["endpoints"] is None
    assert rejected["error"]["error"] == "rejected"
    assert rejected["error"]["delivery_id"] == "b-2"  # 原错误详情（同单笔 422 detail）
    assert rejected["error"]["version"] == v2 and rejected["error"]["selection"] == "candidate"

    no_route = results[2]
    assert no_route["status"] == "failed"
    assert no_route["error"]["error"] == "no_route"

    # 每笔都已持久化，可按编号查询（含失败判定）
    for r in results:
        stored = client.get(f"/deliveries/{r['delivery_id']}")
        assert stored.status_code == 200
        assert stored.json()["status"] == r["status"]


def test_batch_new_items_share_acceptance_policy(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    resp = submit_batch(client, [item(f"shr-{i}", f"user{i}@corp.example.com") for i in range(5)])
    assert resp.status_code == 200
    results = resp.json()["results"]
    # 同批新判定共用受理时的策略修订号与比例
    assert {r["policy_revision"] for r in results} == {1}
    assert {r["candidate_percent"] for r in results} == {50}
    assert {r["baseline_version"] for r in results} == {v1}
    assert {r["candidate_version"] for r in results} == {v2}


# ---------- 重放与混合批 ----------

def test_batch_replay_identical_even_after_policy_change(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit_batch(client, [item("r-1", "shared@corp.example.com"), item("r-2", "blocked@corp.example.com")])
    assert first.status_code == 200
    assert set_policy(client, 1, v1, v2, 0).status_code == 200  # 策略随后改变
    replay = submit_batch(client, [item("r-1", "SHARED@CORP.EXAMPLE.COM"), item("r-2", "Blocked@Corp.Example.Com")])
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 整批原样重放
    assert replay.json()["results"][0]["policy_revision"] == 1


def test_batch_mixed_replay_and_new_items(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert submit_batch(client, [item("m-1", "shared@corp.example.com")]).status_code == 200
    assert set_policy(client, 1, v1, v2, 0).status_code == 200  # 新策略：全部走基线
    resp = submit_batch(client, [
        item("m-2", "shared@corp.example.com"),  # 新编号：按受理时策略（修订 2，基线）
        item("m-1", "shared@corp.example.com"),  # 重放：保持首次判定（修订 1，候选）
    ])
    assert resp.status_code == 200
    new, replayed = resp.json()["results"]
    assert replayed["policy_revision"] == 1 and replayed["version"] == v2
    assert new["policy_revision"] == 2 and new["version"] == v1
    assert new["selection"] == "baseline"
    assert [e["mailbox"] for e in new["endpoints"]] == ["base@mail.example.com"]


# ---------- 整批校验：任一不满足即整批拒绝且不写入 ----------

def test_batch_invalid_address_rejects_entire_batch(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    resp = submit_batch(client, [
        item("ia-1", "shared@corp.example.com"),
        item("ia-2", "not-an-address"),
        item("ia-3", "also@@bad"),
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_address"
    assert [i["delivery_id"] for i in detail["invalid_items"]] == ["ia-2", "ia-3"]
    assert detail["invalid_items"][0]["index"] == 1
    # 整批不写入：有效编号也未被占用
    assert client.get("/deliveries/ia-1").status_code == 404
    ok = submit_batch(client, [item("ia-1", "shared@corp.example.com")])
    assert ok.status_code == 200


def test_batch_duplicate_id_rejects_entire_batch(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    resp = submit_batch(client, [
        item("dup-1", "shared@corp.example.com"),
        item("dup-2", "other@corp.example.com"),
        item("dup-1", "shared@corp.example.com"),  # 批内重复编号（同地址也不允许）
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_delivery_id"
    assert detail["delivery_ids"] == ["dup-1"]
    assert client.get("/deliveries/dup-1").status_code == 404
    assert client.get("/deliveries/dup-2").status_code == 404


def test_batch_conflict_with_existing_id_rejects_entire_batch(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    assert client.post("/deliveries", json=item("cf-1", "shared@corp.example.com")).status_code == 200
    resp = submit_batch(client, [
        item("cf-1", "other@corp.example.com"),   # 既有编号绑定其他规范化地址
        item("cf-2", "shared@corp.example.com"),  # 新编号：整批拒绝，不得写入
    ])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "delivery_id_conflict"
    assert detail["conflicts"] == [{
        "delivery_id": "cf-1",
        "submitted_address": "other@corp.example.com",
        "stored_address": "shared@corp.example.com",
    }]
    assert client.get("/deliveries/cf-2").status_code == 404  # 新编号未被占用
    # 原判定不被改写
    assert client.get("/deliveries/cf-1").json()["address"] == "shared@corp.example.com"
    ok = submit_batch(client, [item("cf-2", "shared@corp.example.com")])
    assert ok.status_code == 200


def test_batch_missing_policy_with_new_ids_rejected(client):
    v1, v2 = publish_two_versions(client)
    resp = submit_batch(client, [item("np-1", "shared@corp.example.com")])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    assert client.get("/deliveries/np-1").status_code == 404  # 编号未被占用
    set_policy(client, 0, v1, v2, 100)
    ok = submit_batch(client, [item("np-1", "shared@corp.example.com")])
    assert ok.status_code == 200


def test_batch_all_replays_need_no_policy(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit_batch(client, [item("rp-1", "shared@corp.example.com"), item("rp-2", "blocked@corp.example.com")])
    assert first.status_code == 200
    # 直接删除策略行模拟“尚无默认策略”（接口不提供删除，测试直接改库）
    conn = db.connect()
    conn.execute("DELETE FROM delivery_policy")
    conn.close()
    replay = submit_batch(client, [item("rp-1", "shared@corp.example.com"), item("rp-2", "blocked@corp.example.com")])
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 全部重放：无需策略
    # 但只要存在新编号，仍整批拒绝
    resp = submit_batch(client, [item("rp-1", "shared@corp.example.com"), item("rp-3", "shared@corp.example.com")])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    assert client.get("/deliveries/rp-3").status_code == 404


# ---------- 请求形状校验 ----------

def test_batch_empty_and_oversize_rejected(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    assert submit_batch(client, []).status_code == 422                      # 非空
    assert client.post("/deliveries/batch", json={}).status_code == 422     # 缺 items
    too_many = [item(f"ov-{i}", "shared@corp.example.com") for i in range(101)]
    assert submit_batch(client, too_many).status_code == 422                # 单批至多 100 笔
    assert client.get("/deliveries/ov-0").status_code == 404
    exactly = [item(f"ex-{i}", "shared@corp.example.com") for i in range(100)]
    resp = submit_batch(client, exactly)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 100


def test_batch_item_shape_validated(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    assert submit_batch(client, [item("", "shared@corp.example.com")]).status_code == 422
    assert submit_batch(client, [{"address": "shared@corp.example.com"}]).status_code == 422
    assert submit_batch(client, [{"delivery_id": "x-1"}]).status_code == 422


# ---------- 熔断：新判定全部写入后仅评估一次，重放不重复计数 ----------

def test_batch_circuit_breaker_evaluated_once_after_all_writes(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全部走候选；候选对 blocked@ 拒收
    assert client.put("/policy/circuit-breaker", json={
        "revision": 1,
        "failure_threshold_percent": 50,
        "min_candidate_samples": 2,
        "window_minutes": 5,
    }).status_code == 200
    resp = submit_batch(client, [
        item("cb-1", "blocked@corp.example.com"),
        item("cb-2", "blocked@corp.example.com"),
    ])
    assert resp.status_code == 200  # 触发请求仍返回本批已持久化的原判定
    assert all(r["status"] == "failed" for r in resp.json()["results"])
    assert all(r["policy_revision"] == 1 for r in resp.json()["results"])
    # 熔断只触发一次：候选比例置 0、策略修订号 +1、一条熔断记录
    policy = client.get("/policy").json()
    assert policy["revision"] == 2 and policy["candidate_percent"] == 0
    breaker = client.get("/policy/circuit-breaker").json()
    assert len(breaker["breaks"]) == 1
    assert breaker["breaks"][0]["policy_revision"] == 1
    assert breaker["breaks"][0]["sample_count"] == 2
    assert breaker["breaks"][0]["failure_count"] == 2
    # 同批重放：原样返回，不重复计数、不再触发
    replay = submit_batch(client, [
        item("cb-1", "blocked@corp.example.com"),
        item("cb-2", "blocked@corp.example.com"),
    ])
    assert replay.status_code == 200
    assert replay.json() == resp.json()
    assert len(client.get("/policy/circuit-breaker").json()["breaks"]) == 1


def test_batch_replay_only_does_not_evaluate_breaker(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert client.put("/policy/circuit-breaker", json={
        "revision": 1,
        "failure_threshold_percent": 1,
        "min_candidate_samples": 1,
        "window_minutes": 5,
    }).status_code == 200
    first = submit_batch(client, [item("ro-1", "blocked@corp.example.com")])
    assert first.status_code == 200
    assert len(client.get("/policy/circuit-breaker").json()["breaks"]) == 1  # 首笔已触发
    # 新修订上重新设置后，纯重放批不写入新判定、不评估熔断
    assert client.put("/policy/circuit-breaker", json={
        "revision": 2,
        "failure_threshold_percent": 1,
        "min_candidate_samples": 1,
        "window_minutes": 5,
    }).status_code == 200
    replay = submit_batch(client, [item("ro-1", "blocked@corp.example.com")])
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(client.get("/policy/circuit-breaker").json()["breaks"]) == 1


# ---------- 与单笔接口共存 ----------

def test_batch_and_single_endpoint_share_idempotency(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    single = client.post("/deliveries", json=item("s-1", "shared@corp.example.com"))
    assert single.status_code == 200
    # 单笔提交的判定在批量接口中按同编号同地址原样重放
    batch = submit_batch(client, [item("s-1", "SHARED@CORP.EXAMPLE.COM")])
    assert batch.status_code == 200
    result = batch.json()["results"][0]
    assert result["status"] == "ok"
    assert result["endpoints"] == single.json()["endpoints"]
    assert result["version"] == single.json()["version"]
    # 批量提交的判定也可被单笔接口重放（同响应体）
    assert submit_batch(client, [item("s-2", "shared@corp.example.com")]).status_code == 200
    again = client.post("/deliveries", json=item("s-2", "shared@corp.example.com"))
    assert again.status_code == 200
    assert again.json()["delivery_id"] == "s-2"
