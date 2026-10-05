"""多人协作的草稿按键合并：POST /draft/changes + 草稿修订快照。

- upsert / delete 一组操作，键与内容沿用规范化与编辑期校验；
- 每次草稿变更（含整体替换 PUT /draft）留下完整修订快照；
- 所见修订落后时按“涉及键是否相对该快照改变”决定合并或冲突；
- 重复操作键、删除不存在的规则、无效规则整组拒绝；
- 无法取得所见修订快照时拒绝合并；并发提交原子核对和写入。
"""
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.main import app


def _reset():
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


@pytest.fixture()
def client():
    db.init_db()
    _reset()
    with TestClient(app) as c:
        yield c


def route_rule(key, *targets, match_type="exact"):
    return {
        "match_type": match_type,
        "match_key": key,
        "action": "route",
        "targets": [{"kind": t[0], "value": t[1]} for t in targets],
    }


def reject_rule(key, match_type="exact"):
    return {"match_type": match_type, "match_key": key, "action": "reject", "targets": []}


def changes(client, revision, ops):
    return client.post("/draft/changes", json={"revision": revision, "changes": ops})


def upsert(rule):
    return {"op": "upsert", **rule}


def delete_key(match_type, key):
    return {"op": "delete", "match_type": match_type, "match_key": key}


def keys_of(rules):
    return {(r["match_type"], r["match_key"]) for r in rules}


# ---------- 基本合并与修订快照 ----------

def test_initial_draft_has_revision_zero_snapshot(client):
    resp = client.get("/draft/revisions")
    assert resp.status_code == 200
    revisions = resp.json()["revisions"]
    assert [r["revision"] for r in revisions] == [0]
    assert revisions[0]["rule_count"] == 0
    snap = client.get("/draft/revisions/0").json()
    assert snap == {"revision": 0, "rules": []}


def test_upsert_group_creates_new_revision(client):
    resp = changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
        upsert(reject_rule("b@corp.example.com")),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert keys_of(body["rules"]) == {("exact", "a@corp.example.com"), ("exact", "b@corp.example.com")}
    draft = client.get("/draft").json()
    assert draft["revision"] == 1 and len(draft["rules"]) == 2


def test_keys_are_normalized_like_existing_rules(client):
    resp = changes(client, 0, [
        upsert(route_rule("  A@CORP.EXAMPLE.COM  ", ("mailbox", "X@MAIL.EXAMPLE.COM"))),
    ])
    assert resp.status_code == 200
    rule = resp.json()["rules"][0]
    assert rule["match_key"] == "a@corp.example.com"
    assert rule["targets"][0]["value"] == "x@mail.example.com"


def test_merge_upserts_and_deletes_in_one_group(client):
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ]).status_code == 200
    # 修订 1 上：改 a、删 b、加 c，一组完成
    resp = changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "z@mail.example.com"))),
        delete_key("exact", "b@corp.example.com"),
        upsert(route_rule("c@corp.example.com", ("mailbox", "w@mail.example.com"))),
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 2
    assert keys_of(body["rules"]) == {("exact", "a@corp.example.com"), ("exact", "c@corp.example.com")}
    a = next(r for r in body["rules"] if r["match_key"] == "a@corp.example.com")
    assert a["targets"] == [{"kind": "mailbox", "value": "z@mail.example.com"}]


def test_replace_draft_also_leaves_snapshot(client):
    # 合并产生 rev 1，整体替换产生 rev 2，两个修订都可核对
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200
    resp = client.put("/draft", json={"revision": 1, "rules": [
        route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com")),
    ]})
    assert resp.status_code == 200
    revisions = [r["revision"] for r in client.get("/draft/revisions").json()["revisions"]]
    assert revisions == [0, 1, 2]
    snap1 = client.get("/draft/revisions/1").json()
    assert keys_of(snap1["rules"]) == {("exact", "a@corp.example.com")}
    snap2 = client.get("/draft/revisions/2").json()
    assert keys_of(snap2["rules"]) == {("exact", "b@corp.example.com")}


def test_unknown_revision_snapshot_404(client):
    resp = client.get("/draft/revisions/99")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "draft_revision_not_found"


# ---------- 整组拒绝：重复操作键 / 删除不存在 / 无效规则 ----------

def test_duplicate_change_keys_rejected(client):
    resp = changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
        delete_key("exact", "A@corp.example.com"),  # 规范化后与上一个同键
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "duplicate_change_key"
    assert client.get("/draft").json()["revision"] == 0  # 草稿不变


