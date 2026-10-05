"""请求模型与草稿规则的编辑期校验（无效地址 / 重复规则 / 空目标 / 按键合并操作组在此拒绝）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .routing import Rule, Target, is_valid_address, is_valid_domain


class TargetIn(BaseModel):
    kind: Literal["alias", "mailbox"]
    value: str


class RuleIn(BaseModel):
    match_type: Literal["exact", "domain"]
    match_key: str
    action: Literal["reject", "route"]
    targets: list[TargetIn] = Field(default_factory=list)


class RuleChange(BaseModel):
    """一次按键提交中的单个操作：upsert（设置一条规则）或 delete（删除一条规则）。"""

    op: Literal["upsert", "delete"]
    match_type: Literal["exact", "domain"]
    match_key: str
    # 仅 upsert 需要；delete 携带任何规则字段都视为请求错误
    action: Literal["reject", "route"] | None = None
    targets: list[TargetIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> "RuleChange":
        if self.op == "upsert":
            if self.action is None:
                raise ValueError("upsert change requires an action")
        elif self.action is not None or self.targets:
            raise ValueError("delete change must not carry action or targets")
        return self


class DraftChangesRequest(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")
    changes: list[RuleChange] = Field(min_length=1, description="按键的一组设置 / 删除操作")


class DraftUpdate(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")
    rules: list[RuleIn]


class PublishRequest(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class ResolveRequest(BaseModel):
    address: str
    version: int = Field(ge=1, description="已发布版本号")


class PreviewRequest(BaseModel):
    base_version: int = Field(ge=1, description="基线已发布版本号")
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class PolicyUpdate(BaseModel):
    baseline_version: int = Field(ge=1, description="基线已发布版本号")
    candidate_version: int = Field(ge=1, description="候选已发布版本号")
    candidate_percent: int = Field(ge=0, le=100, description="候选流量比例（整数 0–100）")
    revision: int = Field(ge=0, description="客户端所见策略修订号（未配置时为 0）")


class CircuitBreakerUpdate(BaseModel):
    failure_threshold_percent: int = Field(ge=1, le=100, description="候选侧失败率阈值（整数 1–100）")
    min_candidate_samples: int = Field(ge=1, description="最少候选判定数（正整数）")
    window_minutes: int = Field(ge=1, description="观察窗口（分钟，正整数）")
    revision: int = Field(ge=0, description="客户端所见策略修订号（设置针对该修订生效）")


class DeliverRequest(BaseModel):
    address: str


class DeliverySubmit(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=200, description="调用方唯一投递编号")
    address: str


class DeliveryBatchItem(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=200, description="调用方唯一投递编号")
    address: str


class DeliveryBatchSubmit(BaseModel):
    items: list[DeliveryBatchItem] = Field(
        min_length=1, max_length=100, description="非空投递项列表（单批至多 100 笔）"
    )


class DeliveryReceiptIn(BaseModel):
    """投递系统对某投递编号的某个终点邮箱回传的一次实际投递结果。"""

    mailbox: str
    attempt_no: int = Field(ge=1, description="正整数尝试序号")
    result: Literal["temporary_failure", "permanent_failure", "delivered"]


class ValidationFailure(Exception):
    """编辑期校验失败，message 直接作为 4xx 响应。"""

    def __init__(self, message: str, code: str = "invalid_rule"):
        super().__init__(message)
        self.code = code


def _normalized_key(match_type: str, match_key: str) -> tuple[str, str]:
    """规则键规范化（与规则一致：去空白、小写）；无效键直接拒绝。"""
    key = match_key.strip().lower()
    if match_type == "exact":
        if not is_valid_address(key):
            raise ValidationFailure(f"invalid address in exact rule: {match_key!r}")
    else:
        if not is_valid_domain(key):
            raise ValidationFailure(f"invalid domain in domain rule: {match_key!r}")
    return match_type, key


def normalize_rules(raw: list[RuleIn]) -> list[Rule]:
    """把请求里的规则规范化为内部 Rule，并做编辑期校验。

    - 键与目标统一小写、去空白；
    - 无效地址 / 域、重复规则、空目标（route 无目标、reject 带目标）一律拒绝。
    """
    rules: list[Rule] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in raw:
        match_type, key = _normalized_key(item.match_type, item.match_key)

        dedup = (match_type, key)
        if dedup in seen_keys:
            raise ValidationFailure(f"duplicate rule for {match_type} {key!r}")
        seen_keys.add(dedup)

        if item.action == "reject":
            if item.targets:
                raise ValidationFailure(f"reject rule for {key!r} must not have targets")
            targets: tuple[Target, ...] = ()
        else:
            if not item.targets:
                raise ValidationFailure(f"route rule for {key!r} has empty targets")
            normalized: list[Target] = []
            seen_targets: set[tuple[str, str]] = set()
            for t in item.targets:
                value = t.value.strip().lower()
                if not is_valid_address(value):
                    raise ValidationFailure(f"invalid target address {t.value!r} in rule for {key!r}")
                if (t.kind, value) not in seen_targets:
                    seen_targets.add((t.kind, value))
                    normalized.append(Target(kind=t.kind, value=value))
            targets = tuple(normalized)

        rules.append(Rule(match_type=match_type, match_key=key, action=item.action, targets=targets))

    return rules


def normalize_changes(raw: list[RuleChange]) -> list[tuple[str, str, str, Rule | None]]:
    """规范化按键操作组，返回 [(op, match_type, match_key, Rule|None), ...]（保持请求顺序）。

    复用规则键 / 内容的规范化与编辑期校验：
    - 重复操作键（规范化后相同）整组拒绝（duplicate_change_key）；
    - 无效键沿用 invalid_rule；upsert 的规则内容沿用 normalize_rules 的全部校验。
    """
    changes: list[tuple[str, str, str, Rule | None]] = []
    seen: set[tuple[str, str]] = set()

    for item in raw:
        match_type, key = _normalized_key(item.match_type, item.match_key)
        dedup = (match_type, key)
        if dedup in seen:
            raise ValidationFailure(
                f"duplicate change for {match_type} {key!r}", code="duplicate_change_key"
            )
        seen.add(dedup)

        if item.op == "upsert":
            assert item.action is not None
            normalized = normalize_rules(
                [RuleIn(match_type=match_type, match_key=key, action=item.action, targets=item.targets)]
            )
            rule: Rule | None = normalized[0]
        else:
            rule = None
        changes.append((item.op, match_type, key, rule))

    return changes
