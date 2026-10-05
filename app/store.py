"""持久层：草稿（带乐观锁修订号、按键合并与修订快照）、不可变发布版本、默认投递策略（基线/候选灰度）、幂等投递判定、投递回执（按终点 + 序号回传实际结果与汇总）、历史判定核对报表与灰度自动熔断。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .routing import Rule, RuleSet, Target


class RevisionConflict(Exception):
    """所见修订号与当前修订号不一致（草稿或默认投递策略）。"""

    def __init__(self, current: int, subject: str = "draft"):
        super().__init__(f"revision conflict: current {subject} revision is {current}")
        self.current = current


class RevisionSnapshotUnavailable(Exception):
    """无法取得客户端所见修订的快照（如旧库未留存该修订）：明确拒绝合并。"""

    def __init__(self, revision: int, current: int):
        super().__init__(f"revision snapshot unavailable: {revision} (current revision is {current})")
        self.revision = revision
        self.current = current


class MergeConflict(RevisionConflict):
    """落后修订合并时，本次操作涉及的键中有相对所见快照已改变的键。"""

    def __init__(self, current: int, conflict_keys: list[dict]):
        super().__init__(current)
        self.conflict_keys = conflict_keys


class DraftRuleNotFound(Exception):
    """删除操作引用了当前草稿中不存在的规则。"""

    def __init__(self, match_type: str, match_key: str):
        super().__init__(f"rule not found in draft: {match_type} {match_key}")
        self.match_type = match_type
        self.match_key = match_key


class PolicyVersionNotFound(Exception):
    """策略引用了不存在的已发布版本。"""

    def __init__(self, version: int):
        super().__init__(f"published version not found: {version}")
        self.version = version


class PolicyNotConfigured(Exception):
    """默认投递策略尚未配置（熔断设置等依赖策略的操作据此明确拒绝）。"""

    def __init__(self):
        super().__init__("no delivery policy configured")


def _row_to_rule(row: sqlite3.Row) -> Rule:
    targets = tuple(Target(kind=t["kind"], value=t["value"]) for t in json.loads(row["targets"]))
    return Rule(
        match_type=row["match_type"],
        match_key=row["match_key"],
        action=row["action"],
        targets=targets,
    )


def _rule_from_doc(doc: dict) -> Rule:
    return Rule(
        match_type=doc["match_type"],
        match_key=doc["match_key"],
        action=doc["action"],
        targets=tuple(Target(kind=t["kind"], value=t["value"]) for t in doc.get("targets", [])),
    )


def rule_to_dict(rule: Rule) -> dict:
    return {
        "match_type": rule.match_type,
        "match_key": rule.match_key,
        "action": rule.action,
        "targets": [{"kind": t.kind, "value": t.value} for t in rule.targets],
    }


def _save_draft_snapshot(conn: sqlite3.Connection, revision: int, rules: list[Rule]) -> None:
    """把某次草稿变更的完整规则快照写入 draft_revisions。须在事务内调用。"""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at) VALUES (?, ?, ?, ?)",
        (revision, json.dumps([rule_to_dict(r) for r in rules]), len(rules), created_at),
    )


def get_draft(conn: sqlite3.Connection) -> tuple[int, list[Rule]]:
    revision = conn.execute("SELECT revision FROM draft_meta WHERE id = 1").fetchone()["revision"]
    rows = conn.execute(
        "SELECT match_type, match_key, action, targets FROM draft_rules ORDER BY match_type, match_key"
    ).fetchall()
    return revision, [_row_to_rule(r) for r in rows]


def replace_draft(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """整体替换草稿；修订号不匹配抛 RevisionConflict。须在事务内调用。

    成功后留存完整修订快照（与按键合并一样可核对）。
    """
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    conn.execute("DELETE FROM draft_rules")
    conn.executemany(
        "INSERT INTO draft_rules (match_type, match_key, action, targets) VALUES (?, ?, ?, ?)",
        [
            (
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    _save_draft_snapshot(conn, new_revision, rules)
    return new_revision


def _rules_by_key(rules: list[Rule]) -> dict[tuple[str, str], Rule]:
    return {(r.match_type, r.match_key): r for r in rules}


def get_draft_at(conn: sqlite3.Connection, revision: int) -> tuple[int, list[Rule]] | None:
    """读取某个历史草稿修订的完整快照；不存在返回 None。"""
    row = conn.execute(
        "SELECT revision, rules FROM draft_revisions WHERE revision = ?", (revision,)
    ).fetchone()
    if row is None:
        return None
    return row["revision"], [_rule_from_doc(doc) for doc in json.loads(row["rules"])]


def list_draft_revisions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT revision, rule_count, created_at FROM draft_revisions ORDER BY revision"
    ).fetchall()
    return [dict(r) for r in rows]


def merge_draft(
    conn: sqlite3.Connection,
    expected_revision: int,
    changes: list[tuple[str, str, str, Rule | None]],
) -> tuple[int, list[Rule]]:
    """把一组按键设置 / 删除操作合并进当前草稿。须在写事务内调用。

    - 取得到所见修订（expected_revision）的快照后逐键核对：仅当本次涉及的每个键
      相对该快照在当前草稿中均未改变（存在性与完整内容一致）时才合并；
      否则抛 MergeConflict（附全部冲突键与当前修订号），草稿不变。
    - 所见修订即当前修订时，键必然“未改变”，直接合并。
    - 无法取得所见修订快照（RevisionSnapshotUnavailable）时明确拒绝合并。
    - 删除当前草稿中不存在的规则抛 DraftRuleNotFound（整组拒绝，草稿不变）。
    成功则写入合并结果、修订号 +1，并留存完整修订快照。
    """
    revision, current_rules = get_draft(conn)
    current_by_key = _rules_by_key(current_rules)

    if revision != expected_revision:
        seen = get_draft_at(conn, expected_revision)
        if seen is None:
            raise RevisionSnapshotUnavailable(expected_revision, revision)
        seen_by_key = _rules_by_key(seen[1])
        conflict_keys: list[dict] = []
        for _op, match_type, key, _rule in changes:
            ident = (match_type, key)
            seen_rule = seen_by_key.get(ident)
            current_rule = current_by_key.get(ident)
            if seen_rule != current_rule:  # 含“快照存在 / 当前不存在”与反向新增
                conflict_keys.append({"match_type": match_type, "match_key": key})
        if conflict_keys:
            raise MergeConflict(revision, conflict_keys)

    # 键相对所见快照均未改变（或所见即当前）：删除前先确认规则当前存在
    for _op, match_type, key, _rule in changes:
        if _op == "delete" and (match_type, key) not in current_by_key:
            raise DraftRuleNotFound(match_type, key)

    merged = dict(current_by_key)
    for op, match_type, key, rule in changes:
        ident = (match_type, key)
        if op == "delete":
            del merged[ident]
        else:
            assert rule is not None
            merged[ident] = rule
    new_rules = sorted(merged.values(), key=lambda r: (r.match_type, r.match_key))

    conn.execute("DELETE FROM draft_rules")
    conn.executemany(
        "INSERT INTO draft_rules (match_type, match_key, action, targets) VALUES (?, ?, ?, ?)",
        [
            (
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in new_rules
        ],
    )
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    _save_draft_snapshot(conn, new_revision, new_rules)
    return new_revision, new_rules


def publish(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """把当前草稿固化为不可变版本，返回版本号。须在事务内调用。"""
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO versions (source_revision, created_at) VALUES (?, ?)",
        (revision, created_at),
    )
    version = cur.lastrowid
    conn.executemany(
        "INSERT INTO version_rules (version, match_type, match_key, action, targets) VALUES (?, ?, ?, ?, ?)",
        [
            (
                version,
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    return version


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.version, v.source_revision, v.created_at, COUNT(r.match_key) AS rule_count
        FROM versions v LEFT JOIN version_rules r ON r.version = v.version
        GROUP BY v.version ORDER BY v.version
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version: int) -> dict | None:
    row = conn.execute(
        "SELECT version, source_revision, created_at FROM versions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        return None
    rules = conn.execute(
        "SELECT match_type, match_key, action, targets FROM version_rules "
        "WHERE version = ? ORDER BY match_type, match_key",
        (version,),
    ).fetchall()
    return {**dict(row), "rules": [_row_to_rule(r) for r in rules]}


def load_ruleset(conn: sqlite3.Connection, version: int) -> RuleSet | None:
    found = get_version(conn, version)
    if found is None:
        return None
    return RuleSet(found["rules"])


# ---------- 默认投递策略（基线 / 候选灰度） ----------

def get_policy(conn: sqlite3.Connection) -> dict | None:
    """读取当前默认投递策略；从未配置时返回 None（修订号视为 0）。"""
    row = conn.execute(
        "SELECT revision, baseline_version, candidate_version, candidate_percent, updated_at "
        "FROM delivery_policy WHERE id = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def replace_policy(
    conn: sqlite3.Connection,
    expected_revision: int,
    baseline_version: int,
    candidate_version: int,
    candidate_percent: int,
) -> dict:
    """在同一事务内核验所见策略修订号并保存新策略。

    - 未配置策略时修订号为 0，行尚不存在；
    - 修订号不匹配抛 RevisionConflict，调用方回滚，原策略保持不变；
    - 基线 / 候选必须都是已发布版本，否则抛 PolicyVersionNotFound（同样不改原策略）。
    """
    current = get_policy(conn)
    current_revision = current["revision"] if current is not None else 0
    if current_revision != expected_revision:
        raise RevisionConflict(current_revision, subject="policy")
    for version in (baseline_version, candidate_version):
        if conn.execute("SELECT 1 FROM versions WHERE version = ?", (version,)).fetchone() is None:
            raise PolicyVersionNotFound(version)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_policy (id, revision, baseline_version, candidate_version, candidate_percent, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            baseline_version = excluded.baseline_version,
            candidate_version = excluded.candidate_version,
            candidate_percent = excluded.candidate_percent,
            updated_at = excluded.updated_at
        """,
        (current_revision + 1, baseline_version, candidate_version, candidate_percent, updated_at),
    )
    return {
        "revision": current_revision + 1,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "candidate_percent": candidate_percent,
        "updated_at": updated_at,
    }