def test_delete_nonexistent_rule_rejected(client):
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200
    resp = changes(client, 1, [
        delete_key("exact", "a@corp.example.com"),
        delete_key("exact", "missing@corp.example.com"),
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "rule_not_found"
    assert detail["match_key"] == "missing@corp.example.com"
    # 整组拒绝：a 仍在，修订号不增
    draft = client.get("/draft").json()
    assert draft["revision"] == 1
    assert keys_of(draft["rules"]) == {("exact", "a@corp.example.com")}


def test_delete_invalid_key_rejected(client):
    resp = changes(client, 0, [delete_key("exact", "not-an-address")])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_rule"


def test_invalid_upsert_rule_rejected(client):
    # route 无目标
    resp = changes(client, 0, [
        upsert({"match_type": "exact", "match_key": "a@corp.example.com",
                "action": "route", "targets": []}),
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_rule"
    # reject 带目标
    resp = changes(client, 0, [
        upsert(reject_rule("a@corp.example.com") | {"targets": [{"kind": "mailbox", "value": "x@mail.example.com"}]}),
    ])
    assert resp.status_code == 422
    # 无效目标地址
    resp = changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "broken"))),
    ])
    assert resp.status_code == 422
    assert client.get("/draft").json()["revision"] == 0


def test_delete_with_rule_fields_rejected(client):
    resp = client.post("/draft/changes", json={"revision": 0, "changes": [
        {"op": "delete", "match_type": "exact", "match_key": "a@corp.example.com",
         "action": "reject"},
    ]})
    assert resp.status_code == 422


def test_empty_changes_rejected(client):
    resp = client.post("/draft/changes", json={"revision": 0, "changes": []})
    assert resp.status_code == 422


def test_stale_revision_with_no_snapshot_allowed_when_keys_unchanged(client):
    """落后修订但涉及键未被他人改动：整组合并，产生一个新修订。"""
    # rev 0: a, b
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ]).status_code == 200
    # rev 2: 他人只改了 c（新增），a/b 相对 rev 0 的状态……注意 rev0 为空
    # 这里构造“所见为 rev1、他人只动不相关键”的场景：
    assert changes(client, 1, [
        upsert(route_rule("c@corp.example.com", ("mailbox", "z@mail.example.com"))),
    ]).status_code == 200
    # 客户端仍持 rev1，提交涉及 a（rev1 时已存在且未被 rev2 改动）与 d（新增键）
    resp = changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "new@mail.example.com"))),
        upsert(route_rule("d@corp.example.com", ("mailbox", "w@mail.example.com"))),
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 3
    assert keys_of(resp.json()["rules"]) == {
        ("exact", f"{n}@corp.example.com") for n in "abcd"
    }


def test_stale_revision_with_changed_key_conflicts(client):
    # rev1: a, b；rev2 他人改了 a
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ]).status_code == 200
    assert changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "other@mail.example.com"))),
    ]).status_code == 200
    # 持 rev1 提交 a + 新键 d：a 已变 → 冲突；整组不合并
    resp = changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "mine@mail.example.com"))),
        upsert(route_rule("d@corp.example.com", ("mailbox", "w@mail.example.com"))),
    ])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 2
    assert detail["conflict_keys"] == [{"match_type": "exact", "match_key": "a@corp.example.com"}]
    draft = client.get("/draft").json()
    assert draft["revision"] == 2  # 草稿不变，d 未写入


