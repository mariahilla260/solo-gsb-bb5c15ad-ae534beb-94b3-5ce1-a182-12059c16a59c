"""投递回执（POST /deliveries/{id}/receipts、GET /deliveries/{id}/receipts）：

仅对已持久化的成功判定开放：未知编号 → 404，原判定失败 / 非所属终点 → 拒绝且不产生记录。
同一终点同序号同结果重试原样返回，同序号不同结果 → 冲突；新序号必须严格递增；
已送达 / 永久失败为终结结果，之后不再接收新尝试。
汇总：全部终点已送达 → delivered，任一终点永久失败 → failed，其余（含无回执）→ pending。
并发回传经 BEGIN IMMEDIATE 串行化，序号与汇总一致；回执不影响判定查询、
同编号重放内容与灰度熔断统计。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时设置
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
        "DELETE FROM delivery_attempts;"
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
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


RULES = [
    {
        "match_type": "exact",
        "match_key": "multi@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
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


def setup(client):
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200


def submit_decision(client, delivery_id, address="multi@corp.example.com"):
    resp = client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})
    assert resp.status_code == 200, resp.text
    return resp.json()


def receipt(client, delivery_id, mailbox, attempt_no, result):
    return client.post(
        f"/deliveries/{delivery_id}/receipts",
        json={"mailbox": mailbox, "attempt_no": attempt_no, "result": result},
    )


# ---------- 受理与查询：基础形状 ----------

def test_receipt_accepted_for_listed_endpoint_and_persisted(client):
    setup(client)
    submit_decision(client, "msg-1")
    resp = receipt(client, "msg-1", " ALICE@MAIL.EXAMPLE.COM ", 1, "temporary_failure")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "delivery_id": "msg-1",
        "mailbox": "alice@mail.example.com",  # 规范化（去空白、小写）
        "attempt_no": 1,
        "result": "temporary_failure",
        "created_at": body["created_at"],
    }
    assert body["created_at"]


def test_get_receipts_empty_is_pending(client):
    setup(client)
    submit_decision(client, "msg-1")
    resp = client.get("/deliveries/msg-1/receipts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_id"] == "msg-1"
    assert body["status"] == "pending"  # 无回执也为待完成
    assert [ep["mailbox"] for ep in body["endpoints"]] == [
        "alice@mail.example.com", "bob@mail.example.com",
    ]
    for ep in body["endpoints"]:
        assert ep["status"] == "pending" and ep["attempts"] == []


def test_summary_all_delivered(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "temporary_failure").status_code == 200
    assert client.get("/deliveries/msg-1/receipts").json()["status"] == "pending"
    assert receipt(client, "msg-1", "bob@mail.example.com", 2, "delivered").status_code == 200
    body = client.get("/deliveries/msg-1/receipts").json()
    assert body["status"] == "delivered"  # 全部终点已送达
    by = {ep["mailbox"]: ep for ep in body["endpoints"]}
    assert by["alice@mail.example.com"]["status"] == "delivered"
    assert [a["attempt_no"] for a in by["bob@mail.example.com"]["attempts"]] == [1, 2]
    assert [a["result"] for a in by["bob@mail.example.com"]["attempts"]] == [
        "temporary_failure", "delivered",
    ]


def test_summary_any_permanent_failure_is_failed_even_if_others_pending(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "permanent_failure").status_code == 200
    # bob 尚无任何回执；任一终点永久失败即汇总为失败
    body = client.get("/deliveries/msg-1/receipts").json()
    assert body["status"] == "failed"
    by = {ep["mailbox"]: ep for ep in body["endpoints"]}
    assert by["alice@mail.example.com"]["status"] == "permanent_failure"
    assert by["bob@mail.example.com"]["status"] == "pending"


def test_temporary_failures_keep_pending(client):
    setup(client)
    submit_decision(client, "msg-1")
    for no in (1, 2, 3):
        assert receipt(client, "msg-1", "alice@mail.example.com", no, "temporary_failure").status_code == 200
    body = client.get("/deliveries/msg-1/receipts").json()
    assert body["status"] == "pending"
    assert body["endpoints"][0]["status"] == "temporary_failure"  # 取最大序号的结果


# ---------- 拒绝：未知编号 / 原判定失败 / 非所属终点，且不产生记录 ----------

def test_unknown_delivery_404_and_no_record(client):
    setup(client)
    resp = receipt(client, "nope", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "delivery_not_found"
    assert client.get("/deliveries/nope/receipts").status_code == 404


def test_failed_decision_rejects_receipt_post_and_get(client):
    setup(client)
    failed = client.post("/deliveries", json={"delivery_id": "msg-f", "address": "blocked@corp.example.com"})
    assert failed.status_code == 422
    resp = receipt(client, "msg-f", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "delivery_decision_failed"
    assert detail["decision_status"] == "failed"
    assert detail["error_type"] == "rejected"
    # 查询同样拒绝
    got = client.get("/deliveries/msg-f/receipts")
    assert got.status_code == 409
    assert got.json()["detail"]["error"] == "delivery_decision_failed"


def test_endpoint_not_in_decision_rejected(client):
    setup(client)
    submit_decision(client, "msg-1")
    resp = receipt(client, "msg-1", "catchall@mail.example.com", 1, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_not_allowed"
    assert detail["mailbox"] == "catchall@mail.example.com"
    assert set(detail["allowed_endpoints"]) == {"alice@mail.example.com", "bob@mail.example.com"}
    # 未产生任何记录：汇总仍全 pending
    assert client.get("/deliveries/msg-1/receipts").json()["status"] == "pending"


def test_receipt_does_not_leak_across_deliveries(client):
    setup(client)
    submit_decision(client, "msg-1")
    submit_decision(client, "msg-2", "stranger@corp.example.com")  # 仅 catchall 终点
    # catchall 属于 msg-2 但不属于 msg-1
    assert receipt(client, "msg-1", "catchall@mail.example.com", 1, "delivered").status_code == 409
    assert receipt(client, "msg-2", "alice@mail.example.com", 1, "delivered").status_code == 409
    assert receipt(client, "msg-2", "catchall@mail.example.com", 1, "delivered").status_code == 200


def test_invalid_mailbox_422(client):
    setup(client)
    submit_decision(client, "msg-1")
    resp = receipt(client, "msg-1", "not-an-address", 1, "delivered")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"
    assert client.get("/deliveries/msg-1/receipts").json()["status"] == "pending"


def test_invalid_attempt_no_and_result_rejected_by_schema(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 0, "delivered").status_code == 422
    assert receipt(client, "msg-1", "alice@mail.example.com", -1, "delivered").status_code == 422
    resp = client.post(
        "/deliveries/msg-1/receipts",
        json={"mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "bounced"},
    )
    assert resp.status_code == 422


# ---------- 幂等重试 / 冲突 / 序号 / 终结 ----------

def test_same_attempt_same_result_replays_identical_record(client):
    setup(client)
    submit_decision(client, "msg-1")
    first = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    replay = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 原样返回
    body = client.get("/deliveries/msg-1/receipts").json()
    assert len(body["endpoints"][0]["attempts"]) == 1  # 不产生重复记录


def test_same_attempt_different_result_conflict(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    resp = receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "attempt_result_conflict"
    assert detail["stored_result"] == "temporary_failure"
    assert detail["submitted_result"] == "delivered"
    assert detail["attempt_no"] == 1
    # 原记录不改写，再来相同结果仍原样返回
    again = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert again.status_code == 200 and again.json()["result"] == "temporary_failure"


def test_new_attempt_no_must_be_greater(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure").status_code == 200
    # 同序号同结果是重放 → 200；更小序号 → 冲突
    assert receipt(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure").status_code == 200
    resp = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "attempt_sequence_conflict"
    assert detail["last_attempt_no"] == 2 and detail["submitted_attempt_no"] == 1
    # 允许跳号：3 大于 2 即可
    assert receipt(client, "msg-1", "alice@mail.example.com", 3, "temporary_failure").status_code == 200


def test_delivered_is_terminal(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    resp = receipt(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_terminal"
    assert detail["terminal_result"] == "delivered"
    # 同序号同结果的重放仍然允许（原样返回）
    replay = receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered")
    assert replay.status_code == 200
    assert replay.json()["attempt_no"] == 1
    # 另一终点不受影响，仍可继续接收
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "delivered").status_code == 200


def test_permanent_failure_is_terminal(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 2, "permanent_failure").status_code == 200
    # 更大的新序号也拒绝
    resp = receipt(client, "msg-1", "alice@mail.example.com", 3, "temporary_failure")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "endpoint_terminal"
    assert receipt(client, "msg-1", "alice@mail.example.com", 2, "permanent_failure").status_code == 200


def test_terminal_takes_precedence_over_sequence_check(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 3, "permanent_failure").status_code == 200
    # 更小的新序号：先撞终结，而不是序号冲突
    resp = receipt(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "endpoint_terminal"


def test_sequence_is_per_endpoint(client):
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 1, "temporary_failure").status_code == 200
    assert receipt(client, "msg-1", "alice@mail.example.com", 2, "delivered").status_code == 200
    assert receipt(client, "msg-1", "bob@mail.example.com", 2, "delivered").status_code == 200


# ---------- 并发：序号与汇总一致，同序号重试收敛为一条 ----------

def test_concurrent_retry_storms_per_attempt_each_persisted_once(client):
    # 真实投递中第 N+1 次尝试只在第 N 次结果已知后发生；并发的是同一尝试的超时重试。
    # 每个序号并发 6 个完全相同的回传：全部 200，且每个序号只落一条记录。
    setup(client)
    submit_decision(client, "msg-1")
    for no in range(1, 9):
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(
                lambda _: receipt(client, "msg-1", "alice@mail.example.com", no, "temporary_failure"),
                range(6),
            ))
        assert {r.status_code for r in responses} == {200}
        assert {r.text for r in responses} == {responses[0].text}
    body = client.get("/deliveries/msg-1/receipts").json()
    attempts = body["endpoints"][0]["attempts"]
    assert [a["attempt_no"] for a in attempts] == list(range(1, 9))
    assert {a["result"] for a in attempts} == {"temporary_failure"}


def test_late_smaller_attempt_after_newer_committed_is_rejected(client):
    # 写锁串行化后，先提交了较大序号的请求，迟到的较小序号请求不产生记录。
    setup(client)
    submit_decision(client, "msg-1")
    assert receipt(client, "msg-1", "alice@mail.example.com", 3, "temporary_failure").status_code == 200
    late = receipt(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert late.status_code == 409
    assert late.json()["detail"]["error"] == "attempt_sequence_conflict"
    attempts = client.get("/deliveries/msg-1/receipts").json()["endpoints"][0]["attempts"]
    assert [a["attempt_no"] for a in attempts] == [3]  # 迟到请求未写入


def test_concurrent_same_attempt_same_result_single_record(client):
    setup(client)
    submit_decision(client, "msg-1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: receipt(client, "msg-1", "alice@mail.example.com", 1, "delivered"), range(16)
        ))
    assert {r.status_code for r in responses} == {200}
    assert {r.text for r in responses} == {responses[0].text}
    attempts = client.get("/deliveries/msg-1/receipts").json()["endpoints"][0]["attempts"]
    assert len(attempts) == 1 and attempts[0]["result"] == "delivered"


def test_concurrent_same_attempt_different_results_exactly_one_wins(client):
    setup(client)
    submit_decision(client, "msg-1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda r: receipt(client, "msg-1", "alice@mail.example.com", 1, r),
            ["delivered", "permanent_failure"],
        ))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409]
    winner = next(r for r in responses if r.status_code == 200).json()["result"]
    loser = next(r for r in responses if r.status_code == 409).json()["detail"]
    assert loser["error"] == "attempt_result_conflict"
    assert loser["stored_result"] == winner
    attempts = client.get("/deliveries/msg-1/receipts").json()["endpoints"][0]["attempts"]
    assert len(attempts) == 1 and attempts[0]["result"] == winner


def test_concurrent_endpoints_consistent_summary(client):
    setup(client)
    submit_decision(client, "msg-1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(
            lambda mb: receipt(client, "msg-1", mb, 1, "delivered"),
            ["alice@mail.example.com", "bob@mail.example.com",
             "alice@mail.example.com", "bob@mail.example.com"],
        ))
    assert {r.status_code for r in responses} == {200}
    body = client.get("/deliveries/msg-1/receipts")
    assert body.json()["status"] == "delivered"


# ---------- 既有判定查询 / 重放 / 熔断不受回执影响 ----------

def test_receipts_do_not_change_decision_or_replay(client):
    setup(client)
    decision = submit_decision(client, "msg-1")
    receipt(client, "msg-1", "alice@mail.example.com", 1, "permanent_failure")
    receipt(client, "msg-1", "bob@mail.example.com", 5, "delivered")
    # GET 判定内容不变
    stored = client.get("/deliveries/msg-1").json()
    assert stored["status"] == "ok"
    assert stored["endpoints"] == decision["endpoints"]
    # 同编号重放仍是首次判定（不随回执变化）
    replay = client.post("/deliveries", json={"delivery_id": "msg-1", "address": "MULTI@CORP.EXAMPLE.COM"})
    assert replay.status_code == 200
    assert replay.json() == decision


def test_receipts_do_not_feed_circuit_breaker(client):
    # 候选侧失败判定触发熔断只统计判定本身；回执写入不触发、不计数。
    assert client.put("/draft", json={"revision": 0, "rules": [
        {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject", "targets": []},
        {"match_type": "exact", "match_key": "ok@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "m@mail.example.com"}]},
    ]}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200
    assert client.put("/policy/circuit-breaker", json={
        "revision": 1, "failure_threshold_percent": 50,
        "min_candidate_samples": 2, "window_minutes": 5,
    }).status_code == 200
    # 一笔成功判定 + 对其写入若干“永久失败”回执
    submit_decision(client, "msg-ok", "ok@corp.example.com")
    for no in range(1, 4):
        assert receipt(client, "msg-ok", "m@mail.example.com", no, "permanent_failure").status_code in (
            200 if no == 1 else 409,  # 第 2、3 笔因终结被拒
        )
    # 熔断未触发：样本只有 1 笔判定（回执不计数），策略修订号仍为 1
    assert client.get("/policy").json()["revision"] == 1
    assert client.get("/policy/circuit-breaker").json()["breaks"] == []
    # 再来一笔失败判定才达阈值并触发
    failed = client.post("/deliveries", json={"delivery_id": "msg-bad", "address": "blocked@corp.example.com"})
    assert failed.status_code == 422
    assert client.get("/policy").json()["revision"] == 2
    assert len(client.get("/policy/circuit-breaker").json()["breaks"]) == 1