# ---------- 灰度自动熔断（按候选侧失败率熔断，设置绑定策略修订号） ----------
#
# 设置针对设置时所见的策略修订号；策略修订号一旦变化（人工 PUT /policy 或自动熔断
# 都会令其 +1），旧设置即失效——评估时只认“设置的政策修订号 == 当前策略修订号”。
# 评估发生在首次投递判定持久化之后、同一 BEGIN IMMEDIATE 事务内：只统计同一策略
# 修订号、窗口内已持久化的候选侧判定；样本达下限且失败占比达阈值时触发一次——
# 候选比例置 0、策略修订号 +1 并写入一条熔断记录。修订号随之变化使旧设置失效，
# 因此同一设置天然只触发一次；并发提交与人工更新由 BEGIN IMMEDIATE 串行化，
# 后到者只能看到前者已提交的结果（修订号已变），不会互相覆盖。

def get_circuit_breaker_settings(conn: sqlite3.Connection) -> dict | None:
    """读取最近保存的熔断设置（可能已随策略修订号变化而失效）；从未设置返回 None。"""
    row = conn.execute(
        "SELECT policy_revision, failure_threshold_percent, min_candidate_samples, "
        "window_minutes, updated_at FROM circuit_breaker_settings WHERE id = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def get_active_circuit_breaker_settings(
    conn: sqlite3.Connection, policy_revision: int
) -> dict | None:
    """读取对指定策略修订号仍然有效的熔断设置；未设置或已失效返回 None。"""
    settings = get_circuit_breaker_settings(conn)
    if settings is None or settings["policy_revision"] != policy_revision:
        return None
    return settings