def test_stale_revision_deleted_key_conflicts(client):
    # rev1 有 a；rev2 他人删了 a；持 rev1 再删 a → 冲突而非 rule_not_found
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200
    assert changes(client, 1, [delete_key("exact", "a@corp.example.com")]).status_code == 200
    resp = changes(client, 1, [
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
        delete_key("exact", "a@corp.example.com"),
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflict_keys"] == [
        {"match_type": "exact", "match_key": "a@corp.example.com"}
    ]
    # b 也未写入
    assert keys_of(client.get("/draft").json()["rules"]) == set()


def test_stale_revision_delete_then_recreate_same_content_conflicts_when_different(client):
    # rev1 有 a；rev2 他人删除 a；客户端在 rev1 基础上 upsert a 为“不同内容” → 冲突
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200
    assert changes(client, 1, [delete_key("exact", "a@corp.example.com")]).status_code == 200
    resp = changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflict_keys"][0]["match_key"] == "a@corp.example.com"


def test_future_revision_rejected(client):
    resp = changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ])
    assert resp.status_code == 200
    # 当前 rev1，请求声称 rev5 且无该快照 → 明确拒绝合并
    resp = changes(client, 5, [
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "revision_snapshot_unavailable"
    assert client.get("/draft").json()["revision"] == 1


def test_snapshot_unavailable_refuses_merge(monkeypatch, client):
    """旧库未留存所见修订快照：即使落后也不得按当前状态盲合并。"""
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200  # rev1
    # 人为删掉 rev0 快照（模拟旧数据）：客户端持 rev0 而当前已 rev1
    conn = db.connect()
    conn.execute("DELETE FROM draft_revisions WHERE revision = 0")
    conn.close()
    resp = changes(client, 0, [
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_snapshot_unavailable"
    assert detail["revision"] == 0
    assert detail["current_revision"] == 1
    assert client.get("/draft").json()["revision"] == 1


# ---------- 与既有流程兼容：合并后的草稿可发布 / 解析 / 预览 ----------

def test_upgraded_legacy_db_gets_no_fabricated_snapshots(client):
    """旧库（已有非零修订但无快照表）升级后不臆造 rev0 快照：持旧修订合并明确拒绝，
    持当前修订仍可正常合并。"""
    # 正常流程到 rev1
    assert changes(client, 0, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com"))),
    ]).status_code == 200
    # 模拟旧库：删掉快照表后重新建表（init_db 不应补 rev0，因为当前修订已是 1）
    conn = db.connect()
    conn.execute("DROP TABLE draft_revisions")
    conn.close()
    db.init_db()

    revisions = client.get("/draft/revisions").json()["revisions"]
    assert revisions == []  # 不臆造任何历史快照
    resp = changes(client, 0, [
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "revision_snapshot_unavailable"
    # 当前修订（所见即当前）不依赖历史快照，仍可合并
    resp = changes(client, 1, [
        upsert(route_rule("b@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2


def test_merged_draft_can_be_published_and_resolved(client):
    assert changes(client, 0, [
        upsert(route_rule("boss@corp.example.com", ("alias", "assistant@corp.example.com"))),
        upsert(route_rule("assistant@corp.example.com", ("mailbox", "alice@mail.example.com"))),
    ]).status_code == 200
    resp = client.post("/publish", json={"revision": 1})
    assert resp.status_code == 200, resp.text
    version = resp.json()["version"]
    resolved = client.post("/resolve", json={"address": "boss@corp.example.com", "version": version})
    assert resolved.status_code == 200
    assert resolved.json()["endpoints"][0]["mailbox"] == "alice@mail.example.com"


def test_replace_then_merge_preview_contract_unchanged(client):
    # PUT /draft 与 POST /draft/changes 混用过的草稿，/preview 契约不变
    assert client.put("/draft", json={"revision": 0, "rules": [
        route_rule("a@corp.example.com", ("mailbox", "x@mail.example.com")),
    ]}).status_code == 200
    v1 = client.post("/publish", json={"revision": 1}).json()["version"]
    assert changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "z@mail.example.com"))),
    ]).status_code == 200
    resp = client.post("/preview", json={"base_version": v1, "revision": 2})
    assert resp.status_code == 200
    changed = {item["address"] for item in resp.json()["changed_addresses"]}
    assert "a@corp.example.com" in changed


# ---------- 并发：原子核对和写入 ----------

def test_concurrent_disjoint_changes_both_merge(client):
    """两个线程同持 rev0、操作互不相交的键：串行化后各自合并，最终两条都在，共两个新修订。"""
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def submit(key):
        barrier.wait()
        with TestClient(app) as local:
            r = changes(local, 0, [
                upsert(route_rule(key, ("mailbox", "m@mail.example.com"))),
            ])
            with lock:
                results.append((r.status_code, r.json()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(submit, ["a@corp.example.com", "b@corp.example.com"]))

    assert sorted(code for code, _ in results) == [200, 200], results
    draft = client.get("/draft").json()
    assert draft["revision"] == 2
    assert keys_of(draft["rules"]) == {
        ("exact", "a@corp.example.com"),
        ("exact", "b@corp.example.com"),
    }


def test_concurrent_overlapping_keys_reports_conflict(client):
    """两个线程同持 rev0、改同一键：一个成功，另一个拿到该键的冲突。"""
    barrier = threading.Barrier(2)

    def submit(mailbox):
        barrier.wait()
        with TestClient(app) as local:
            r = changes(local, 0, [
                upsert(route_rule("a@corp.example.com", ("mailbox", mailbox))),
            ])
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["x@mail.example.com", "y@mail.example.com"]))

    codes = sorted(c for c, _ in results)
    assert codes == [200, 409]
    conflict = next(b["detail"] for c, b in results if c == 409)
    assert conflict["conflict_keys"] == [{"match_type": "exact", "match_key": "a@corp.example.com"}]
    assert conflict["current_revision"] == 1
    # 冲突后可带新修订号重试，把键改成自己的内容
    retry = changes(client, 1, [
        upsert(route_rule("a@corp.example.com", ("mailbox", "y@mail.example.com"))),
    ])
    assert retry.status_code == 200
    assert retry.json()["revision"] == 2
