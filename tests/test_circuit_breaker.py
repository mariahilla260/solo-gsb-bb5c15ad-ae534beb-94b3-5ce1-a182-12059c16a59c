"""灰度自动熔断（PUT / GET /policy/circuit-breaker + POST /deliveries 触发评估）：

操作方凭当前策略修订号设置失败率阈值（1–100%）、最少候选判定数与观察窗口（分钟）；
每次首次提交投递判定持久化后，仅统计同一策略修订号在窗口内已持久化的候选判定，
样本达下限且失败数占比达到阈值时只触发一次：候选比例置 0、策略修订号 +1，
并记录旧修订号、样本数、失败数与触发时间。触发请求仍返回原判定；
同编号重放不重复计数；人工更新策略后旧设置失效，须针对新修订重新设置。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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


def set_breaker(client, revision, threshold=50, min_samples=3, window_minutes=10):
    return client.put("/policy/circuit-breaker", json={
        "revision": revision,
        "failure_threshold_percent": threshold,
        "min_candidate_samples": min_samples,
        "window_minutes": window_minutes,
    })


def submit(client, delivery_id, address):
    return client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})


def breaker_state(client):
    return client.get("/policy/circuit-breaker")


def backdate_decision(delivery_id, *, minutes):
    """把某条判定的受理时刻改到过去（模拟窗口外的旧判定）。"""
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE delivery_decisions SET created_at = ? WHERE delivery_id = ?",
            (old, delivery_id),
        )
    finally:
        conn.close()


# ---------- 设置与查询 ----------

def test_put_settings_without_policy_is_409(client):
    publish_two_versions(client)
    resp = set_breaker(client, 0)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    # 未配置策略时不产生任何设置
    assert client.get("/policy/circuit-breaker").status_code == 404


def test_get_circuit_breaker_without_policy_is_404(client):
    resp = client.get("/policy/circuit-breaker")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "policy_not_configured"


def test_put_and_get_settings(client):
    v1, v2 = publish_two_versions(client)
    assert set_policy(client, 0, v1, v2, 20).status_code == 200
    resp = set_breaker(client, 1, threshold=40, min_samples=5, window_minutes=15)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["policy_revision"] == 1
    assert body["failure_threshold_percent"] == 40
    assert body["min_candidate_samples"] == 5
    assert body["window_minutes"] == 15
    assert "updated_at" in body

    state = breaker_state(client)
    assert state.status_code == 200
    got = state.json()
    assert got["policy_revision"] == 1
    assert got["settings"] == body
    assert got["breaks"] == []


def test_get_settings_null_when_not_set(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 20)
    state = breaker_state(client).json()
    assert state["policy_revision"] == 1
    assert state["settings"] is None
    assert state["breaks"] == []


def test_put_settings_stale_revision_conflict_keeps_old(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 20)
    assert set_breaker(client, 1, threshold=40).status_code == 200
    resp = set_breaker(client, 0, threshold=90)  # 落后的修订号
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 1
    # 冲突不覆盖原设置
    assert breaker_state(client).json()["settings"]["failure_threshold_percent"] == 40


def test_put_settings_invalid_params_rejected(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 20)
    assert set_breaker(client, 1, threshold=0).status_code == 422
    assert set_breaker(client, 1, threshold=101).status_code == 422
    assert set_breaker(client, 1, min_samples=0).status_code == 422
    assert set_breaker(client, 1, min_samples=-2).status_code == 422
    assert set_breaker(client, 1, window_minutes=0).status_code == 422
    assert set_breaker(client, 1, window_minutes=-5).status_code == 422
    # 非法参数不落任何设置
    assert breaker_state(client).json()["settings"] is None


def test_manual_policy_update_invalidates_settings(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 20)
    assert set_breaker(client, 1).status_code == 200
    # 人工更新策略 → 修订号 +1，旧设置失效
    assert set_policy(client, 1, v1, v2, 30).status_code == 200
    state = breaker_state(client).json()
    assert state["policy_revision"] == 2
    assert state["settings"] is None
    # 旧修订号不能再设置；须凭新修订号重新设置
    assert set_breaker(client, 1).status_code == 409
    assert set_breaker(client, 2, threshold=60).status_code == 200
    assert breaker_state(client).json()["settings"]["failure_threshold_percent"] == 60


# ---------- 触发：阈值、样本下限、只触发一次 ----------

def test_trigger_sets_percent_zero_bumps_revision_and_records(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全部走候选；候选对 blocked@ 拒收、未知地址 no_route
    assert set_breaker(client, 1, threshold=50, min_samples=3, window_minutes=10).status_code == 200

    # 前两次失败：样本未达下限，不触发
    assert submit(client, "t-1", "blocked@corp.example.com").status_code == 422
    assert submit(client, "t-2", "stranger@corp.example.com").status_code == 422
    assert client.get("/policy").json()["candidate_percent"] == 100
    assert breaker_state(client).json()["breaks"] == []

    # 第三次失败：样本 3、失败占比 100% ≥ 50% → 触发；但本次仍返回原判定
    third = submit(client, "t-3", "blocked@corp.example.com")
    assert third.status_code == 422
    detail = third.json()["detail"]
    assert detail["error"] == "rejected"
    assert detail["selection"] == "candidate"
    assert detail["version"] == v2
    assert detail["policy_revision"] == 1

    # 策略：候选比例置 0、修订号 +1；旧设置随之失效
    policy = client.get("/policy").json()
    assert policy["revision"] == 2
    assert policy["candidate_percent"] == 0
    assert policy["baseline_version"] == v1 and policy["candidate_version"] == v2
    state = breaker_state(client).json()
    assert state["settings"] is None
    assert len(state["breaks"]) == 1
    record = state["breaks"][0]
    assert record["policy_revision"] == 1          # 旧修订号
    assert record["sample_count"] == 3             # 窗口内候选样本数
    assert record["failure_count"] == 3            # 其中失败数
    assert record["triggered_at"]

    # 后续默认投递按基线执行
    follow = client.post("/deliver", json={"address": "blocked@corp.example.com"})
    assert follow.status_code == 200
    assert follow.json()["selection"] == "baseline"
    assert follow.json()["version"] == v1
    assert follow.json()["policy_revision"] == 2
    new_sub = submit(client, "t-4", "blocked@corp.example.com")
    assert new_sub.status_code == 200
    assert new_sub.json()["selection"] == "baseline"

    # 历史判定不改写：触发前的失败判定原样可查、原样重放
    stored = client.get("/deliveries/t-3").json()
    assert stored["status"] == "failed"
    assert stored["selection"] == "candidate"
    assert stored["version"] == v2
    assert stored["policy_revision"] == 1
    replay = submit(client, "t-3", "blocked@corp.example.com")
    assert replay.status_code == 422
    assert replay.json() == third.json()

    # 只触发一次：后续投递不再产生新的熔断记录
    submit(client, "t-5", "stranger@corp.example.com")
    assert len(breaker_state(client).json()["breaks"]) == 1


def test_trigger_on_ok_request_still_returns_original_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=50, min_samples=2, window_minutes=10).status_code == 200
    assert submit(client, "ok-1", "blocked@corp.example.com").status_code == 422  # 1/1，样本未达下限
    # 第二次为成功判定：1/2 = 50% 达到阈值 → 触发，但本次仍返回原（成功）判定
    resp = submit(client, "ok-2", "shared@corp.example.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["selection"] == "candidate"
    assert body["version"] == v2
    assert body["policy_revision"] == 1
    assert body["candidate_percent"] == 100
    assert [e["mailbox"] for e in body["endpoints"]] == ["cand@mail.example.com"]
    # 熔断已生效
    assert client.get("/policy").json()["candidate_percent"] == 0
    record = breaker_state(client).json()["breaks"][0]
    assert record["sample_count"] == 2
    assert record["failure_count"] == 1


def test_threshold_boundary_exact_ratio_triggers(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=60, min_samples=4, window_minutes=10).status_code == 200
    # 2/4 = 50% < 60%：不触发
    assert submit(client, "b-1", "blocked@corp.example.com").status_code == 422
    assert submit(client, "b-2", "stranger@corp.example.com").status_code == 422
    assert submit(client, "b-3", "shared@corp.example.com").status_code == 200
    assert submit(client, "b-4", "shared@corp.example.com").status_code == 200
    assert breaker_state(client).json()["breaks"] == []
    # 3/5 = 60% 达到阈值：触发
    assert submit(client, "b-5", "blocked@corp.example.com").status_code == 422
    record = breaker_state(client).json()["breaks"][0]
    assert record["sample_count"] == 5
    assert record["failure_count"] == 3


def test_only_candidate_decisions_counted(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)  # 全部走基线
    assert set_breaker(client, 1, threshold=1, min_samples=1, window_minutes=10).status_code == 200
    # 基线侧失败（未知域 no_route）不计入候选样本
    assert submit(client, "c-1", "ghost@other.example.net").status_code == 422
    assert submit(client, "c-2", "ghost2@other.example.net").status_code == 422
    assert breaker_state(client).json()["breaks"] == []
    assert client.get("/policy").json()["candidate_percent"] == 0


def test_only_same_policy_revision_counted(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=50, min_samples=3, window_minutes=10).status_code == 200
    # 修订 1 下两次候选失败
    assert submit(client, "r-1", "blocked@corp.example.com").status_code == 422
    assert submit(client, "r-2", "stranger@corp.example.com").status_code == 422
    # 人工更新策略（修订 1 → 2，仍 100% 候选），旧设置失效，针对新修订重新设置
    assert set_policy(client, 1, v1, v2, 100).status_code == 200
    assert set_breaker(client, 2, threshold=50, min_samples=3, window_minutes=10).status_code == 200
    # 修订 2 下两次候选失败：旧修订的两次不计入 → 样本 2 < 3，不触发
    assert submit(client, "r-3", "blocked@corp.example.com").status_code == 422
    assert submit(client, "r-4", "stranger@corp.example.com").status_code == 422
    assert breaker_state(client).json()["breaks"] == []
    # 修订 2 下第三次：样本 3、失败 3 → 触发，记录的是修订 2
    assert submit(client, "r-5", "blocked@corp.example.com").status_code == 422
    record = breaker_state(client).json()["breaks"][0]
    assert record["policy_revision"] == 2
    assert record["sample_count"] == 3
    assert record["failure_count"] == 3
    assert client.get("/policy").json()["revision"] == 3


def test_replay_does_not_double_count(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=50, min_samples=2, window_minutes=10).status_code == 200
    first = submit(client, "p-1", "blocked@corp.example.com")
    assert first.status_code == 422
    # 同编号重放多次：不重复计数（否则 1 次失败就会被算成多样本并触发）
    for _ in range(5):
        replay = submit(client, "p-1", "blocked@corp.example.com")
        assert replay.status_code == 422
        assert replay.json() == first.json()
    assert breaker_state(client).json()["breaks"] == []
    # 再首次提交一条成功判定：样本 2、失败 1 = 50% → 恰好触发
    assert submit(client, "p-2", "shared@corp.example.com").status_code == 200
    record = breaker_state(client).json()["breaks"][0]
    assert record["sample_count"] == 2
    assert record["failure_count"] == 1


def test_window_excludes_old_decisions(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=50, min_samples=2, window_minutes=1).status_code == 200
    assert submit(client, "w-1", "blocked@corp.example.com").status_code == 422
    backdate_decision("w-1", minutes=10)  # 移出 1 分钟观察窗口
    # 窗口内只剩 w-2 一条：样本 1 < 2，不触发
    assert submit(client, "w-2", "blocked@corp.example.com").status_code == 422
    assert breaker_state(client).json()["breaks"] == []
    # 窗口内 w-2、w-3 两条：2/2 = 100% → 触发
    assert submit(client, "w-3", "blocked@corp.example.com").status_code == 422
    record = breaker_state(client).json()["breaks"][0]
    assert record["sample_count"] == 2
    assert record["failure_count"] == 2


def test_no_settings_no_trigger(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    for i in range(5):
        assert submit(client, f"n-{i}", "blocked@corp.example.com").status_code == 422
    policy = client.get("/policy").json()
    assert policy["candidate_percent"] == 100
    assert policy["revision"] == 1
    assert breaker_state(client).json()["breaks"] == []


def test_deliver_does_not_participate_in_breaker_stats(client):
    # /deliver 不产生持久化判定，不参与熔断统计
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=1, min_samples=1, window_minutes=10).status_code == 200
    for _ in range(5):
        resp = client.post("/deliver", json={"address": "blocked@corp.example.com"})
        assert resp.status_code == 422
    assert breaker_state(client).json()["breaks"] == []
    assert client.get("/policy").json()["candidate_percent"] == 100


# ---------- 并发：只触发一次，互不覆盖 ----------

def test_concurrent_submissions_trigger_exactly_once(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=1, min_samples=2, window_minutes=10).status_code == 200
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda i: submit(client, f"cc-{i}", "blocked@corp.example.com"), range(16)
        ))
    # 每个编号都受理成功（触发前为候选失败 422，触发后为基线成功 200）
    assert {r.status_code for r in responses} <= {200, 422}
    assert any(r.status_code == 422 for r in responses)
    # 只触发一次：一条熔断记录，策略修订号只 +1
    state = breaker_state(client).json()
    assert len(state["breaks"]) == 1
    assert state["breaks"][0]["policy_revision"] == 1
    policy = client.get("/policy").json()
    assert policy["revision"] == 2
    assert policy["candidate_percent"] == 0
    # 16 个编号各产生一份判定
    for i in range(16):
        assert client.get(f"/deliveries/cc-{i}").status_code == 200


def test_concurrent_manual_update_and_trigger_do_not_overwrite(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert set_breaker(client, 1, threshold=1, min_samples=1, window_minutes=10).status_code == 200

    def manual_update():
        # 凭所见修订号 1 人工更新；与并发触发串行化，后到者只能看到前者结果
        return set_policy(client, 1, v1, v2, 55)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(manual_update) for _ in range(2)]
        futures += [pool.submit(submit, client, f"m-{i}", "blocked@corp.example.com") for i in range(6)]
        results = [f.result() for f in futures]

    # 人工更新与自动熔断都基于修订号 1：只有一个能先落，另一个得到 409 或按新状态受理
    policy = client.get("/policy").json()
    manual_ok = [r for r in results[:2] if r.status_code == 200]
    manual_conflict = [r for r in results[:2] if r.status_code == 409]
    assert len(manual_ok) <= 1
    assert len(manual_ok) + len(manual_conflict) == 2
    breaks = breaker_state(client).json()["breaks"]
    if manual_ok:
        # 人工更新先到：修订号变为 2，旧设置失效 → 不再触发
        assert policy["candidate_percent"] == 55
        assert breaks == []
    else:
        # 熔断先到：候选比例置 0，人工更新全部冲突（不覆盖熔断结果）
        assert policy["candidate_percent"] == 0
        assert len(breaks) == 1
    # 无论哪种次序，策略修订号只从 1 前进 1 步，且结果一致可查
    assert policy["revision"] == 2
    assert client.get("/policy").json() == policy