def save_circuit_breaker_settings(
    conn: sqlite3.Connection,
    expected_revision: int,
    failure_threshold_percent: int,
    min_candidate_samples: int,
    window_minutes: int,
) -> dict:
    """凭所见策略修订号保存熔断设置（针对当前策略修订号生效）。须在事务内调用。

    - 未配置策略抛 PolicyNotConfigured；所见修订号与当前策略修订号不一致抛
      RevisionConflict——两者都不落任何设置，也不会产生熔断；
    - 成功则整体替换旧设置（针对新修订号须重新设置，旧设置随之失效）。
    """
    policy = get_policy(conn)
    if policy is None:
        raise PolicyNotConfigured()
    if policy["revision"] != expected_revision:
        raise RevisionConflict(policy["revision"], subject="policy")
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO circuit_breaker_settings (
            id, policy_revision, failure_threshold_percent,
            min_candidate_samples, window_minutes, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            policy_revision = excluded.policy_revision,
            failure_threshold_percent = excluded.failure_threshold_percent,
            min_candidate_samples = excluded.min_candidate_samples,
            window_minutes = excluded.window_minutes,
            updated_at = excluded.updated_at
        """,
        (
            policy["revision"],
            failure_threshold_percent,
            min_candidate_samples,
            window_minutes,
            updated_at,
        ),
    )
    return {
        "policy_revision": policy["revision"],
        "failure_threshold_percent": failure_threshold_percent,
        "min_candidate_samples": min_candidate_samples,
        "window_minutes": window_minutes,
        "updated_at": updated_at,
    }


def list_circuit_breaks(conn: sqlite3.Connection) -> list[dict]:
    """按触发顺序列出全部熔断记录（只增不改）。"""
    rows = conn.execute(
        "SELECT id, policy_revision, failure_threshold_percent, min_candidate_samples, "
        "window_minutes, sample_count, failure_count, triggered_at "
        "FROM circuit_breaks ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def maybe_trigger_circuit_breaker(conn: sqlite3.Connection, policy: dict) -> dict | None:
    """首次投递判定持久化后评估候选侧失败率，达到条件则熔断。须在写事务内调用。

    policy 为本事务内受理判定时的当前策略（BEGIN IMMEDIATE 保证事务期间不变）。
    仅统计同一策略修订号、窗口内已持久化的候选侧判定（含本次刚写入的判定；
    同编号重放不会走到这里，自然不重复计数）。样本达下限且失败数占比达到阈值时：
    候选比例置 0、策略修订号 +1，并记录旧修订号、样本数、失败数与触发时间。
    设置不存在或已失效（修订号不匹配）时不评估；返回熔断记录或 None。
    """
    settings = get_active_circuit_breaker_settings(conn, policy["revision"])
    if settings is None:
        return None
    now = datetime.now(timezone.utc)
    # created_at 一律为 UTC ISO 秒精度字符串，可直接按字符串比较窗口下界
    cutoff = (now - timedelta(minutes=settings["window_minutes"])).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT COUNT(*) AS samples,
               COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failures
        FROM delivery_decisions
        WHERE policy_revision = ? AND selection = 'candidate' AND created_at >= ?
        """,
        (policy["revision"], cutoff),
    ).fetchone()
    samples, failures = row["samples"], row["failures"]
    if samples < settings["min_candidate_samples"]:
        return None
    if failures * 100 < settings["failure_threshold_percent"] * samples:  # 整数比较：失败占比未达阈值
        return None
    triggered_at = now.isoformat(timespec="seconds")
    conn.execute(
        "UPDATE delivery_policy SET candidate_percent = 0, revision = revision + 1, "
        "updated_at = ? WHERE id = 1",
        (triggered_at,),
    )
    cur = conn.execute(
        """
        INSERT INTO circuit_breaks (
            policy_revision, failure_threshold_percent, min_candidate_samples,
            window_minutes, sample_count, failure_count, triggered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            policy["revision"],
            settings["failure_threshold_percent"],
            settings["min_candidate_samples"],
            settings["window_minutes"],
            samples,
            failures,
            triggered_at,
        ),
    )
    return {
        "id": cur.lastrowid,
        "policy_revision": policy["revision"],
        "failure_threshold_percent": settings["failure_threshold_percent"],
        "min_candidate_samples": settings["min_candidate_samples"],
        "window_minutes": settings["window_minutes"],
        "sample_count": samples,
        "failure_count": failures,
        "triggered_at": triggered_at,
    }


# ---------- 幂等投递判定（投递编号 → 首次判定，永久可查、原样重放） ----------

_DECISION_COLUMNS = (
    "delivery_id, address, policy_revision, baseline_version, candidate_version, "
    "candidate_percent, selection, version, status, endpoints, error, created_at"
)


def _row_to_decision(row: sqlite3.Row) -> dict:
    decision = dict(row)
    decision["endpoints"] = json.loads(decision["endpoints"]) if decision["endpoints"] is not None else None
    decision["error"] = json.loads(decision["error"]) if decision["error"] is not None else None
    return decision


def get_decision(conn: sqlite3.Connection, delivery_id: str) -> dict | None:
    """按投递编号读取已持久化的判定；不存在返回 None。JSON 字段在此还原。"""
    row = conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM delivery_decisions WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    return _row_to_decision(row) if row is not None else None


def get_decisions(conn: sqlite3.Connection, delivery_ids: list[str]) -> dict[str, dict]:
    """按投递编号批量读取已持久化的判定，返回 {delivery_id: 判定}（不含未知编号）。"""
    if not delivery_ids:
        return {}
    placeholders = ", ".join("?" for _ in delivery_ids)
    rows = conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM delivery_decisions WHERE delivery_id IN ({placeholders})",
        list(delivery_ids),
    ).fetchall()
    return {row["delivery_id"]: _row_to_decision(row) for row in rows}


def save_decision(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    address: str,
    policy: dict,
    selection: str,
    version: int,
    status: str,
    endpoints: list[dict] | None,
    error: dict | None,
) -> dict:
    """把首次判定整体持久化（成功含全部终点路径，失败含原错误详情）。须在事务内调用。

    判定一旦写入不再改写：策略后续变更不影响已持久化的结果。
    """
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_decisions (
            delivery_id, address, policy_revision, baseline_version, candidate_version,
            candidate_percent, selection, version, status, endpoints, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            address,
            policy["revision"],
            policy["baseline_version"],
            policy["candidate_version"],
            policy["candidate_percent"],
            selection,
            version,
            status,
            json.dumps(endpoints) if endpoints is not None else None,
            json.dumps(error) if error is not None else None,
            created_at,
        ),
    )
    return get_decision(conn, delivery_id)


# ---------- 历史投递判定核对报表（时间窗 + 快照边界 + 分组计数 + 键集分页） ----------
#
# 报表在 delivery_decisions 的 rowid（随提交单调递增的内部序号）上固定边界：
# 首次查询记下当时的最大序号，分组、总数与全部翻页都只看序号不超过该边界的判定，
# 因此并发新投递不会进入旧报表；按 rowid 键集分页（唯一、单调）保证页间不漏不重。
# delivery_id 是主键，同一编号天然只计一次；已持久化的失败判定同样计入。

def _report_where(
    start: str,
    end: str,
    policy_revision: int | None,
    snapshot_seq: int,
    after_seq: int = 0,
) -> tuple[str, list]:
    """报表共用的筛选：同一快照边界 + 左闭右开时间窗（+ 可选策略修订号 + 分页下界）。"""
    clause = "rowid <= ? AND rowid > ? AND created_at >= ? AND created_at < ?"
    params: list = [snapshot_seq, after_seq, start, end]
    if policy_revision is not None:
        clause += " AND policy_revision = ?"
        params.append(policy_revision)
    return clause, params


def report_snapshot_seq(conn: sqlite3.Connection) -> int:
    """截至当前已提交判定的内部序号上界；报表以此固定边界，之后的新投递不进入该报表。"""
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS seq FROM delivery_decisions").fetchone()
    return row["seq"]


def report_groups(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    policy_revision: int | None,
    snapshot_seq: int,
) -> list[dict]:
    """按选中版本、分流侧（基线/候选）、结果（ok 或失败原因）分组计数。"""
    clause, params = _report_where(start, end, policy_revision, snapshot_seq)
    rows = conn.execute(
        f"""
        SELECT version, selection,
               CASE WHEN status = 'ok' THEN 'ok' ELSE json_extract(error, '$.error') END AS outcome,
               COUNT(*) AS count
        FROM delivery_decisions
        WHERE {clause}
        GROUP BY version, selection, outcome
        ORDER BY version, selection, outcome
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def report_total(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    policy_revision: int | None,
    snapshot_seq: int,
) -> int:
    """与分组同一筛选下的判定总数（恒等于各组计数之和）。"""
    clause, params = _report_where(start, end, policy_revision, snapshot_seq)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM delivery_decisions WHERE {clause}", params
    ).fetchone()
    return row["n"]


