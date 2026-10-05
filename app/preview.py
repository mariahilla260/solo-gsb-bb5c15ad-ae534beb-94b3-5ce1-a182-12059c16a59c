"""发布前预览：草稿 vs 基线版本的投递差异。

纯内存计算，不落库：对两侧规则快照逐一解析并比较——
- 入口地址：两侧全部精确规则地址与别名目标（并集），别名下游变化由此追溯到受影响入口；
- 域兜底类别：两侧任一处出现过域规则的每个域，排除上述具体地址，
  用一个避开两侧精确地址与别名目标的合成示例地址代表。

比较沿用 resolve() 的精确优先、逐跳展开与整次失败语义；
路径排列顺序不计差异，仅终点集合、到达路径集合或失败结果（类别 + 失败点 + 到达链）不同才列出。
"""
from __future__ import annotations

from .routing import ResolutionError, Rule, RuleSet, resolve


def _named_addresses(*rule_lists: list[Rule]) -> set[str]:
    """两侧被单独命名的地址：精确规则键 + 所有规则目标里的内部别名。"""
    named: set[str] = set()
    for rules in rule_lists:
        for rule in rules:
            if rule.match_type == "exact":
                named.add(rule.match_key)
            for target in rule.targets:
                if target.kind == "alias":
                    named.add(target.value)
    return named


def probe_addresses(base_rules: list[Rule], draft_rules: list[Rule]) -> list[str]:
    """需要逐一比较的入口：两侧全部精确规则地址与别名目标（并集）。"""
    return sorted(_named_addresses(base_rules, draft_rules))


def fallback_domains(base_rules: list[Rule], draft_rules: list[Rule]) -> list[str]:
    """两侧任一处出现过域规则的域。"""
    domains: set[str] = set()
    for rules in (base_rules, draft_rules):
        for rule in rules:
            if rule.match_type == "domain":
                domains.add(rule.match_key)
    return sorted(domains)


def sample_address(domain: str, taken: set[str]) -> str:
    """域兜底类别的示例地址：合法且避开两侧全部精确地址与别名目标。"""
    n = 0
    while True:
        suffix = f"-{n}" if n else ""
        candidate = f"_preview-sample{suffix}@{domain}"
        if candidate not in taken:
            return candidate
        n += 1


def probe(address: str, ruleset: RuleSet) -> dict:
    """单个地址在一侧快照下的完整结果：成功含全部终点与路径，失败含错误详情。

    形状与 /resolve 的响应 / 错误详情一致，便于调用方复用既有解析。
    """
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        return {"status": "failed", "error": exc.detail()}
    return {
        "status": "ok",
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


def _signature(result: dict) -> tuple:
    """比较签名：路径按集合处理（排列顺序不计）；失败比较类别 + 失败点 + 到达链。"""
    if result["status"] == "failed":
        err = result["error"]
        return ("failed", err["error"], err["address"], tuple(err["chain"]))
    return (
        "ok",
        tuple(
            sorted(
                (e["mailbox"], frozenset(tuple(p) for p in e["paths"]))
                for e in result["endpoints"]
            )
        ),
    )


def results_differ(base: dict, draft: dict) -> bool:
    return _signature(base) != _signature(draft)


def preview(base_rules: list[Rule], draft_rules: list[Rule]) -> dict:
    """对两份规则快照做全量对比，只列出结果有变化的入口与域兜底类别。"""
    base_set = RuleSet(base_rules)
    draft_set = RuleSet(draft_rules)

    changed_addresses: list[dict] = []
    for address in probe_addresses(base_rules, draft_rules):
        base_result = probe(address, base_set)
        draft_result = probe(address, draft_set)
        if results_differ(base_result, draft_result):
            changed_addresses.append({"address": address, "base": base_result, "draft": draft_result})

    taken = _named_addresses(base_rules, draft_rules)
    changed_domain_fallbacks: list[dict] = []
    for domain in fallback_domains(base_rules, draft_rules):
        sample = sample_address(domain, taken)
        base_result = probe(sample, base_set)
        draft_result = probe(sample, draft_set)
        if results_differ(base_result, draft_result):
            changed_domain_fallbacks.append(
                {"domain": domain, "sample": sample, "base": base_result, "draft": draft_result}
            )

    return {
        "changed_addresses": changed_addresses,
        "changed_domain_fallbacks": changed_domain_fallbacks,
    }
