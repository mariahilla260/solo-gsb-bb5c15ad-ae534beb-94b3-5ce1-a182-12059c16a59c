"""resolve() 单元测试：运行期的成环 / 无路由 / 拒收（绕过发布校验直接构造规则集）。"""
import os
import tempfile

os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest

from app.routing import ResolutionError, Rule, RuleSet, Target, resolve


def route(key, *targets, match_type="exact"):
    return Rule(match_type=match_type, match_key=key, action="route", targets=tuple(targets))


def alias(value):
    return Target(kind="alias", value=value)


def mailbox(value):
    return Target(kind="mailbox", value=value)


def test_cycle_fails_whole_resolution():
    ruleset = RuleSet([
        route("a@x.example.com", mailbox("ok@mail.example.com"), alias("b@x.example.com")),
        route("b@x.example.com", alias("a@x.example.com")),
    ])
    with pytest.raises(ResolutionError) as exc:
        resolve("a@x.example.com", ruleset)
    assert exc.value.kind == "cycle"
    assert exc.value.chain[0] == "a@x.example.com"


def test_self_loop_is_cycle():
    ruleset = RuleSet([route("a@x.example.com", alias("a@x.example.com"))])
    with pytest.raises(ResolutionError) as exc:
        resolve("a@x.example.com", ruleset)
    assert exc.value.kind == "cycle"


def test_no_route_fails():
    ruleset = RuleSet([route("a@x.example.com", alias("ghost@y.example.com"))])
    with pytest.raises(ResolutionError) as exc:
        resolve("a@x.example.com", ruleset)
    assert exc.value.kind == "no_route"
    assert exc.value.address == "ghost@y.example.com"


def test_any_rejected_branch_fails_even_with_valid_others():
    ruleset = RuleSet([
        route("a@x.example.com", mailbox("ok@mail.example.com"), alias("b@x.example.com")),
        Rule(match_type="exact", match_key="b@x.example.com", action="reject", targets=()),
    ])
    with pytest.raises(ResolutionError) as exc:
        resolve("a@x.example.com", ruleset)
    assert exc.value.kind == "rejected"


def test_mailbox_targets_do_not_recurse():
    # 终点邮箱恰好也有规则：不再递归
    ruleset = RuleSet([
        route("a@x.example.com", mailbox("b@x.example.com")),
        route("b@x.example.com", mailbox("c@mail.example.com")),
    ])
    endpoints = resolve("a@x.example.com", ruleset)
    assert endpoints == {"b@x.example.com": [["a@x.example.com", "b@x.example.com"]]}


def test_domain_fallback_only_when_no_exact():
    ruleset = RuleSet([
        route("x.example.com", mailbox("catchall@mail.example.com"), match_type="domain"),
        route("vip@x.example.com", mailbox("vip@mail.example.com")),
    ])
    assert resolve("vip@x.example.com", ruleset) == {
        "vip@mail.example.com": [["vip@x.example.com", "vip@mail.example.com"]]
    }
    assert resolve("other@x.example.com", ruleset) == {
        "catchall@mail.example.com": [["other@x.example.com", "catchall@mail.example.com"]]
    }
