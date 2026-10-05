"""HTTP API：草稿编辑（乐观锁）、发布（原子校验 + 不可变版本）、按版本解析、发布前预览、默认投递策略、幂等投递判定（单笔与批量）、投递回执（按终点 + 序号回传实际结果与汇总查询）、历史判定核对报表与灰度自动熔断。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import db, preview, reporting, store
from .models import (
    CircuitBreakerUpdate,
    DeliverRequest,
    DeliveryBatchSubmit,
    DeliveryReceiptIn,
    DeliverySubmit,
    DraftChangesRequest,
    DraftUpdate,
    PolicyUpdate,
    PreviewRequest,
    PublishRequest,
    ResolveRequest,
    ValidationFailure,
    normalize_changes,
    normalize_rules,
)
from .routing import ResolutionError, RuleSet, is_valid_address, resolve, validate_ruleset
from .traffic import select_version


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # 首次启动自动建表
    yield


app = FastAPI(title="Alias Router", version="1.0.0", lifespan=lifespan)


def _conflict(exc: store.RevisionConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/draft")
def get_draft() -> dict:
    with db.connect() as conn:
        revision, rules = store.get_draft(conn)
    return {"revision": revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.put("/draft")
def put_draft(body: DraftUpdate) -> dict:
    """整体替换草稿。必须携带所见修订号，冲突拒绝（409）。"""
    try:
        rules = normalize_rules(body.rules)
    except ValidationFailure as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_rule", "message": str(exc)})
    try:
        with db.transaction() as conn:
            new_revision = store.replace_draft(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"revision": new_revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.post("/draft/changes")
def patch_draft(body: DraftChangesRequest) -> dict:
    """多人协作：按规则键合并一组设置（upsert）/ 删除（delete）操作。

    规则键与内容沿用现有规范化与编辑期校验；重复操作键、删除当前不存在的规则、
    无效规则一律整组拒绝（422），草稿不变。
    所见修订落后时，仅当本次操作涉及的每个键相对所见快照在当前草稿中均未改变，
    才把整组操作合并并产生一个新修订；否则 409 返回全部冲突键与当前修订号，草稿不变。
    无法取得所见修订的快照时明确拒绝合并（409）。
    核对与写入在单个 BEGIN IMMEDIATE 事务内原子完成。
    """
    try:
        changes = normalize_changes(body.changes)
    except ValidationFailure as exc:
        raise HTTPException(status_code=422, detail={"error": exc.code, "message": str(exc)})
    try:
        with db.transaction() as conn:
            new_revision, rules = store.merge_draft(conn, body.revision, changes)
    except store.MergeConflict as exc:  # 必须先于父类 RevisionConflict 捕获
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "current_revision": exc.current,
                "conflict_keys": exc.conflict_keys,
                "message": f"revision conflict: {len(exc.conflict_keys)} involved key(s) changed since the seen revision",
            },
        )
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except store.RevisionSnapshotUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_snapshot_unavailable",
                "revision": exc.revision,
                "current_revision": exc.current,
                "message": f"snapshot for seen revision {exc.revision} is unavailable; merge refused",
            },
        )
    except store.DraftRuleNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "rule_not_found",
                "match_type": exc.match_type,
                "match_key": exc.match_key,
                "message": str(exc),
            },
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"revision": new_revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.get("/draft/revisions")
def list_draft_revisions() -> dict:
    """列出全部草稿修订（含整体替换与按键合并；revision 0 为初始空草稿）。"""
    with db.connect() as conn:
        return {"revisions": store.list_draft_revisions(conn)}


@app.get("/draft/revisions/{revision}")
def get_draft_revision(revision: int) -> dict:
    """查看某个草稿修订的完整规则快照（可核对每次草稿变更）。"""
    with db.connect() as conn:
        found = store.get_draft_at(conn, revision)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "draft_revision_not_found", "revision": revision},
        )
    rev, rules = found
    return {"revision": rev, "rules": [store.rule_to_dict(r) for r in rules]}


@app.post("/publish")
def publish(body: PublishRequest) -> dict:
    """原子核对修订号并校验全部规则目标可解析且无环；成功生成不可变版本，失败保持旧版。"""
    try:
        with db.transaction() as conn:
            revision, rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            problems = validate_ruleset(RuleSet(rules))
            if problems:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "validation_failed", "message": "unresolvable targets or routing loops", "problems": problems},
                )
            version = store.publish(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"version": version, "source_revision": body.revision, "rule_count": len(rules)}


@app.get("/versions")
def list_versions() -> dict:
    with db.connect() as conn:
        return {"versions": store.list_versions(conn)}


@app.get("/versions/{version}")
def get_version(version: int) -> dict:
    with db.connect() as conn:
        found = store.get_version(conn, version)
    if found is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    return {
        "version": found["version"],
        "source_revision": found["source_revision"],
        "created_at": found["created_at"],
        "rules": [store.rule_to_dict(r) for r in found["rules"]],
    }


@app.post("/resolve")
def resolve_address(body: ResolveRequest) -> dict:
    """按指定已发布版本解析收件地址。任一分支拒收 / 无路由 / 成环则整次失败。"""
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.connect() as conn:
        ruleset = store.load_ruleset(conn, body.version)
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": body.version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail())
    return {
        "address": address,
        "version": body.version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


@app.post("/preview")
def preview_changes(body: PreviewRequest) -> dict:
    """发布前预览：当前草稿相对基线版本的投递变化。

    在同一只读事务快照内读取基线版本与草稿（不改动草稿或历史版本），
    对两侧全部精确规则地址与别名目标逐一解析，并对两侧出现过域规则的每个域
    用合成示例地址探测兜底类别；仅列出结果有变化的项，附两侧完整结果。
    草稿尚未通过发布校验时仍正常展示相应失败结果。
    """
    try:
        with db.read_transaction() as conn:
            revision, draft_rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            found = store.get_version(conn, body.base_version)
            if found is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "version_not_found", "version": body.base_version},
                )
            base_rules = found["rules"]
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    diff = preview.preview(base_rules, draft_rules)
    return {"base_version": body.base_version, "revision": revision, **diff}


# ---------- 默认投递策略（已发布版本间的灰度分桶） ----------

def _policy_doc(policy: dict) -> dict:
    return {
        "revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "updated_at": policy["updated_at"],
    }


@app.get("/policy")
def get_policy() -> dict:
    """查看当前默认投递策略。从未配置时返回 404（策略修订号视为 0）。"""
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
    if policy is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "policy_not_configured", "revision": 0,
                    "message": "no delivery policy configured"},
        )
    return _policy_doc(policy)


@app.put("/policy")
def put_policy(body: PolicyUpdate) -> dict:
    """配置默认投递策略：基线 / 候选均须为已发布版本。

    在单个 BEGIN IMMEDIATE 事务内核验所见策略修订号并保存；
    修订号冲突（409）或引用未发布版本（422）时回滚，原策略不被覆盖。
    草稿与历史版本均不会因策略变更而改写。
    """
    try:
        with db.transaction() as conn:
            policy = store.replace_policy(
                conn,
                body.revision,
                body.baseline_version,
                body.candidate_version,
                body.candidate_percent,
            )
    except store.RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
        )
    except store.PolicyVersionNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "version_not_published", "version": exc.version,
                    "message": f"only published versions may be used in a delivery policy: {exc.version}"},
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return _policy_doc(policy)


# ---------- 灰度自动熔断（候选侧失败率达阈值 → 候选比例自动置 0） ----------

def _circuit_breaker_settings_doc(settings: dict) -> dict:
    return {
        "policy_revision": settings["policy_revision"],
        "failure_threshold_percent": settings["failure_threshold_percent"],
        "min_candidate_samples": settings["min_candidate_samples"],
        "window_minutes": settings["window_minutes"],
        "updated_at": settings["updated_at"],
    }


@app.put("/policy/circuit-breaker")
def put_circuit_breaker(body: CircuitBreakerUpdate) -> dict:
    """凭所见策略修订号设置灰度熔断：失败率阈值（1–100）、最少候选判定数与观察窗口（分钟）。

    设置针对当前策略修订号生效；人工更新策略（PUT /policy）或自动熔断都会使策略
    修订号 +1，旧设置随之失效，须凭新修订号重新设置。
    未配置策略（409 policy_not_configured）、修订号冲突（409 revision_conflict）、
    非法参数（422）都明确报错且不产生任何熔断；核对与保存在单个
    BEGIN IMMEDIATE 事务内原子完成，与并发投递提交互串行化、互不覆盖。
    """
    try:
        with db.transaction() as conn:
            settings = store.save_circuit_breaker_settings(
                conn,
                body.revision,
                body.failure_threshold_percent,
                body.min_candidate_samples,
                body.window_minutes,
            )
    except store.PolicyNotConfigured as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "policy_not_configured",
                    "message": "no delivery policy configured; configure one via PUT /policy first"},
        )
    except store.RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return _circuit_breaker_settings_doc(settings)


@app.get("/policy/circuit-breaker")
def get_circuit_breaker() -> dict:
    """查询当前熔断设置与全部熔断记录。

    settings 仅在对当前策略修订号有效时非空（人工更新策略或自动熔断后旧设置失效，
    为 null，须重新设置）；breaks 按触发顺序列出每次熔断的旧策略修订号、
    窗口内候选样本数、失败数与触发时间。未配置策略时返回 404。
    """
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
        if policy is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "policy_not_configured", "revision": 0,
                        "message": "no delivery policy configured"},
            )
        settings = store.get_active_circuit_breaker_settings(conn, policy["revision"])
        breaks = store.list_circuit_breaks(conn)
    return {
        "policy_revision": policy["revision"],
        "settings": _circuit_breaker_settings_doc(settings) if settings is not None else None,
        "breaks": breaks,
    }


@app.post("/deliver")
def deliver(body: DeliverRequest) -> dict:
    """默认投递：按当前策略与规范化地址稳定分桶，选择基线或候选已发布版本解析。

    未配置策略时明确报错（409），不使用草稿或最新版本兜底；
    同一地址在同一策略下总选同一版本；选中版本的拒收 / 无路由 / 成环
    仍整次失败（422），不自动改用另一版本。
    """
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
        if policy is None:
            raise HTTPException(
                status_code=409,
                detail={"error": "policy_not_configured",
                        "message": "no delivery policy configured; specify an explicit version via /resolve"},
            )
        chosen = select_version(address, policy["candidate_percent"])
        version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
        ruleset = store.load_ruleset(conn, version)
    # 策略引用的版本必然已发布（写入时有外键 + 发布版本不可变），此处仅作防御
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        detail = exc.detail()
        detail.update({"policy_revision": policy["revision"], "version": version, "selection": chosen})
        raise HTTPException(status_code=422, detail=detail)
    return {
        "address": address,
        "policy_revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "selection": chosen,
        "version": version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


# ---------- 幂等投递判定（投递编号 → 首次判定持久化，重试 / 并发原样重放） ----------

def _decision_doc(decision: dict) -> dict:
    """成功判定的响应体：/deliver 的策略与终点字段 + 投递编号。重放时内容完全相同。"""
    return {
        "delivery_id": decision["delivery_id"],
        "address": decision["address"],
        "policy_revision": decision["policy_revision"],
        "baseline_version": decision["baseline_version"],
        "candidate_version": decision["candidate_version"],
        "candidate_percent": decision["candidate_percent"],
        "selection": decision["selection"],
        "version": decision["version"],
        "endpoints": decision["endpoints"],
    }


@app.post("/deliveries")
def submit_delivery(body: DeliverySubmit) -> dict:
    """按投递编号幂等提交路由判定（供可能超时重试的投递系统使用）。

    首次受理：按当前默认策略选定已发布版本，把规范化地址、策略修订号、选中版本、
    完整终点路径（或拒收 / 无路由 / 成环错误）作为一份判定持久化并返回。
    相同编号 + 相同规范化地址的并发或后续请求原样返回首次判定（含原错误状态），
    即使策略随后改变；编号相同而地址不同返回 409。
    无效地址（422）与未配置策略（409）不占用编号；选中版本失败整次失败，不切换版本。
    首次判定持久化后，若当前策略修订号设有熔断设置，则按窗口内候选侧失败率评估：
    达到条件时候选比例置 0、策略修订号 +1（只触发一次，影响后续投递）；
    本次请求仍返回已持久化的原判定，同编号重放不参与统计。
    """
    address = body.address.strip().lower()
    if not is_valid_address(address):  # 无效地址不占用编号
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    conflict = False
    try:
        with db.transaction() as conn:
            decision = store.get_decision(conn, body.delivery_id)
            if decision is not None:  # 已有判定：地址一致 → 重放；不一致 → 冲突
                conflict = decision["address"] != address
            else:
                policy = store.get_policy(conn)
                if policy is None:  # 回滚：未配置策略不占用编号
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "policy_not_configured",
                                "message": "no delivery policy configured; specify an explicit version via /resolve"},
                    )
                chosen = select_version(address, policy["candidate_percent"])
                version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
                ruleset = store.load_ruleset(conn, version)
                if ruleset is None:  # 防御：策略引用的版本必然已发布且不可变
                    raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
                try:
                    endpoints = resolve(address, ruleset)
                except ResolutionError as exc:  # 选中版本失败：整次失败并持久化，不切换版本
                    detail = exc.detail()
                    detail.update({
                        "delivery_id": body.delivery_id,
                        "policy_revision": policy["revision"],
                        "version": version,
                        "selection": chosen,
                    })
                    decision = store.save_decision(
                        conn, delivery_id=body.delivery_id, address=address, policy=policy,
                        selection=chosen, version=version, status="failed", endpoints=None, error=detail,
                    )
                else:
                    endpoint_docs = [
                        {"mailbox": mailbox, "paths": sorted(paths)}
                        for mailbox, paths in sorted(endpoints.items())
                    ]
                    decision = store.save_decision(
                        conn, delivery_id=body.delivery_id, address=address, policy=policy,
                        selection=chosen, version=version, status="ok", endpoints=endpoint_docs, error=None,
                    )
                # 首次判定已持久化：按当前策略修订号的熔断设置评估候选侧失败率。
                # 触发也只把候选比例置 0 并递增策略修订号（影响后续投递）；
                # 本次响应仍为上面已持久化的原判定，历史判定不改写。
                store.maybe_trigger_circuit_breaker(conn, policy)
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    if conflict:
        raise HTTPException(
            status_code=409,
            detail={"error": "delivery_id_conflict", "delivery_id": body.delivery_id,
                    "message": f"delivery id {body.delivery_id!r} already submitted with a different address"},
        )
    if decision["status"] == "failed":  # 首次失败与重放都返回原错误状态
        raise HTTPException(status_code=422, detail=decision["error"])
    return _decision_doc(decision)


def _batch_item_doc(decision: dict) -> dict:
    """批量提交中单项判定的响应：成功含终点路径，失败含原错误详情；重放内容完全相同。"""
    return {
        "delivery_id": decision["delivery_id"],
        "status": decision["status"],
        "address": decision["address"],
        "policy_revision": decision["policy_revision"],
        "baseline_version": decision["baseline_version"],
        "candidate_version": decision["candidate_version"],
        "candidate_percent": decision["candidate_percent"],
        "selection": decision["selection"],
        "version": decision["version"],
        "endpoints": decision["endpoints"],
        "error": decision["error"],
    }


@app.post("/deliveries/batch")
def submit_delivery_batch(body: DeliveryBatchSubmit) -> dict:
    """一次提交多笔收件判定（非空、单批至多 100 笔），返回与输入顺序一致的逐项判定。

    整批先校验，任一不满足即整批拒绝且不写入：
    - 任一地址无效 → 422 invalid_address（附全部无效项）；
    - 批内重复投递编号 → 422 duplicate_delivery_id；
    - 既有编号绑定了其他规范化地址 → 409 delivery_id_conflict（附全部冲突项）；
    - 尚无默认策略且存在新编号 → 409 policy_not_configured。
    已有同编号同地址的判定原样重放；其余项目在同一事务中共用受理时的策略修订号
    与比例，分别稳定分桶、解析并持久化（路由失败仍作为判定保存，含原错误详情）。
    新判定全部写入后仅评估一次该修订号的候选侧熔断（触发只影响后续请求）；
    重放不重复计数。
    """
    normalized: list[tuple[str, str]] = []
    invalid_items: list[dict] = []
    for index, item in enumerate(body.items):
        address = item.address.strip().lower()
        if not is_valid_address(address):
            invalid_items.append({"index": index, "delivery_id": item.delivery_id, "address": item.address})
        normalized.append((item.delivery_id, address))
    if invalid_items:  # 无效地址：整批拒绝，不写入
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_address", "invalid_items": invalid_items,
                    "message": f"{len(invalid_items)} invalid address(es) in batch"},
        )
    seen: set[str] = set()
    duplicates: set[str] = set()
    for delivery_id, _ in normalized:
        if delivery_id in seen:
            duplicates.add(delivery_id)
        seen.add(delivery_id)
    if duplicates:  # 批内重复编号：整批拒绝，不写入
        raise HTTPException(
            status_code=422,
            detail={"error": "duplicate_delivery_id", "delivery_ids": sorted(duplicates),
                    "message": f"duplicate delivery id(s) in batch: {', '.join(sorted(duplicates))}"},
        )
    try:
        with db.transaction() as conn:
            existing = store.get_decisions(conn, [did for did, _ in normalized])
            conflicts = [
                {
                    "delivery_id": did,
                    "submitted_address": address,
                    "stored_address": existing[did]["address"],
                }
                for did, address in normalized
                if did in existing and existing[did]["address"] != address
            ]
            if conflicts:  # 既有编号绑定其他规范化地址：整批拒绝，不写入
                raise HTTPException(
                    status_code=409,
                    detail={"error": "delivery_id_conflict", "conflicts": conflicts,
                            "message": f"{len(conflicts)} delivery id(s) already submitted with a different address"},
                )
            decisions = dict(existing)
            new_items = [(did, address) for did, address in normalized if did not in existing]
            if new_items:
                policy = store.get_policy(conn)
                if policy is None:  # 回滚：尚无默认策略且存在新编号，整批拒绝
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "policy_not_configured",
                                "message": "no delivery policy configured; configure one via PUT /policy first"},
                    )
                rulesets: dict[int, RuleSet] = {}  # 同批共用受理时的策略：按版本缓存规则集
                for did, address in new_items:
                    chosen = select_version(address, policy["candidate_percent"])
                    version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
                    if version not in rulesets:
                        ruleset = store.load_ruleset(conn, version)
                        if ruleset is None:  # 防御：策略引用的版本必然已发布且不可变
                            raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
                        rulesets[version] = ruleset
                    try:
                        endpoints = resolve(address, rulesets[version])
                    except ResolutionError as exc:  # 路由失败仍作为判定持久化，不切换版本
                        detail = exc.detail()
                        detail.update({
                            "delivery_id": did,
                            "policy_revision": policy["revision"],
                            "version": version,
                            "selection": chosen,
                        })
                        decisions[did] = store.save_decision(
                            conn, delivery_id=did, address=address, policy=policy,
                            selection=chosen, version=version, status="failed", endpoints=None, error=detail,
                        )
                    else:
                        endpoint_docs = [
                            {"mailbox": mailbox, "paths": sorted(paths)}
                            for mailbox, paths in sorted(endpoints.items())
                        ]
                        decisions[did] = store.save_decision(
                            conn, delivery_id=did, address=address, policy=policy,
                            selection=chosen, version=version, status="ok", endpoints=endpoint_docs, error=None,
                        )
                # 新判定全部写入后，仅评估一次受理时修订号的候选侧熔断：
                # 触发只影响后续请求；本批响应仍为已持久化的原判定，重放不进入统计。
                store.maybe_trigger_circuit_breaker(conn, policy)
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"results": [_batch_item_doc(decisions[did]) for did, _ in normalized]}


# ---------- 历史投递判定核对报表（时间窗 + 分组计数 + 游标分页） ----------

_REPORT_ITEM_FIELDS = (
    "delivery_id", "address", "policy_revision", "version",
    "selection", "status", "error_type", "created_at",
)


@app.get("/deliveries/report")
def delivery_report(
    start: str | None = None,
    end: str | None = None,
    policy_revision: int | None = Query(default=None, ge=0),
    page_size: int | None = None,
    cursor: str | None = None,
) -> dict:
    """按带时区的起止时刻（左闭右开）与可选策略修订号核对历史投递判定。

    返回按选中版本、分流侧（基线/候选）、结果（ok 或失败原因）分组的数量，
    并分页列出判定明细；已持久化的失败判定同样计入，同一编号只计一次。
    首次查询在单只读事务内固定“截至当时已提交判定”的边界并随 next_cursor 返回；
    后续翻页只凭 cursor（不再携带 start/end/policy_revision），沿用同一筛选与边界，
    并发新投递不会进入旧报表，分组总数与明细一致，页间不漏不重。
    无效时间窗（invalid_time_window）、非法页大小（invalid_page_size）、
    无效游标（invalid_cursor）均返回 422。
    """
    try:
        size = reporting.validate_page_size(page_size)
        if cursor is not None:  # 翻页：筛选与边界全部由游标携带，不允许再传筛选参数
            if start is not None or end is not None or policy_revision is not None:
                raise reporting.ReportQueryError(
                    "invalid_cursor",
                    "cursor already carries the filter and snapshot boundary; "
                    "do not pass start/end/policy_revision together with it",
                )
            state = reporting.decode_cursor(cursor)
            window = (state.start, state.end, state.policy_revision)
            snapshot_seq, after_seq = state.snapshot_seq, state.after_seq
        else:  # 首次查询：校验时间窗，边界在下面的只读事务内固定
            window = (*reporting.parse_time_window(start, end), policy_revision)
            snapshot_seq, after_seq = None, 0
        with db.read_transaction() as conn:
            if snapshot_seq is None:
                snapshot_seq = store.report_snapshot_seq(conn)
            w_start, w_end, w_policy = window
            groups = store.report_groups(
                conn, start=w_start, end=w_end,
                policy_revision=w_policy, snapshot_seq=snapshot_seq,
            )
            total = store.report_total(
                conn, start=w_start, end=w_end,
                policy_revision=w_policy, snapshot_seq=snapshot_seq,
            )
            rows = store.report_items(
                conn, start=w_start, end=w_end,
                policy_revision=w_policy, snapshot_seq=snapshot_seq,
                after_seq=after_seq, limit=size + 1,  # 多取一条判断是否还有下一页
            )
    except reporting.ReportQueryError as exc:
        raise HTTPException(status_code=422, detail={"error": exc.code, "message": exc.message})

    page = rows[:size]
    next_cursor = None
    if len(rows) > size:  # 还有后续页：把同一筛选、同一边界与页位置编码进游标
        next_cursor = reporting.encode_cursor(
            reporting.ReportCursor(
                start=w_start, end=w_end, policy_revision=w_policy,
                snapshot_seq=snapshot_seq, after_seq=page[-1]["seq"],
            )
        )
    return {
        "window": {"start": w_start, "end": w_end, "policy_revision": w_policy},
        "page_size": size,
        "total": total,
        "groups": groups,
        "items": [{k: item[k] for k in _REPORT_ITEM_FIELDS} for item in page],
        "next_cursor": next_cursor,
    }


@app.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: str) -> dict:
    """查询已持久化的投递判定（含失败判定与原错误详情）；未知编号返回 404。"""
    with db.read_transaction() as conn:
        decision = store.get_decision(conn, delivery_id)
    if decision is None:
        raise HTTPException(status_code=404, detail={"error": "delivery_not_found", "delivery_id": delivery_id})
    return {
        "delivery_id": decision["delivery_id"],
        "address": decision["address"],
        "status": decision["status"],
        "policy_revision": decision["policy_revision"],
        "baseline_version": decision["baseline_version"],
        "candidate_version": decision["candidate_version"],
        "candidate_percent": decision["candidate_percent"],
        "selection": decision["selection"],
        "version": decision["version"],
        "endpoints": decision["endpoints"],
        "error": decision["error"],
        "created_at": decision["created_at"],
    }


# ---------- 投递回执：成功判定后按终点 + 序号回传实际投递结果，并查询尝试记录与汇总 ----------

@app.post("/deliveries/{delivery_id}/receipts")
def submit_delivery_receipt(delivery_id: str, body: DeliveryReceiptIn) -> dict:
    """投递系统取得已持久化的成功判定后，按投递编号、终点邮箱与正整数尝试序号
    回传实际投递结果（temporary_failure / permanent_failure / delivered）。

    - 未知编号 → 404 delivery_not_found；原判定失败（拒收 / 无路由 / 成环）→
      409 delivery_decision_failed；终点不在判定列出的终点中（规范化后比较）→
      409 endpoint_not_allowed：均拒绝且不产生记录。
    - 同一终点同一序号同结果重试 → 原样返回首次记录；同序号不同结果 →
      409 attempt_result_conflict。
    - 新序号必须大于该终点已记录的最大序号，否则 409 attempt_sequence_conflict；
      已送达 / 永久失败后再收新尝试 → 409 endpoint_terminal。
    核对与写入在单个 BEGIN IMMEDIATE 事务内完成：并发回传串行化，序号与汇总一致。
    回执只增不改，不影响判定查询、同编号重放与灰度熔断统计。
    """
    mailbox = body.mailbox.strip().lower()
    if not is_valid_address(mailbox):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_address", "message": f"invalid mailbox: {body.mailbox!r}"},
        )
    try:
        with db.transaction() as conn:
            attempt = store.record_attempt(
                conn, delivery_id, mailbox, body.attempt_no, body.result
            )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"error": "delivery_not_found", "delivery_id": delivery_id},
        )
    except store.ReceiptDecisionFailed as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "delivery_decision_failed", "delivery_id": delivery_id,
                    "decision_status": exc.status, "error_type": exc.error_type,
                    "message": "receipts are only accepted for successful delivery decisions"},
        )
    except store.ReceiptEndpointNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "endpoint_not_allowed", "delivery_id": delivery_id, "mailbox": mailbox,
                    "allowed_endpoints": exc.endpoints, "message": str(exc)},
        )
    except store.ReceiptResultConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "attempt_result_conflict", "delivery_id": delivery_id, "mailbox": mailbox,
                    "attempt_no": exc.attempt_no, "stored_result": exc.stored_result,
                    "submitted_result": exc.submitted_result, "message": str(exc)},
        )
    except store.ReceiptEndpointClosed as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "endpoint_terminal", "delivery_id": delivery_id, "mailbox": mailbox,
                    "terminal_result": exc.terminal_result, "terminal_attempt_no": exc.last_attempt_no,
                    "submitted_attempt_no": exc.attempt_no, "message": str(exc)},
        )
    except store.ReceiptSequenceConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "attempt_sequence_conflict", "delivery_id": delivery_id, "mailbox": mailbox,
                    "last_attempt_no": exc.last_attempt_no, "submitted_attempt_no": exc.attempt_no,
                    "message": str(exc)},
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return attempt


@app.get("/deliveries/{delivery_id}/receipts")
def get_delivery_receipts(delivery_id: str) -> dict:
    """查询某投递编号各终点的尝试记录（按序号升序）与汇总状态。

    未知编号 → 404；原判定失败 → 409 delivery_decision_failed（失败判定没有终点，
    不产生回执）。成功判定即使尚无任何回执也返回 200：各终点状态为 pending，
    汇总为 pending。汇总：全部终点已送达 → delivered；任一终点永久失败 → failed；
    其余（含无回执）→ pending。只读，不影响判定、重放与熔断统计。
    """
    with db.read_transaction() as conn:
        try:
            summary = store.receipt_summary(conn, delivery_id)
        except store.ReceiptDecisionFailed as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "delivery_decision_failed", "delivery_id": delivery_id,
                        "decision_status": exc.status, "error_type": exc.error_type,
                        "message": "receipts are only available for successful delivery decisions"},
            )
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "delivery_not_found", "delivery_id": delivery_id},
        )
    return summary
