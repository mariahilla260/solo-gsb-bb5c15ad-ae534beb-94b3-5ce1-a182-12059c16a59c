"""历史投递判定核对报表（GET /deliveries/report）：

按带时区的起止时刻（左闭右开）与可选策略修订号核对历史判定；分组计数 +
游标分页；首次查询固定快照边界，后续翻页沿用同一筛选与边界，并发新投递
不进入旧报表；无效时间窗 / 非法页大小 / 无效游标明确拒绝（422）。
"""
import json
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

# 覆盖全部测试判定的宽时间窗（判定 created_at 为当前时刻）
WIDE_START = "2000-01-01T00:00:00+00:00"
WIDE_END = "2100-01-01T00:00:00+00:00"


def publish_two_versions(client):
    assert client.put("/draft", json={"revision": 0, "rules": BASE_RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/draft", json={"revision": 1, "rules": CANDIDATE_RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 2}).status_code == 200
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


def insert_decision(delivery_id, *, created_at, status="ok", version=1, selection="baseline",
                    policy_revision=1, error=None, endpoints=None):
    """直接落库一条判定（可控 created_at，用于时间窗边界测试）。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO delivery_decisions ("
            " delivery_id, address, policy_revision, baseline_version, candidate_version,"
            " candidate_percent, selection, version, status, endpoints, error, created_at)"
            " VALUES (?, ?, ?, 1, 2, 50, ?, ?, ?, ?, ?, ?)",
            (
                delivery_id,
                f"{delivery_id.lower()}@corp.example.com",
                policy_revision,
                selection,
                version,
                status,
                json.dumps(endpoints) if endpoints is not None else None,
                json.dumps(error) if error is not None else None,
                created_at,
            ),
        )
    finally:
        conn.close()


def report(client, **params):
    return client.get("/deliveries/report", params=params)


def fetch_all_pages(client, **params):
    """逐页取完一份报表，返回 (每页响应体列表, 全部明细)。"""
    pages, items = [], []
    resp = report(client, **params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pages.append(body)
    items.extend(body["items"])
    while body["next_cursor"] is not None:
        resp = report(client, cursor=body["next_cursor"], page_size=params.get("page_size"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append(body)
        items.extend(body["items"])
    return pages, items


# ---------- 分组计数与明细 ----------

def test_report_groups_by_version_selection_outcome(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全部走候选
    assert submit(client, "r-1", "shared@corp.example.com").status_code == 200      # candidate ok
    assert submit(client, "r-2", "blocked@corp.example.com").status_code == 422     # candidate rejected
    assert submit(client, "r-3", "stranger@corp.example.com").status_code == 422    # candidate no_route
    assert set_policy(client, 1, v1, v2, 0).status_code == 200  # 改为全部走基线
    assert submit(client, "r-4", "shared@corp.example.com").status_code == 200      # baseline ok
    assert submit(client, "r-5", "other@corp.example.com").status_code == 200       # baseline ok（域兜底）

    resp = report(client, start=WIDE_START, end=WIDE_END)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["groups"] == [
        {"version": v1, "selection": "baseline", "outcome": "ok", "count": 2},
        {"version": v2, "selection": "candidate", "outcome": "no_route", "count": 1},
        {"version": v2, "selection": "candidate", "outcome": "ok", "count": 1},
        {"version": v2, "selection": "candidate", "outcome": "rejected", "count": 1},
    ]
    assert sum(g["count"] for g in body["groups"]) == body["total"]
    assert len(body["items"]) == 5  # 默认页大小足以装下
    assert body["next_cursor"] is None

    by_id = {item["delivery_id"]: item for item in body["items"]}
    assert by_id["r-1"] == {
        "delivery_id": "r-1",
        "address": "shared@corp.example.com",
        "policy_revision": 1,
        "version": v2,
        "selection": "candidate",
        "status": "ok",
        "error_type": None,
        "created_at": by_id["r-1"]["created_at"],
    }
    assert by_id["r-2"]["status"] == "failed" and by_id["r-2"]["error_type"] == "rejected"
    assert by_id["r-3"]["status"] == "failed" and by_id["r-3"]["error_type"] == "no_route"
    assert by_id["r-4"]["policy_revision"] == 2 and by_id["r-4"]["selection"] == "baseline"
    assert by_id["r-4"]["version"] == v1 and by_id["r-4"]["status"] == "ok"
    assert all(item["created_at"] for item in body["items"])


def test_report_counts_cycle_failure_by_error_type(client):
    publish_two_versions(client)
    insert_decision(
        "r-cycle", created_at="2026-01-01T00:00:00+00:00", status="failed",
        error={"error": "cycle", "address": "loop@corp.example.com", "chain": []},
    )
    body = report(client, start=WIDE_START, end=WIDE_END).json()
    assert body["groups"] == [
        {"version": 1, "selection": "baseline", "outcome": "cycle", "count": 1}
    ]
    assert body["items"][0]["error_type"] == "cycle"
    assert body["items"][0]["status"] == "failed"


def test_report_counts_each_delivery_id_once(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    assert submit(client, "r-dup", "shared@corp.example.com").status_code == 200
    replay = submit(client, "r-dup", "SHARED@CORP.EXAMPLE.COM")  # 重放，不产生新判定
    assert replay.status_code == 200
    body = report(client, start=WIDE_START, end=WIDE_END).json()
    assert body["total"] == 1
    assert [item["delivery_id"] for item in body["items"]] == ["r-dup"]


def test_report_policy_revision_filter(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "r-p1", "shared@corp.example.com")            # policy_revision 1
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    submit(client, "r-p2a", "shared@corp.example.com")           # policy_revision 2
    submit(client, "r-p2b", "other@corp.example.com")            # policy_revision 2

    body = report(client, start=WIDE_START, end=WIDE_END, policy_revision=2).json()
    assert body["total"] == 2
    assert {item["delivery_id"] for item in body["items"]} == {"r-p2a", "r-p2b"}
    assert all(item["policy_revision"] == 2 for item in body["items"])
    assert body["groups"] == [
        {"version": v1, "selection": "baseline", "outcome": "ok", "count": 2}
    ]
    assert body["window"]["policy_revision"] == 2

    # 不带策略修订号时两个修订的判定都在
    body = report(client, start=WIDE_START, end=WIDE_END).json()
    assert body["total"] == 3
    assert body["window"]["policy_revision"] is None


# ---------- 时间窗（左闭右开、带时区） ----------

def test_report_window_is_left_closed_right_open(client):
    publish_two_versions(client)
    insert_decision("r-before", created_at="2026-03-01T09:59:59+00:00")
    insert_decision("r-start", created_at="2026-03-01T10:00:00+00:00")   # 恰好起点 → 计入
    insert_decision("r-inside", created_at="2026-03-01T10:30:00+00:00")
    insert_decision("r-end", created_at="2026-03-01T11:00:00+00:00")     # 恰好终点 → 不计入
    insert_decision("r-after", created_at="2026-03-01T11:00:01+00:00")

    body = report(
        client,
        start="2026-03-01T10:00:00+00:00",
        end="2026-03-01T11:00:00+00:00",
    ).json()
    assert [item["delivery_id"] for item in body["items"]] == ["r-start", "r-inside"]
    assert body["total"] == 2
    assert sum(g["count"] for g in body["groups"]) == 2


def test_report_window_accepts_timezone_offsets(client):
    publish_two_versions(client)
    insert_decision("r-tz", created_at="2026-03-01T10:00:00+00:00")
    # 同一时刻用 +08:00 与 UTC 两种写法，窗口等价
    body = report(
        client,
        start="2026-03-01T17:59:59+08:00",
        end="2026-03-01T19:00:01+08:00",
    ).json()
    assert [item["delivery_id"] for item in body["items"]] == ["r-tz"]
    assert body["window"]["start"] == "2026-03-01T09:59:59+00:00"  # 规范化为 UTC
    assert body["window"]["end"] == "2026-03-01T11:00:01+00:00"
    # Z 写法同样接受
    body = report(client, start="2026-03-01T10:00:00Z", end="2026-03-01T10:00:01Z").json()
    assert [item["delivery_id"] for item in body["items"]] == ["r-tz"]


def test_report_empty_window_returns_zero_counts_and_empty_page(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "r-outside", "shared@corp.example.com")  # 在当前时刻，不在历史窗口内
    body = report(
        client, start="2020-01-01T00:00:00+00:00", end="2020-01-02T00:00:00+00:00"
    ).json()
    assert body["total"] == 0
    assert body["groups"] == []
    assert body["items"] == []
    assert body["next_cursor"] is None


# ---------- 分页：同一筛选与边界、不漏不重、并发新投递不进入 ----------

def test_report_pagination_no_gaps_no_duplicates(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)  # 全部走基线，都能成功
    expected = []
    for i in range(25):
        delivery_id = f"r-page-{i:02d}"
        assert submit(client, delivery_id, f"user{i}@corp.example.com").status_code == 200
        expected.append(delivery_id)

    pages, items = fetch_all_pages(client, start=WIDE_START, end=WIDE_END, page_size=10)
    assert [len(p["items"]) for p in pages] == [10, 10, 5]
    got = [item["delivery_id"] for item in items]
    assert got == expected  # 按提交顺序完整列出，不漏不重
    assert len(set(got)) == 25
    # 每一页的分组与总数都基于同一边界，完全一致
    for page in pages:
        assert page["total"] == 25
        assert page["groups"] == [
            {"version": v1, "selection": "baseline", "outcome": "ok", "count": 25}
        ]
        assert page["window"] == {"start": WIDE_START, "end": WIDE_END, "policy_revision": None}
    assert pages[-1]["next_cursor"] is None


def test_report_page_size_can_change_between_pages(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    for i in range(7):
        submit(client, f"r-ps-{i}", f"ps{i}@corp.example.com")
    first = report(client, start=WIDE_START, end=WIDE_END, page_size=3).json()
    assert len(first["items"]) == 3
    rest = report(client, cursor=first["next_cursor"], page_size=100).json()
    assert len(rest["items"]) == 4
    assert rest["next_cursor"] is None
    got = [i["delivery_id"] for i in first["items"] + rest["items"]]
    assert got == [f"r-ps-{i}" for i in range(7)]


def test_report_snapshot_excludes_concurrent_new_deliveries(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    for i in range(3):
        submit(client, f"r-old-{i}", f"old{i}@corp.example.com")

    first = report(client, start=WIDE_START, end=WIDE_END, page_size=2).json()
    assert [i["delivery_id"] for i in first["items"]] == ["r-old-0", "r-old-1"]
    assert first["total"] == 3

    # 首次查询之后、翻页之前：并发新投递（落在同一时间窗内）
    assert submit(client, "r-new", "new@corp.example.com").status_code == 200

    # 后续翻页沿用同一边界：新投递不进入旧报表，分组与总数不变
    second = report(client, cursor=first["next_cursor"]).json()
    assert [i["delivery_id"] for i in second["items"]] == ["r-old-2"]
    assert second["next_cursor"] is None
    assert second["total"] == 3
    assert second["groups"] == first["groups"]

    # 重新发起首次查询则是新报表：包含新投递
    fresh = report(client, start=WIDE_START, end=WIDE_END).json()
    assert fresh["total"] == 4
    assert [i["delivery_id"] for i in fresh["items"]] == ["r-old-0", "r-old-1", "r-old-2", "r-new"]


def test_report_cursor_keeps_original_filter(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "r-f1", "shared@corp.example.com")
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    submit(client, "r-f2", "shared@corp.example.com")

    first = report(client, start=WIDE_START, end=WIDE_END, policy_revision=2, page_size=1).json()
    assert [i["delivery_id"] for i in first["items"]] == ["r-f2"]
    assert first["next_cursor"] is None  # 只有一条，无后续页

    # 多造一些同修订判定，验证翻页仍带 policy_revision=2 的筛选
    submit(client, "r-f3", "other@corp.example.com")
    pages, items = fetch_all_pages(client, start=WIDE_START, end=WIDE_END,
                                   policy_revision=2, page_size=1)
    assert [i["delivery_id"] for i in items] == ["r-f2", "r-f3"]
    for page in pages:
        assert page["window"]["policy_revision"] == 2  # 翻页沿用首次筛选


# ---------- 明确拒绝：无效时间窗 / 非法页大小 / 无效游标 ----------

def test_report_invalid_time_window_rejected(client):
    publish_two_versions(client)
    # 缺 start / end
    assert report(client).status_code == 422
    assert report(client, start=WIDE_START).status_code == 422
    resp = report(client, end=WIDE_END)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_time_window"
    # 不带时区
    resp = report(client, start="2026-03-01T10:00:00", end="2026-03-01T11:00:00")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_time_window"
    # 无法解析
    assert report(client, start="not-a-time", end=WIDE_END).status_code == 422
    # start >= end（空窗口 / 倒窗）
    resp = report(client, start="2026-03-01T11:00:00+00:00", end="2026-03-01T10:00:00+00:00")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_time_window"
    resp = report(client, start="2026-03-01T10:00:00+00:00", end="2026-03-01T10:00:00+00:00")
    assert resp.status_code == 422


def test_report_invalid_page_size_rejected(client):
    publish_two_versions(client)
    for bad in (0, -1, 501):
        resp = report(client, start=WIDE_START, end=WIDE_END, page_size=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["detail"]["error"] == "invalid_page_size"
    assert report(client, start=WIDE_START, end=WIDE_END, page_size="abc").status_code == 422
    # 边界值合法
    assert report(client, start=WIDE_START, end=WIDE_END, page_size=1).status_code == 200
    assert report(client, start=WIDE_START, end=WIDE_END, page_size=500).status_code == 200


def test_report_invalid_cursor_rejected(client):
    publish_two_versions(client)
    # 完全无法解码
    resp = report(client, cursor="not-a-cursor")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_cursor"
    # 可解码但形状不对
    import base64 as b64
    for payload in ({"v": 99}, {"v": 1, "start": "x"}, {"foo": "bar"},
                    {"v": 1, "start": WIDE_START, "end": WIDE_END,
                     "policy_revision": None, "snapshot": -1, "after": 0},
                    {"v": 1, "start": WIDE_END, "end": WIDE_START,  # 游标内窗口无效
                     "policy_revision": None, "snapshot": 0, "after": 0}):
        token = b64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        resp = report(client, cursor=token)
        assert resp.status_code == 422, payload
        assert resp.json()["detail"]["error"] == "invalid_cursor"


def test_report_cursor_must_not_be_combined_with_filters(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    for i in range(3):
        submit(client, f"r-c-{i}", f"c{i}@corp.example.com")
    first = report(client, start=WIDE_START, end=WIDE_END, page_size=2).json()
    cursor = first["next_cursor"]
    assert cursor is not None
    # 游标已携带筛选与边界：再传 start/end/policy_revision 一律拒绝
    for extra in ({"start": WIDE_START}, {"end": WIDE_END}, {"policy_revision": 1}):
        resp = report(client, cursor=cursor, **extra)
        assert resp.status_code == 422, extra
        assert resp.json()["detail"]["error"] == "invalid_cursor"
    # 但允许带 page_size 翻页
    assert report(client, cursor=cursor, page_size=1).status_code == 200


# ---------- 与既有接口兼容 ----------

def test_report_route_does_not_shadow_get_delivery(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "r-keep", "shared@corp.example.com")
    # 静态路径 /deliveries/report 优先于 /deliveries/{delivery_id}
    assert report(client, start=WIDE_START, end=WIDE_END).status_code == 200
    # 按编号查询不受影响
    stored = client.get("/deliveries/r-keep")
    assert stored.status_code == 200
    assert stored.json()["delivery_id"] == "r-keep"
    assert client.get("/deliveries/nope").status_code == 404


def test_report_does_not_change_existing_semantics(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit(client, "r-replay", "shared@corp.example.com")
    assert first.status_code == 200
    # 查询报表后：重放仍原样返回，/deliver 不产生判定，/resolve 不受影响
    report(client, start=WIDE_START, end=WIDE_END)
    assert submit(client, "r-replay", "shared@corp.example.com").json() == first.json()
    assert "delivery_id" not in client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    resp = client.post("/resolve", json={"address": "shared@corp.example.com", "version": v1})
    assert resp.status_code == 200
    assert [e["mailbox"] for e in resp.json()["endpoints"]] == ["base@mail.example.com"]