def report_items(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    policy_revision: int | None,
    snapshot_seq: int,
    after_seq: int,
    limit: int,
) -> list[dict]:
    """取一页判定明细（按内部序号升序，从 after_seq 之后继续，最多 limit 条）。"""
    clause, params = _report_where(start, end, policy_revision, snapshot_seq, after_seq)
    rows = conn.execute(
        f"""
        SELECT rowid AS seq, delivery_id, address, policy_revision, version, selection, status,
               CASE WHEN status = 'ok' THEN NULL ELSE json_extract(error, '$.error') END AS error_type,
               created_at
        FROM delivery_decisions
        WHERE {clause}
        ORDER BY rowid
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- 投递回执（成功判定后，投递系统按终点 + 序号回传实际投递结果） ----------
#
# 回执依附于已持久化的“成功”判定：未知编号、原判定失败、终点不在判定终点列表中的
# 请求一律拒绝且不产生记录。每个 (投递编号, 终点) 的尝试序号严格递增；同一序号的
# 结果只允许写入一次——同序号同结果重试原样返回，同序号不同结果报冲突；
# 已送达 / 永久失败是该终点的终结结果，之后不再接收新尝试。
# 回执与判定、策略、熔断统计完全解耦：只增不改 delivery_attempts，不触碰
# delivery_decisions / delivery_policy / circuit_*，因此判定查询、同编号重放与
# 灰度熔断统计都不受回执影响。全部核对 + 写入在单个 BEGIN IMMEDIATE 事务内完成，
# 并发回传按写锁串行化，序号与汇总状态保持一致。

_ATTEMPT_COLUMNS = "mailbox, attempt_no, result, created_at"
TERMINAL_RESULTS = ("delivered", "permanent_failure")


class ReceiptDecisionFailed(Exception):
    """原判定为路由失败（rejected / no_route / cycle）：无终点，不接收任何回执。"""

    def __init__(self, status: str, error_type: str | None):
        super().__init__(f"delivery decision is not successful (status={status})")
        self.status = status
        self.error_type = error_type


class ReceiptEndpointNotAllowed(Exception):
    """回传终点邮箱不在成功判定列出的终点集合中。"""

    def __init__(self, mailbox: str, endpoints: list[str]):
        super().__init__(f"mailbox {mailbox!r} is not an endpoint of this delivery decision")
        self.mailbox = mailbox
        self.endpoints = endpoints


class ReceiptResultConflict(Exception):
    """同一终点的同一尝试序号已记录不同结果。"""

    def __init__(self, stored_result: str, submitted_result: str, attempt_no: int):
        super().__init__(
            f"attempt {attempt_no} already recorded as {stored_result!r}, cannot change to {submitted_result!r}"
        )
        self.stored_result = stored_result
        self.submitted_result = submitted_result
        self.attempt_no = attempt_no


class ReceiptSequenceConflict(Exception):
    """新尝试序号不大于该终点已记录的最大序号。"""

    def __init__(self, last_attempt_no: int, attempt_no: int):
        super().__init__(
            f"attempt number {attempt_no} must be greater than the last recorded number {last_attempt_no}"
        )
        self.last_attempt_no = last_attempt_no
        self.attempt_no = attempt_no


class ReceiptEndpointClosed(Exception):
    """该终点已有终结结果（已送达 / 永久失败），不得再接收新尝试。"""

    def __init__(self, terminal_result: str, last_attempt_no: int, attempt_no: int):
        super().__init__(
            f"endpoint already has a terminal result {terminal_result!r} at attempt {last_attempt_no}"
        )
        self.terminal_result = terminal_result
        self.last_attempt_no = last_attempt_no
        self.attempt_no = attempt_no


def decision_endpoints(decision: dict) -> list[str]:
    """成功判定中按持久化顺序列出的终点邮箱；失败判定无终点。"""
    if decision["status"] != "ok" or not decision["endpoints"]:
        return []
    return [endpoint["mailbox"] for endpoint in decision["endpoints"]]


def _row_to_attempt(row: sqlite3.Row) -> dict:
    return {"attempt_no": row["attempt_no"], "result": row["result"], "created_at": row["created_at"]}


def get_attempt(conn: sqlite3.Connection, delivery_id: str, mailbox: str, attempt_no: int) -> dict | None:
    """读取某投递编号下某终点某序号的尝试记录；不存在返回 None。"""
    row = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM delivery_attempts "
        "WHERE delivery_id = ? AND mailbox = ? AND attempt_no = ?",
        (delivery_id, mailbox, attempt_no),
    ).fetchone()
    return _row_to_attempt(row) if row is not None else None


def get_last_attempt(conn: sqlite3.Connection, delivery_id: str, mailbox: str) -> dict | None:
    """读取某终点已记录的最大序号尝试；无记录返回 None。"""
    row = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM delivery_attempts "
        "WHERE delivery_id = ? AND mailbox = ? ORDER BY attempt_no DESC LIMIT 1",
        (delivery_id, mailbox),
    ).fetchone()
    return _row_to_attempt(row) if row is not None else None


def list_attempts(conn: sqlite3.Connection, delivery_id: str) -> list[dict]:
    """按终点邮箱、尝试序号升序列出某投递编号的全部尝试记录。"""
    rows = conn.execute(
        f"SELECT mailbox, attempt_no, result, created_at FROM delivery_attempts "
        "WHERE delivery_id = ? ORDER BY mailbox, attempt_no",
        (delivery_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_attempt(
    conn: sqlite3.Connection, delivery_id: str, mailbox: str, attempt_no: int, result: str
) -> dict:
    """回传一次实际投递结果并持久化（须在 BEGIN IMMEDIATE 写事务内调用）。

    返回该（投递编号, 终点, 序号）的尝试记录——新写入与同序号同结果重放返回同一份内容。
    拒绝顺序：未知编号由调用方先查（返回 None）；原判定失败 → ReceiptDecisionFailed；
    终点不在判定中 → ReceiptEndpointNotAllowed；同序号不同结果 → ReceiptResultConflict；
    已终结后再收新序号 → ReceiptEndpointClosed；序号不大于已记录最大序号 →
    ReceiptSequenceConflict。以上拒绝均不写入任何记录。
    """
    decision = get_decision(conn, delivery_id)
    if decision is None:
        raise KeyError(delivery_id)
    if decision["status"] != "ok":
        raise ReceiptDecisionFailed(
            decision["status"],
            decision["error"].get("error") if decision.get("error") else None,
        )
    endpoints = decision_endpoints(decision)
    if mailbox not in endpoints:
        raise ReceiptEndpointNotAllowed(mailbox, endpoints)

    existing = get_attempt(conn, delivery_id, mailbox, attempt_no)
    if existing is not None:  # 同序号：结果一致原样返回，不一致报冲突（终结状态也不例外）
        if existing["result"] != result:
            raise ReceiptResultConflict(existing["result"], result, attempt_no)
        return {"delivery_id": delivery_id, "mailbox": mailbox, **existing}

    last = get_last_attempt(conn, delivery_id, mailbox)
    if last is not None:
        if last["result"] in TERMINAL_RESULTS:  # 已送达 / 永久失败后不得再接收新尝试
            raise ReceiptEndpointClosed(last["result"], last["attempt_no"], attempt_no)
        if attempt_no <= last["attempt_no"]:  # 新序号必须严格大于已记录序号
            raise ReceiptSequenceConflict(last["attempt_no"], attempt_no)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_attempts (delivery_id, mailbox, attempt_no, result, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (delivery_id, mailbox, attempt_no, result, created_at),
    )
    stored = get_attempt(conn, delivery_id, mailbox, attempt_no)
    return {"delivery_id": delivery_id, "mailbox": mailbox, **stored}


def receipt_summary(conn: sqlite3.Connection, delivery_id: str) -> dict | None:
    """汇总某投递编号各终点的尝试记录与整体状态。须在事务（只读 / 写）内调用。

    未知编号返回 None；原判定失败抛 ReceiptDecisionFailed。
    终点状态：无记录为 pending；否则取最大序号记录的结果（终结结果必为最后一条）。
    汇总：全部终点已送达 → delivered；任一终点永久失败 → failed；其余（含无回执）→ pending。
    """
    decision = get_decision(conn, delivery_id)
    if decision is None:
        return None
    if decision["status"] != "ok":
        raise ReceiptDecisionFailed(
            decision["status"],
            decision["error"].get("error") if decision.get("error") else None,
        )
    by_mailbox: dict[str, list[dict]] = {}
    for row in list_attempts(conn, delivery_id):
        mailbox = row.pop("mailbox")
        by_mailbox.setdefault(mailbox, []).append(
            {"attempt_no": row["attempt_no"], "result": row["result"], "created_at": row["created_at"]}
        )

    endpoints_out: list[dict] = []
    for mailbox in decision_endpoints(decision):
        attempts = by_mailbox.get(mailbox, [])  # list_attempts 已按序号升序
        status = attempts[-1]["result"] if attempts else "pending"
        endpoints_out.append({"mailbox": mailbox, "status": status, "attempts": attempts})

    if endpoints_out and all(ep["status"] == "delivered" for ep in endpoints_out):
        overall = "delivered"
    elif any(ep["status"] == "permanent_failure" for ep in endpoints_out):
        overall = "failed"
    else:
        overall = "pending"
    return {"delivery_id": delivery_id, "status": overall, "endpoints": endpoints_out}
