"""投递任务领取与租约（POST /deliveries/claims、POST /deliveries/claims/{token}/complete）：

仅已持久化成功判定的终点入列：无回执立即到期（下一尝试序号 1）；最新回执
temporary_failure 按 min(60×2^(尝试序号−1),3600) 秒退避到期，下一序号 +1；
delivered / permanent_failure 不再入列。按到期时刻、投递编号、终点稳定排序。
并发领取经 BEGIN IMMEDIATE 串行化，不重复分配有效租约；到期可重领且旧令牌失效；
凭有效令牌原子写入回执并结束租约；既有回执接口先记录新回执时令牌失效且不覆盖；
过期 / 失效 / 未知令牌报 409；参数非法 422；无到期项返回空列表。
既有回执接口、判定查询 / 重放与熔断统计的约定不变。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# 必须在导入 app 之前设置，config 在导入时设置
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM endpoint_leases;"
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

ISO_NOW = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"


def setup(client):
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200


def submit(client, delivery_id, address="multi@corp.example.com"):
    resp = client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})
    assert resp.status_code == 200, resp.text
    return resp.json()


def claim(client, max_claims=100, lease_seconds=300):
    return client.post("/deliveries/claims", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })


def complete(client, token, result):
    return client.post(f"/deliveries/claims/{token}/complete", json={"result": result})


def shift_attempt_ago(delivery_id, mailbox, seconds):
    """把某终点最新回执的 created_at 前移 seconds 秒（确定性测试退避到期）。"""
    conn = db.connect()
    conn.execute(
        "UPDATE delivery_attempts SET created_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) "
        "WHERE delivery_id = ? AND mailbox = ?",
        (f"-{seconds} seconds", delivery_id, mailbox),
    )
    conn.close()


def expire_lease(token):
    conn = db.connect()
    conn.execute(
        "UPDATE endpoint_leases SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-1 second') WHERE token = ?",
        (token,),
    )
    conn.close()


def pin_decision_time(delivery_ids, value="2026-10-05T00:00:00+00:00"):
    conn = db.connect()
    conn.executemany(
        "UPDATE delivery_decisions SET created_at = ? WHERE delivery_id = ?",
        [(value, did) for did in delivery_ids],
    )
    conn.close()


def tasks_by(client, key):
    body = claim(client).json()
    return {t[key]: t for t in body["tasks"]}


# ---------- 入列与排序 ----------

def test_claim_empty_queue_when_nothing_persisted(client):
    setup(client)
    resp = claim(client)
    assert resp.status_code == 200
    assert resp.json() == {"tasks": []}


def test_claim_returns_only_persisted_successful_endpoints(client):
    setup(client)
    submit(client, "msg-1")
    submit(client, "msg-2", "single@corp.example.com")
    # 失败判定持久化但不入列
    blocked = client.post("/deliveries", json={"delivery_id": "msg-bad", "address": "blocked@corp.example.com"})
    assert blocked.status_code == 422
    resp = claim(client, max_claims=10)
    assert resp.status_code == 200, resp.text
    tasks = resp.json()["tasks"]
    got = {(t["delivery_id"], t["mailbox"]) for t in tasks}
    assert got == {
        ("msg-1", "alice@mail.example.com"),
        ("msg-1", "bob@mail.example.com"),
        ("msg-2", "carol@mail.example.com"),
    }
    assert all(t["delivery_id"] != "msg-bad" for t in tasks)


def test_claim_task_shape(client):
    setup(client)
    submit(client, "msg-1")
    task = claim(client).json()["tasks"][0]
    assert set(task) == {"delivery_id", "mailbox", "attempt_no", "due_at", "lease_expires_at", "token"}
    assert task["delivery_id"] == "msg-1"
    assert task["attempt_no"] == 1  # 无回执：下一尝试序号为 1
    assert task["mailbox"] in {"alice@mail.example.com", "bob@mail.example.com"}
    assert task["due_at"]  # 到期时刻（= 判定受理时刻，立即到期）
    assert isinstance(task["token"], str) and len(task["token"]) == 32  # 16 字节十六进制


def test_claim_respects_max_claims(client):
    setup(client)
    submit(client, "msg-1")  # 2 个终点
    assert len(claim(client, max_claims=1).json()["tasks"]) == 1
    # 剩余 1 个仍被上一笔的领取阻塞？不——不同终点各自有有效租约，只返回未出租约的
    rest = claim(client, max_claims=10).json()["tasks"]
    assert len(rest) == 1


def test_stable_order_due_then_delivery_id_then_mailbox(client):
    setup(client)
    submit(client, "msg-c", "single@corp.example.com")      # carol
    submit(client, "msg-a", "multi@corp.example.com")       # alice, bob
    submit(client, "msg-b", "stranger@corp.example.com")    # catchall
    pin_decision_time(["msg-a", "msg-b", "msg-c"])
    order = [(t["delivery_id"], t["mailbox"]) for t in claim(client).json()["tasks"]]
    # 到期时刻相同（无回执，同一秒）→ 投递编号 → 终点
    assert order == [
        ("msg-a", "alice@mail.example.com"),
        ("msg-a", "bob@mail.example.com"),
        ("msg-b", "catchall@mail.example.com"),
        ("msg-c", "carol@mail.example.com"),
    ]


def test_due_time_precedes_delivery_id_in_order(client):
    # carol 无回执，到期点 = now-30；alice 完成 attempt 1 的 temp failure，
    # 回执时刻 = now-10 → 到期点 now+50（退避未到，暂不入列）；
    # 再把 alice 回执前移 51 秒（=now-61）→ 到期点 now-1（已到期，但仍晚于 carol）。
    setup(client)
    submit(client, "msg-old", "single@corp.example.com")   # carol
    submit(client, "msg-new", "multi@corp.example.com")    # alice, bob
    new_first = tasks_by(client, "mailbox")
    assert complete(client, new_first["alice@mail.example.com"]["token"], "temporary_failure").status_code == 200
    pin_decision_time(
        ["msg-old", "msg-new"],
        (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds"),
    )
    shift_attempt_ago("msg-new", "alice@mail.example.com", 10)
    # 把未结束的租约全部置为过期后重领：carol/bob 立即可领，alice 退避未到不出现
    conn = db.connect()
    conn.execute(
        "UPDATE endpoint_leases SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-1 second')"
    )
    conn.close()
    tasks = claim(client).json()["tasks"]
    pairs = {(t["delivery_id"], t["mailbox"]) for t in tasks}
    assert ("msg-old", "carol@mail.example.com") in pairs
    assert ("msg-new", "alice@mail.example.com") not in pairs
    # alice 退避刚到（到期点 now-1，仍晚于 carol 的 now-30）：一次领取比较先后
    shift_attempt_ago("msg-new", "alice@mail.example.com", 61)
    conn = db.connect()
    conn.execute(
        "UPDATE endpoint_leases SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-1 second')"
    )
    conn.close()
    tasks = claim(client).json()["tasks"]
    idx_carol = next(i for i, t in enumerate(tasks)
                     if (t["delivery_id"], t["mailbox"]) == ("msg-old", "carol@mail.example.com"))
    idx_alice = next(i for i, t in enumerate(tasks)
                     if (t["delivery_id"], t["mailbox"]) == ("msg-new", "alice@mail.example.com"))
    assert idx_carol < idx_alice  # carol 到期点 now-30 早于 alice 的 now-1
    assert tasks[idx_alice]["attempt_no"] == 2  # 第 1 次 temp failure 后下一序号为 2


# ---------- 参数校验 ----------

@pytest.mark.parametrize("body", [
    {"max_claims": 0, "lease_seconds": 300},
    {"max_claims": 101, "lease_seconds": 300},
    {"max_claims": -1, "lease_seconds": 300},
    {"max_claims": 10, "lease_seconds": 0},
    {"max_claims": 10, "lease_seconds": 3601},
    {"max_claims": 10},
    {"lease_seconds": 10},
])
def test_invalid_claim_params_rejected(client, body):
    setup(client)
    resp = client.post("/deliveries/claims", json=body)
    assert resp.status_code == 422, resp.text


def test_invalid_result_on_complete_rejected(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    resp = client.post(f"/deliveries/claims/{token}/complete", json={"result": "bounced"})
    assert resp.status_code == 422


# ---------- 有效租约：不重复分配 / 并发 ----------

def test_active_lease_not_assigned_again(client):
    setup(client)
    submit(client, "msg-1")
    first = claim(client).json()["tasks"]
    assert len(first) == 2
    assert claim(client).json()["tasks"] == []  # 两个终点都有有效租约


def test_concurrent_claims_never_double_allocate(client):
    setup(client)
    # 4 笔判定 × 每笔 2 终点 = 8 个可领任务
    for i in range(4):
        submit(client, f"msg-{i}", "multi@corp.example.com")
    assigned: list[tuple[str, str]] = []
    lock_tokens = []

    def worker(_):
        resp = claim(client, max_claims=1)
        for t in resp.json()["tasks"]:
            assigned.append((t["delivery_id"], t["mailbox"]))
            lock_tokens.append(t["token"])
        return resp.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(worker, range(16)))
    assert set(codes) == {200}
    assert len(assigned) == 8  # 每个终点恰好分配一次
    assert len(set(assigned)) == 8
    assert len(set(lock_tokens)) == 8  # 令牌互不相同
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM endpoint_leases").fetchone()["n"] == 8
    conn.close()


# ---------- 凭令牌完成：原子回执 + 结束租约 ----------

def test_complete_with_valid_token_writes_receipt_and_closes(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    resp = complete(client, token, "delivered")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "delivery_id": "msg-1",
        "mailbox": "alice@mail.example.com",
        "attempt_no": 1,
        "result": "delivered",
        "created_at": resp.json()["created_at"],
    }
    # 既有回执查询接口立即可见该记录
    summary = client.get("/deliveries/msg-1/receipts").json()
    ep = next(e for e in summary["endpoints"] if e["mailbox"] == "alice@mail.example.com")
    assert ep["status"] == "delivered" and ep["attempts"][0]["attempt_no"] == 1
    # 租约已结束：同一终点可再次入列前不会被领取；delivered 终结 → 永不入列
    assert not [t for t in claim(client).json()["tasks"]
                if (t["delivery_id"], t["mailbox"]) == ("msg-1", "alice@mail.example.com")]


def test_completed_token_rejected(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    assert complete(client, token, "delivered").status_code == 200
    resp = complete(client, token, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_lease_token" and detail["reason"] == "completed"


def test_unknown_token_conflict(client):
    setup(client)
    resp = complete(client, "0" * 32, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_lease_token" and detail["reason"] == "unknown"


def test_expired_token_rejected_and_endpoint_reclaimable(client):
    setup(client)
    submit(client, "msg-1")
    by = tasks_by(client, "mailbox")
    old_token = by["alice@mail.example.com"]["token"]
    expire_lease(old_token)
    # 过期令牌提交 → 明确冲突（expired），不写入回执
    resp = complete(client, old_token, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "expired"
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert all(e["attempts"] == [] for e in summary["endpoints"])
    # 到期可重领：新令牌；旧令牌因行轮换变为 unknown
    new_task = next(t for t in claim(client).json()["tasks"]
                    if t["mailbox"] == "alice@mail.example.com")
    assert new_task["token"] != old_token
    again = complete(client, old_token, "delivered")
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "unknown"
    # 新令牌有效，完成成功
    assert complete(client, new_task["token"], "delivered").status_code == 200


def test_expiry_boundary_is_strict(client):
    # lease_expires_at == 当前时刻视为已到期（> 才算有效）
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    expire_lease(token)  # 前移 1 秒，严格小于当前时刻
    assert complete(client, token, "delivered").status_code == 409


# ---------- 退避重试：60×2^(n−1)，上限 3600 ----------

def test_temporary_failure_backoff_rounds(client):
    # HTTP 全流程：第 1 次 temp failure → 60s 退避边界（59s 不可领 / 60s 可领，序号 2）；
    # 第 2 次 temp failure → 120s 退避后序号 3；最后 delivered。
    setup(client)
    submit(client, "msg-1", "single@corp.example.com")  # carol 单终点
    token = tasks_by(client, "mailbox")["carol@mail.example.com"]["token"]
    assert complete(client, token, "temporary_failure").status_code == 200

    # 旧租约已结束，但退避未到：差 1 秒不可领
    shift_attempt_ago("msg-1", "carol@mail.example.com", 59)
    assert [t for t in claim(client).json()["tasks"]
            if t["mailbox"] == "carol@mail.example.com"] == []
    # 恰好 60 秒：可领，下一序号为 2
    shift_attempt_ago("msg-1", "carol@mail.example.com", 60)
    task2 = next(t for t in claim(client).json()["tasks"]
                 if t["mailbox"] == "carol@mail.example.com")
    assert task2["attempt_no"] == 2
    assert complete(client, task2["token"], "temporary_failure").status_code == 200

    # 第 2 次退避为 120 秒：119 秒不可领
    shift_attempt_ago("msg-1", "carol@mail.example.com", 119)
    assert [t for t in claim(client).json()["tasks"]
            if t["mailbox"] == "carol@mail.example.com"] == []
    shift_attempt_ago("msg-1", "carol@mail.example.com", 120)
    task3 = next(t for t in claim(client).json()["tasks"]
                 if t["mailbox"] == "carol@mail.example.com")
    assert task3["attempt_no"] == 3
    assert complete(client, task3["token"], "delivered").status_code == 200
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "delivered"
    assert [a["attempt_no"] for a in summary["endpoints"][0]["attempts"]] == [1, 2, 3]


def test_backoff_delay_formula():
    assert store.backoff_delay_seconds(1) == 60
    assert store.backoff_delay_seconds(2) == 120
    assert store.backoff_delay_seconds(3) == 240
    assert store.backoff_delay_seconds(4) == 480
    assert store.backoff_delay_seconds(5) == 960
    assert store.backoff_delay_seconds(6) == 1920
    assert store.backoff_delay_seconds(7) == 3600
    assert store.backoff_delay_seconds(8) == 3600  # 封顶 3600
    assert store.backoff_delay_seconds(20) == 3600


def test_permanent_failure_via_claim_is_terminal(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["bob@mail.example.com"]["token"]
    assert complete(client, token, "permanent_failure").status_code == 200
    # 租约结束、终结结果：无论过多久都不再入列
    assert not [t for t in claim(client).json()["tasks"]
                if (t["delivery_id"], t["mailbox"]) == ("msg-1", "bob@mail.example.com")]
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "failed"


# ---------- 既有回执接口先记录 → 令牌失效且不覆盖 ----------

def test_existing_receipt_api_supersedes_lease(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]
    # 投递系统绕过租约，用既有回执接口先记录了同一终点的 attempt 1
    resp = client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
    })
    assert resp.status_code == 200
    # 旧令牌提交 → 409 superseded，且不得覆盖既有回执
    conflict = complete(client, token, "temporary_failure")
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["error"] == "invalid_lease_token" and detail["reason"] == "superseded"
    ep = next(e for e in client.get("/deliveries/msg-1/receipts").json()["endpoints"]
              if e["mailbox"] == "alice@mail.example.com")
    assert len(ep["attempts"]) == 1 and ep["attempts"][0]["result"] == "delivered"


def test_later_attempt_via_existing_api_supersedes_lease(client):
    setup(client)
    submit(client, "msg-1")
    token = tasks_by(client, "mailbox")["alice@mail.example.com"]["token"]  # attempt_no=1
    # 既有接口先记了更晚的 attempt 5（允许跳号）
    resp = client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 5, "result": "temporary_failure",
    })
    assert resp.status_code == 200
    conflict = complete(client, token, "delivered")
    assert conflict.status_code == 409 and conflict.json()["detail"]["reason"] == "superseded"


# ---------- 与既有功能解耦：查询、重放、熔断不受影响 ----------

def test_claims_do_not_change_decisions_or_replay(client):
    setup(client)
    decision = submit(client, "msg-1")
    claim(client)
    stored = client.get("/deliveries/msg-1").json()
    assert stored["status"] == "ok" and stored["endpoints"] == decision["endpoints"]
    replay = client.post("/deliveries", json={"delivery_id": "msg-1", "address": "MULTI@CORP.EXAMPLE.COM"})
    assert replay.status_code == 200 and replay.json() == decision


def test_claims_do_not_feed_circuit_breaker(client):
    # 领取与租约完成只写 endpoint_leases / delivery_attempts，不触发熔断统计
    assert client.put("/draft", json={"revision": 0, "rules": [
        {"match_type": "exact", "match_key": "ok@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "m@mail.example.com"}]},
        {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject", "targets": []},
    ]}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200
    assert client.put("/policy/circuit-breaker", json={
        "revision": 1, "failure_threshold_percent": 50,
        "min_candidate_samples": 2, "window_minutes": 5,
    }).status_code == 200
    submit(client, "msg-ok", "ok@corp.example.com")
    token = tasks_by(client, "mailbox")["m@mail.example.com"]["token"]
    # 领取 + 完成一次回执：不影响熔断统计
    assert complete(client, token, "temporary_failure").status_code == 200
    assert client.get("/policy").json()["revision"] == 1
    assert client.get("/policy/circuit-breaker").json()["breaks"] == []
