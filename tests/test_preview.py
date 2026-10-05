"""发布前预览（POST /preview）：草稿 vs 基线版本的投递差异。"""
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


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def setup_published(client, rules=BASE_RULES):
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def preview(client, base_version, revision):
    return client.post("/preview", json={"base_version": base_version, "revision": revision})


def changed_addresses(body):
    return {item["address"]: item for item in body["changed_addresses"]}


# ---------- 基本差异：终点 / 失败结果变化才列出，附两侧完整结果 ----------

def test_preview_lists_changed_entries_with_both_full_results(client):
    version = setup_published(client)
    # 草稿：team 改投 carol（下游变化应追溯到 boss / assistant 入口）；删除 blocked 拒收规则
    draft_rules = [
        {**rule, "targets": [dict(t) for t in rule["targets"]]} for rule in BASE_RULES
    ]
    for rule in draft_rules:
        if rule["match_key"] == "team@corp.example.com":
            rule["targets"] = [{"kind": "mailbox", "value": "carol@mail.example.com"}]
    draft_rules = [r for r in draft_rules if r["match_key"] != "blocked@corp.example.com"]
    assert put_rules(client, 1, draft_rules).status_code == 200

    resp = preview(client, version, 2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_version"] == version
    assert body["revision"] == 2

    changed = changed_addresses(body)
    # 别名下游变化追溯到受影响入口：boss / assistant / team 自身
    assert set(changed) == {
        "boss@corp.example.com",
        "assistant@corp.example.com",
        "team@corp.example.com",
        "blocked@corp.example.com",
    }

    team = changed["team@corp.example.com"]
    assert {e["mailbox"] for e in team["base"]["endpoints"]} == {
        "alice@mail.example.com",
        "bob@mail.example.com",
    }
    assert {e["mailbox"] for e in team["draft"]["endpoints"]} == {"carol@mail.example.com"}

    # boss 的完整两侧结果：base 三个终点，draft 两个终点且路径完整
    boss = changed["boss@corp.example.com"]
    assert {e["mailbox"] for e in boss["base"]["endpoints"]} == {
        "boss-backup@mail.example.com",
        "alice@mail.example.com",
        "bob@mail.example.com",
    }
    draft_endpoints = {e["mailbox"]: e["paths"] for e in boss["draft"]["endpoints"]}
    assert draft_endpoints["carol@mail.example.com"] == [[
        "boss@corp.example.com",
        "assistant@corp.example.com",
        "team@corp.example.com",
        "carol@mail.example.com",
    ]]

    # blocked：base 拒收失败 → draft 落到域兜底
    blocked = changed["blocked@corp.example.com"]
    assert blocked["base"]["status"] == "failed"
    assert blocked["base"]["error"]["error"] == "rejected"
    assert blocked["draft"]["status"] == "ok"
    assert {e["mailbox"] for e in blocked["draft"]["endpoints"]} == {"catchall@mail.example.com"}

    # 域兜底本身没变 → 不列出
    assert body["changed_domain_fallbacks"] == []


def test_preview_no_changes_returns_empty(client):
    version = setup_published(client)
    resp = preview(client, version, 1)
    assert resp.status_code == 200
    assert resp.json()["changed_addresses"] == []
    assert resp.json()["changed_domain_fallbacks"] == []


def test_preview_ignores_path_and_rule_ordering(client):
    version = setup_published(client)
    # 同一套规则，仅调换规则顺序与各规则内目标顺序 → 不构成差异
    reordered = []
    for rule in reversed(BASE_RULES):
        reordered.append({**rule, "targets": list(reversed(rule["targets"]))})
    assert put_rules(client, 1, reordered).status_code == 200
    resp = preview(client, version, 2)
    assert resp.status_code == 200
    assert resp.json()["changed_addresses"] == []
    assert resp.json()["changed_domain_fallbacks"] == []


# ---------- 域兜底类别：示例地址避开两侧精确地址与别名目标 ----------

def test_preview_domain_fallback_change_uses_sample_address(client):
    version = setup_published(client)
    draft_rules = [
        {**rule, "targets": [dict(t) for t in rule["targets"]]} for rule in BASE_RULES
    ]
    for rule in draft_rules:
        if rule["match_type"] == "domain":
            rule["targets"] = [{"kind": "mailbox", "value": "new-catchall@mail.example.com"}]
    assert put_rules(client, 1, draft_rules).status_code == 200

    body = preview(client, version, 2).json()
    assert body["changed_addresses"] == []  # 精确入口不受影响
    fallbacks = body["changed_domain_fallbacks"]
    assert len(fallbacks) == 1
    item = fallbacks[0]
    assert item["domain"] == "corp.example.com"
    sample = item["sample"]
    assert sample.endswith("@corp.example.com")
    # 示例地址避开两侧全部精确地址与别名目标
    named = {"boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "blocked@corp.example.com"}
    assert sample not in named
    assert {e["mailbox"] for e in item["base"]["endpoints"]} == {"catchall@mail.example.com"}
    assert {e["mailbox"] for e in item["draft"]["endpoints"]} == {"new-catchall@mail.example.com"}
    assert item["base"]["endpoints"][0]["paths"][0][0] == sample


def test_preview_sample_address_avoids_collisions(client):
    rules = BASE_RULES + [
        {
            "match_type": "exact",
            "match_key": "_preview-sample@corp.example.com",  # 占掉首选示例名
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "occupy@mail.example.com"}],
        }
    ]
    version = setup_published(client, rules)
    draft_rules = [
        {**rule, "targets": [dict(t) for t in rule["targets"]]} for rule in rules
    ]
    for rule in draft_rules:
        if rule["match_type"] == "domain":
            rule["targets"] = [{"kind": "mailbox", "value": "new-catchall@mail.example.com"}]
    assert put_rules(client, 1, draft_rules).status_code == 200

    body = preview(client, version, 2).json()
    item = body["changed_domain_fallbacks"][0]
    assert item["sample"] == "_preview-sample-1@corp.example.com"


