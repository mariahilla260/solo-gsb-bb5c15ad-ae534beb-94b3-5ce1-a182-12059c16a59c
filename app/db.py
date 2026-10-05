"""SQLite 存储层：草稿（修订号 + 修订快照）、不可变发布版本、默认投递策略、幂等投递判定、投递回执与灰度熔断、投递任务领取租约。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO draft_meta (id, revision) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS draft_rules (
    match_type TEXT NOT NULL CHECK (match_type IN ('exact', 'domain')),
    match_key  TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('reject', 'route')),
    targets    TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"kind": "alias"|"mailbox", "value": "..."}]
    PRIMARY KEY (match_type, match_key)
);

-- 草稿修订快照：每次草稿变更（整体替换 PUT /draft 或按键合并 POST /draft/changes）
-- 都留存一份完整规则快照，供多人合并时核对“所见修订”与事后审计。
-- revision 0（初始空草稿）由 init_db 在全新库上补写；旧库升级不臆造历史快照——
-- 合并时若取不到所见修订的快照则明确拒绝。
CREATE TABLE IF NOT EXISTS draft_revisions (
    revision   INTEGER PRIMARY KEY,              -- 草稿修订号，与 draft_meta.revision 对应
    rules      TEXT NOT NULL,                    -- JSON 完整规则快照（形状与 /draft 的 rules 一致）
    rule_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_revision INTEGER NOT NULL,        -- 发布时核对通过的草稿修订号
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_rules (
    version    INTEGER NOT NULL REFERENCES versions (version),
    match_type TEXT NOT NULL,
    match_key  TEXT NOT NULL,
    action     TEXT NOT NULL,
    targets    TEXT NOT NULL,
    PRIMARY KEY (version, match_type, match_key)
);

-- 默认投递策略：单行（id 恒为 1）。未配置时行不存在，策略修订号视为 0。
-- 仅引用已发布版本；版本不可变、不可删除，外键长期有效。
CREATE TABLE IF NOT EXISTS delivery_policy (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    revision          INTEGER NOT NULL,                 -- 策略修订号，每次成功更新 +1
    baseline_version  INTEGER NOT NULL REFERENCES versions (version),
    candidate_version INTEGER NOT NULL REFERENCES versions (version),
    candidate_percent INTEGER NOT NULL CHECK (candidate_percent BETWEEN 0 AND 100),
    updated_at        TEXT NOT NULL
);

-- 幂等投递判定：每个投递编号一行。首次受理时把完整判定（成功含全部终点路径，
-- 失败含原错误详情）整体写入；之后相同编号 + 相同规范化地址的请求原样重放，
-- 判定不随策略变更而改写。无效地址 / 未配置策略的请求不会写入（不占用编号）。
CREATE TABLE IF NOT EXISTS delivery_decisions (
    delivery_id       TEXT PRIMARY KEY,                 -- 调用方唯一投递编号
    address           TEXT NOT NULL,                    -- 规范化后的收件地址
    policy_revision   INTEGER NOT NULL,                 -- 受理时的策略修订号
    baseline_version  INTEGER NOT NULL,
    candidate_version INTEGER NOT NULL,
    candidate_percent INTEGER NOT NULL,
    selection         TEXT NOT NULL CHECK (selection IN ('baseline', 'candidate')),
    version           INTEGER NOT NULL REFERENCES versions (version),  -- 实际选中的已发布版本
    status            TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    endpoints         TEXT,                             -- JSON: [{"mailbox", "paths"}]；status='ok' 时非空
    error             TEXT,                             -- JSON: 失败详情（rejected/no_route/cycle）；status='failed' 时非空
    created_at        TEXT NOT NULL
);

-- 灰度熔断设置：单行（id 恒为 1），绑定设置时所见的策略修订号。
-- 策略修订号一旦变化（人工 PUT /policy 或自动熔断），旧设置即失效，
-- 须凭新修订号重新设置；未配置策略时不允许设置。
CREATE TABLE IF NOT EXISTS circuit_breaker_settings (
    id                        INTEGER PRIMARY KEY CHECK (id = 1),
    policy_revision           INTEGER NOT NULL,              -- 设置针对的策略修订号
    failure_threshold_percent INTEGER NOT NULL CHECK (failure_threshold_percent BETWEEN 1 AND 100),
    min_candidate_samples     INTEGER NOT NULL CHECK (min_candidate_samples >= 1),
    window_minutes            INTEGER NOT NULL CHECK (window_minutes >= 1),
    updated_at                TEXT NOT NULL
);

-- 熔断记录：每次触发一行——旧策略修订号、窗口内候选样本数、失败数与触发时间，
-- 并留存触发依据的设置快照供事后核对。记录只增不改。
CREATE TABLE IF NOT EXISTS circuit_breaks (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_revision           INTEGER NOT NULL,              -- 触发时（旧）策略修订号
    failure_threshold_percent INTEGER NOT NULL,              -- 触发依据的设置快照
    min_candidate_samples     INTEGER NOT NULL,
    window_minutes            INTEGER NOT NULL,
    sample_count              INTEGER NOT NULL,              -- 窗口内同修订号的候选判定数
    failure_count             INTEGER NOT NULL,              -- 其中失败数
    triggered_at              TEXT NOT NULL
);

-- 投递回执：投递系统取得已持久化的成功判定后，按投递编号 + 终点邮箱 + 尝试序号
-- 回传实际投递结果（暂时失败 / 永久失败 / 已送达）。
-- 只允许成功判定中列出的终点；未知编号、原判定失败、非所属终点一律拒绝且不写记录。
-- 同一 (投递编号, 终点, 序号) 只允许一种结果：同结果重试原样返回，不同结果报冲突；
-- 新序号必须大于该终点已记录的最大序号；已送达 / 永久失败为终结结果，之后不得再收新尝试。
-- 回执与判定、策略、熔断统计完全解耦：写入回执不影响判定重放、核对报表与灰度熔断。
CREATE TABLE IF NOT EXISTS delivery_attempts (
    delivery_id TEXT NOT NULL REFERENCES delivery_decisions (delivery_id) ON DELETE CASCADE,
    mailbox     TEXT NOT NULL,                    -- 判定终点邮箱（规范化小写）
    attempt_no  INTEGER NOT NULL CHECK (attempt_no >= 1),   -- 正整数尝试序号
    result      TEXT NOT NULL CHECK (result IN ('temporary_failure', 'permanent_failure', 'delivered')),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (delivery_id, mailbox, attempt_no)
);

-- 投递任务租约：投递系统按“领取上限 + 租约秒数”领取当前到期、可重试的判定终点。
-- 每行是一次领取（令牌唯一）：active 为有效租约，completed 已凭令牌提交回执结束，
-- expired 为到期未完成（领取新一轮时把已过期的 active 行批量置为 expired，旧令牌随之失效）。
-- 部分唯一索引保证同一 (投递编号, 终点) 至多一条 active 租约，并发领取不会重复分配。
-- 到期时间不入库：无回执立即到期；最新回执为 temporary_failure 时按
-- min(60×2^(attempt_no−1), 3600) 秒由该回执的 created_at 现算；delivered /
-- permanent_failure 为终结结果，不再入列。
CREATE TABLE IF NOT EXISTS delivery_leases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  TEXT NOT NULL REFERENCES delivery_decisions (delivery_id) ON DELETE CASCADE,
    mailbox      TEXT NOT NULL,                    -- 领取的判定终点邮箱
    attempt_no   INTEGER NOT NULL CHECK (attempt_no >= 1),   -- 本次领取对应的下一尝试序号
    lease_token  TEXT NOT NULL UNIQUE,             -- 租约令牌（完成 / 失效判定的唯一凭据）
    status       TEXT NOT NULL CHECK (status IN ('active', 'completed', 'expired')),
    expires_at   TEXT NOT NULL,                    -- 租约到期时刻（UTC ISO 秒精度）
    created_at   TEXT NOT NULL,                    -- 领取时刻
    completed_at TEXT                              -- 凭令牌完成时刻；过期行保持 NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_leases_active
ON delivery_leases (delivery_id, mailbox)
WHERE status = 'active';
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)  # 自动提交，事务显式控制
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(config.DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # 仅在全新库（当前确为空草稿 rev0）补写初始快照；旧库升级不臆造历史快照。
        missing = conn.execute(
            "SELECT 1 FROM draft_meta WHERE id = 1 AND revision = 0 "
            "AND NOT EXISTS (SELECT 1 FROM draft_revisions WHERE revision = 0)"
        ).fetchone()
        if missing is not None:
            conn.execute(
                "INSERT INTO draft_revisions (revision, rules, rule_count, created_at) "
                "VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))"
            )
    finally:
        conn.close()


@contextmanager
def transaction():
    """BEGIN IMMEDIATE 事务：进入即取写锁，保证“核对修订号 + 写入”原子完成。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_transaction():
    """只读事务：同一快照内完成多次读取（如草稿 + 基线版本），期间不受并发写影响。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
