"""投递任务领取与租约（POST /delivery-tasks/claim、POST /delivery-tasks/complete）：

仅已持久化的成功判定终点可入列：无回执立即到期；最新回执 temporary_failure 按
min(60×2^(尝试序号−1), 3600) 秒退避后到期；delivered / permanent_failure 不再入列。
领取按到期时间、投递编号、终点稳定排序；有效租约不重复分配，到期可重领且旧令牌失效。
凭有效令牌原子写回执并结束租约；既有回执接口抢先写入时令牌失效且不得覆盖；
过期 / 失效令牌明确 409；非法参数 422；无到期项返回空列表。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# 必须在导入 app 之前设置，config 在导入时设置
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM delivery_leases;"
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
        "match_key": "single@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "carol@mail.example.com"}],
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


def claim(client, max_claims=100, lease_seconds=3600):
    return client.post("/delivery-tasks/claim", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })


def complete(client, token, result):
    return client.post("/delivery-tasks/complete", json={
        "lease_token": token, "result": result,
    })


def set_attempt_time(delivery_id, mailbox, attempt_no, at: datetime):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE delivery_attempts SET created_at = ? "
            "WHERE delivery_id = ? AND mailbox = ? AND attempt_no = ?",
            (_iso(at), delivery_id, mailbox, attempt_no),
        )
    finally:
        conn.close()


def set_lease_expiry(token, at: datetime):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE delivery_leases SET expires_at = ? WHERE lease_token = ?",
            (_iso(at), token),
        )
    finally:
        conn.close()


# ---------- 领取：基础形状、排序、上限 ----------

def test_no_tasks_returns_empty_list(client):
    setup(client)
    resp = claim(client)
    assert resp.status_code == 200
    assert resp.json() == {"tasks": []}


def test_failed_decisions_are_not_claimable(client):
    setup(client)
    # blocked@ 的拒收判定虽持久化（422 但落库），没有可领取终点
    bad = client.post("/deliveries", json={"delivery_id": "msg-bad", "address": "blocked@corp.example.com"})
    assert bad.status_code == 422
    assert claim(client).json() == {"tasks": []}


def test_claim_shape_no_receipt_is_due_immediately_with_next_attempt_1(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    resp = claim(client, max_claims=10, lease_seconds=60)
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["delivery_id"] == "msg-1"
    assert task["endpoint"] == "carol@mail.example.com"
    assert task["next_attempt_no"] == 1
    assert task["lease_token"] and isinstance(task["lease_token"], str)
    assert task["due_at"]  # 无回执：立即到期（判定受理时刻）
    assert task["lease_expires_at"] > task["due_at"]


def test_claim_expands_every_endpoint_of_a_decision(client):
    setup(client)
    submit_decision(client, "msg-1")  # 两个终点
    tasks = claim(client).json()["tasks"]
    assert {t["endpoint"] for t in tasks} == {"alice@mail.example.com", "bob@mail.example.com"}
    assert all(t["delivery_id"] == "msg-1" and t["next_attempt_no"] == 1 for t in tasks)


def test_stable_sort_by_due_time_then_delivery_id_then_endpoint(client):
    setup(client)
    submit_decision(client, "msg-b", "multi@corp.example.com")
    submit_decision(client, "msg-a", "single@corp.example.com")
    # 全部无回执：due_at 等于各自判定受理时刻，msg-a 先提交故更靠前
    tasks = claim(client, max_claims=100).json()["tasks"]
    ordered = [(t["due_at"], t["delivery_id"], t["endpoint"]) for t in tasks]
    assert ordered == sorted(ordered)
    assert ordered[0][1] == "msg-a"
    # 同 delivery 内按终点排序
    bob = [t for t in tasks if t["delivery_id"] == "msg-b"]
    assert [t["endpoint"] for t in bob] == ["alice@mail.example.com", "bob@mail.example.com"]


def test_claim_respects_max_claims_and_skips_active_leases(client):
    setup(client)
    submit_decision(client, "msg-1")  # 2 个终点
    submit_decision(client, "msg-2", "single@corp.example.com")  # 1 个
    first = claim(client, max_claims=2).json()["tasks"]
    assert len(first) == 2
    second = claim(client, max_claims=100).json()["tasks"]
    assert len(second) == 1  # 前两项已持有有效租约
    claimed = {(t["delivery_id"], t["endpoint"]) for t in first}
    assert (second[0]["delivery_id"], second[0]["endpoint"]) not in claimed


def test_older_due_temp_failure_sorts_before_newer_immediate_tasks(client):
    setup(client)
    # msg-old：回执 temporary_failure 已过退避（60s），due_at 是过去时刻，应排最前
    submit_decision(client, "msg-old", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    assert complete(client, token, "temporary_failure").status_code == 200
    receipt_time = _utc_now()
    set_attempt_time("msg-old", "carol@mail.example.com", 1, receipt_time - timedelta(seconds=120))
    # 新提交的无回执判定
    submit_decision(client, "msg-new", "multi@corp.example.com")
    tasks = claim(client).json()["tasks"]
    assert tasks[0]["delivery_id"] == "msg-old"
    assert tasks[0]["next_attempt_no"] == 2


# ---------- 退避：60×2^(k−1)，封顶 3600 ----------

@pytest.mark.parametrize("attempt_no,backoff", [
    (1, 60), (2, 120), (3, 240), (4, 480), (5, 960), (6, 1920), (7, 3600), (8, 3600),
])
def test_temporary_failure_backoff_schedule(client, attempt_no, backoff):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    mailbox = "carol@mail.example.com"
    for no in range(1, attempt_no + 1):
        token = claim(client).json()["tasks"][0]["lease_token"]
        assert complete(client, token, "temporary_failure").status_code == 200
        set_attempt_time("msg-1", mailbox, no, _utc_now() - timedelta(days=30))
    # 把最后一次回执时间挪到“退避差一点才到”：不应入列
    last_at = _utc_now() - timedelta(seconds=backoff - 5)
    set_attempt_time("msg-1", mailbox, attempt_no, last_at)
    tasks = claim(client).json()["tasks"]
    assert tasks == []
    # 再挪到“退避刚到”：应入列，下一序号为 k+1
    set_attempt_time("msg-1", mailbox, attempt_no, _utc_now() - timedelta(seconds=backoff))
    tasks = claim(client).json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["next_attempt_no"] == attempt_no + 1
    assert tasks[0]["endpoint"] == mailbox


def test_delivered_and_permanent_failure_never_requeue(client):
    setup(client)
    submit_decision(client, "msg-1")  # alice + bob
    tasks = {t["endpoint"]: t for t in claim(client).json()["tasks"]}
    assert complete(client, tasks["alice@mail.example.com"]["lease_token"], "delivered").status_code == 200
    assert complete(client, tasks["bob@mail.example.com"]["lease_token"], "permanent_failure").status_code == 200
    assert claim(client).json() == {"tasks": []}


# ---------- 凭令牌完成：原子写回执、结束租约 ----------

def test_complete_with_valid_token_writes_receipt_and_closes_lease(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    resp = complete(client, token, "delivered")
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_id"] == "msg-1"
    assert body["mailbox"] == "carol@mail.example.com"
    assert body["attempt_no"] == 1 and body["result"] == "delivered"
    assert body["created_at"]
    # 租约已结束：该终点终结，不再被领取
    assert claim(client).json() == {"tasks": []}
    # 回执可经既有查询接口看到
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "delivered"
    assert summary["endpoints"][0]["attempts"][0]["result"] == "delivered"


def test_complete_temp_failure_requeues_after_backoff_with_attempt_2(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    assert complete(client, token, "temporary_failure").status_code == 200
    # 60 秒内不可重领
    assert claim(client).json() == {"tasks": []}
    set_attempt_time("msg-1", "carol@mail.example.com", 1, _utc_now() - timedelta(seconds=61))
    tasks = claim(client).json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["next_attempt_no"] == 2


def test_replay_same_token_same_result_returns_first_record(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    first = complete(client, token, "temporary_failure")
    replay = complete(client, token, "temporary_failure")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert len(summary["endpoints"][0]["attempts"]) == 1  # 不产生重复回执


def test_replay_same_token_different_result_conflicts(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    assert complete(client, token, "temporary_failure").status_code == 200
    resp = complete(client, token, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "lease_token_invalid"
    assert detail["reason"] == "result_conflict"
    # 首次记录不改写
    got = client.get("/deliveries/msg-1/receipts").json()
    assert got["endpoints"][0]["attempts"][0]["result"] == "temporary_failure"


# ---------- 过期 / 失效令牌：明确冲突，旧令牌作废 ----------

def test_unknown_token_is_conflict(client):
    setup(client)
    resp = complete(client, "does-not-exist", "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "lease_token_invalid"
    assert detail["reason"] == "not_found"


def test_expired_lease_can_be_reclaimed_and_old_token_fails(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    old = claim(client, lease_seconds=60).json()["tasks"][0]
    # 租约未到期：无任务可领
    assert claim(client).json() == {"tasks": []}
    # 令旧租约过期
    set_lease_expiry(old["lease_token"], _utc_now() - timedelta(seconds=1))
    new = claim(client).json()["tasks"]
    assert len(new) == 1 and new[0]["endpoint"] == "carol@mail.example.com"
    assert new[0]["lease_token"] != old["lease_token"]
    # 旧令牌完成 → 409 expired
    resp = complete(client, old["lease_token"], "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "lease_token_invalid" and detail["reason"] == "expired"
    # 新令牌有效
    assert complete(client, new[0]["lease_token"], "delivered").status_code == 200
    # 旧令牌此后仍是失效状态
    again = complete(client, old["lease_token"], "delivered")
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "expired"


def test_expired_token_without_reclaim_still_rejected(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client, lease_seconds=60).json()["tasks"][0]["lease_token"]
    set_lease_expiry(token, _utc_now() - timedelta(seconds=1))
    # 即使没人重领，过期令牌提交也被拒绝（完成前统一清理过期租约）
    resp = complete(client, token, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "expired"


def test_direct_receipt_first_invalidates_token_and_is_not_overwritten(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    # 既有回执接口抢先记录了该终点的新回执（同一序号）
    direct = client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "carol@mail.example.com", "attempt_no": 1, "result": "delivered",
    })
    assert direct.status_code == 200
    resp = complete(client, token, "temporary_failure")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "lease_token_invalid"
    assert detail["reason"] == "superseded"
    # 抢先回执保持 delivered 不被覆盖；租约仍未完成，但终点已终结不再入列
    got = client.get("/deliveries/msg-1/receipts").json()
    assert got["endpoints"][0]["attempts"][0]["result"] == "delivered"
    assert claim(client).json() == {"tasks": []}


def test_direct_receipt_with_greater_attempt_no_invalidates_token(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    # 直接接口先写了序号 3（允许跳号）：持有序号 1 的令牌立即失效
    direct = client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "carol@mail.example.com", "attempt_no": 3, "result": "temporary_failure",
    })
    assert direct.status_code == 200
    resp = complete(client, token, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "superseded"
    got = client.get("/deliveries/msg-1/receipts").json()
    assert [a["attempt_no"] for a in got["endpoints"][0]["attempts"]] == [3]


# ---------- 参数校验 ----------

@pytest.mark.parametrize("max_claims,lease_seconds", [
    (0, 60), (-1, 60), (101, 60), (50, 0), (50, -10), (50, 3601),
])
def test_invalid_claim_params_rejected(client, max_claims, lease_seconds):
    setup(client)
    resp = client.post("/delivery-tasks/claim", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })
    assert resp.status_code == 422


def test_missing_claim_fields_rejected(client):
    assert client.post("/delivery-tasks/claim", json={"max_claims": 10}).status_code == 422
    assert client.post("/delivery-tasks/claim", json={"lease_seconds": 10}).status_code == 422


def test_invalid_complete_payload_rejected(client):
    assert client.post("/delivery-tasks/complete", json={
        "lease_token": "", "result": "delivered",
    }).status_code == 422
    assert client.post("/delivery-tasks/complete", json={
        "lease_token": "x", "result": "bounced",
    }).status_code == 422
    assert client.post("/delivery-tasks/complete", json={"result": "delivered"}).status_code == 422


# ---------- 并发：领取不重复分配有效租约 ----------

def test_concurrent_claims_never_double_allocate(client):
    setup(client)
    submit_decision(client, "msg-1")  # 2 个终点
    submit_decision(client, "msg-2", "single@corp.example.com")  # 1 个
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: claim(client, max_claims=100), range(16)))
    assert {r.status_code for r in responses} == {200}
    allocated: list[tuple[str, str]] = []
    for r in responses:
        for t in r.json()["tasks"]:
            allocated.append((t["delivery_id"], t["endpoint"]))
    # 3 个到期终点被并发领取 16 轮：每轮领取的数量之和恰好为 3（无重复分配）
    assert len(allocated) == 3
    assert len(set(allocated)) == 3
    conn = db.connect()
    try:
        active = conn.execute("SELECT COUNT(*) AS n FROM delivery_leases WHERE status = 'active'").fetchone()["n"]
        assert active == 3
    finally:
        conn.close()


def test_concurrent_claim_then_complete_each_token_works_once(client):
    setup(client)
    submit_decision(client, "msg-1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: claim(client), range(4)))
    tokens = [t["lease_token"] for r in responses for t in r.json()["tasks"]]
    assert len(tokens) == 2
    results = list(ThreadPoolExecutor(max_workers=2).map(
        lambda tok: complete(client, tok, "delivered"), tokens
    ))
    assert {r.status_code for r in results} == {200}
    assert claim(client).json() == {"tasks": []}  # 两终点均 delivered


# ---------- 与既有功能的边界：判定 / 重放 / 报表 / 熔断 / 直接回执不受破坏 ----------

def test_claim_and_complete_do_not_change_decisions_or_replay(client):
    setup(client)
    decision = submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    complete(client, token, "temporary_failure")
    stored = client.get("/deliveries/msg-1").json()
    assert stored["status"] == "ok" and stored["endpoints"] == decision["endpoints"]
    replay = client.post("/deliveries", json={"delivery_id": "msg-1", "address": "SINGLE@CORP.EXAMPLE.COM"})
    assert replay.status_code == 200 and replay.json() == decision


def test_completed_temp_failure_reappears_in_summary_and_report_unaffected(client):
    setup(client)
    submit_decision(client, "msg-1", "single@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    complete(client, token, "temporary_failure")
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "pending"
    assert summary["endpoints"][0]["status"] == "temporary_failure"
    # 报表仍只统计判定，不出现租约 / 回执痕迹
    report = client.get("/deliveries/report", params={
        "start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z",
    })
    assert report.status_code == 200
    assert report.json()["total"] == 1


def test_claim_does_not_trigger_circuit_breaker(client):
    assert client.put("/draft", json={"revision": 0, "rules": [
        {"match_type": "exact", "match_key": "ok@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "m@mail.example.com"}]},
        {"match_type": "exact", "match_key": "bad@corp.example.com", "action": "reject", "targets": []},
    ]}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200
    assert client.put("/policy/circuit-breaker", json={
        "revision": 1, "failure_threshold_percent": 50,
        "min_candidate_samples": 2, "window_minutes": 5,
    }).status_code == 200
    submit_decision(client, "msg-ok", "ok@corp.example.com")
    token = claim(client).json()["tasks"][0]["lease_token"]
    complete(client, token, "permanent_failure")
    # 回执与领取都不进入熔断统计：仍只有 1 笔判定，未触发
    assert client.get("/policy").json()["revision"] == 1
    assert client.get("/policy/circuit-breaker").json()["breaks"] == []


def test_direct_receipts_and_leases_coexist_per_endpoint_sequence(client):
    setup(client)
    submit_decision(client, "msg-1")  # alice + bob
    # alice 走直接回执接口完成序号 1
    assert client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
    }).status_code == 200
    # bob 走领取
    tasks = claim(client).json()["tasks"]
    assert [(t["endpoint"], t["next_attempt_no"]) for t in tasks] == [("bob@mail.example.com", 1)]
    assert complete(client, tasks[0]["lease_token"], "delivered").status_code == 200
    assert claim(client).json() == {"tasks": []}