def test_preview_domain_rule_added_and_removed(client):
    version = setup_published(client)
    # 草稿：删除 corp.example.com 域兜底，新增 other.example.com 域兜底
    draft_rules = [
        {**rule, "targets": [dict(t) for t in rule["targets"]]} for rule in BASE_RULES
    ]
    draft_rules = [r for r in draft_rules if r["match_type"] != "domain"]
    draft_rules.append(
        {
            "match_type": "domain",
            "match_key": "other.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "other-catchall@mail.example.com"}],
        }
    )
    assert put_rules(client, 1, draft_rules).status_code == 200

    body = preview(client, version, 2).json()
    fallbacks = {item["domain"]: item for item in body["changed_domain_fallbacks"]}
    assert set(fallbacks) == {"corp.example.com", "other.example.com"}
    # 删除：base 正常 → draft 无路由
    removed = fallbacks["corp.example.com"]
    assert removed["base"]["status"] == "ok"
    assert removed["draft"]["status"] == "failed"
    assert removed["draft"]["error"]["error"] == "no_route"
    # 新增：base 无路由 → draft 正常
    added = fallbacks["other.example.com"]
    assert added["base"]["status"] == "failed"
    assert added["base"]["error"]["error"] == "no_route"
    assert added["draft"]["status"] == "ok"


# ---------- 草稿未通过发布校验：预览仍展示失败结果，且不改动数据 ----------

def test_preview_shows_failures_for_unpublishable_draft(client):
    version = setup_published(client)
    # 草稿引入成环：loop-a <-> loop-b（该草稿无法通过发布校验）
    draft_rules = [
        {**rule, "targets": [dict(t) for t in rule["targets"]]} for rule in BASE_RULES
    ]
    draft_rules += [
        {
            "match_type": "exact",
            "match_key": "loop-a@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "loop-b@corp.example.com"}],
        },
        {
            "match_type": "exact",
            "match_key": "loop-b@corp.example.com",
            "action": "route",
            "targets": [{"kind": "alias", "value": "loop-a@corp.example.com"}],
        },
    ]
    assert put_rules(client, 1, draft_rules).status_code == 200
    assert publish(client, 2).status_code == 422  # 确认草稿确实无法发布

    resp = preview(client, version, 2)
    assert resp.status_code == 200
    changed = changed_addresses(resp.json())
    for addr in ("loop-a@corp.example.com", "loop-b@corp.example.com"):
        assert changed[addr]["draft"]["status"] == "failed"
        assert changed[addr]["draft"]["error"]["error"] == "cycle"
        assert changed[addr]["base"]["status"] == "ok"  # base 中落到域兜底

    # 预览是只读的：草稿与历史版本均未改动
    assert client.get("/draft").json()["revision"] == 2
    assert [v["version"] for v in client.get("/versions").json()["versions"]] == [version]


# ---------- 错误契约：修订号过期 409，基线版本不存在 404 ----------

def test_preview_stale_revision_conflict(client):
    version = setup_published(client)
    resp = preview(client, version, 0)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "revision_conflict"
    assert resp.json()["detail"]["current_revision"] == 1


def test_preview_unknown_base_version_404(client):
    setup_published(client)
    resp = preview(client, 99, 1)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "version_not_found"


# ---------- 既有契约延续：按版本解析不受影响 ----------

def test_resolve_contract_unchanged_after_preview(client):
    version = setup_published(client)
    assert preview(client, version, 1).status_code == 200
    resp = client.post("/resolve", json={"address": "boss@corp.example.com", "version": version})
    assert resp.status_code == 200
    assert {e["mailbox"] for e in resp.json()["endpoints"]} == {
        "boss-backup@mail.example.com",
        "alice@mail.example.com",
        "bob@mail.example.com",
    }
    resp = client.post("/resolve", json={"address": "blocked@corp.example.com", "version": version})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "rejected"
