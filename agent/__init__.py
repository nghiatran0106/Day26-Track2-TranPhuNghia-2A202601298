"""agent — YOUR Task 3 build (FINAL-PLAN.md section 5.1: "the student's
biggest build"). Five files, each scored differently — read agent/README.md
first, it maps all five to what each decides and which of the 17 rubric
classes (CONTRACTS.md section 6.1) each one can save you from.

    gateway.py      the control plane: Gateway.decide(cmd) -> Decision
                     (CONTRACTS.md section 4, exactly)
    strategy.py      discovery / delegation / caching / replica / budget
                     policy used by the gateway
    guardrails.py    grounding (real), injection/redaction/arithmetic
                     injection/redaction/arithmetic checks and abstention
    telemetry.py     ctx.emit wrappers — your own side only, never scored
    prompt.md        the system prompt LAYERED ON TOP of kit.loop.prompt's
                     harness prompt (not a replacement for it)

Public surface, re-exported for convenience — everything below is YOUR OWN
code (RULES.md section 1: "you own, entirely: agent/"), written together in
one pass, so — unlike `kit/`'s subpackages, which degrade import-by-import
because several are written by other people concurrently (workspace hard
rule 2) — these four imports are expected to always succeed together:

    from agent import Command, Decision, GatewayContext, Gateway
    from agent import BudgetPacer, ResultCache, should_delegate
    from agent import GroundingResult, check_grounding, abstention_policy
    from agent import Telemetry, RecordingGatewayContext
"""

from __future__ import annotations

from agent.gateway import Command, Decision, Gateway, GatewayContext
from agent.guardrails import (
    ArithmeticCheckResult,
    GroundingResult,
    InjectionScanResult,
    RedactionResult,
    abstention_policy,
    check_grounding,
    redact,
    scan_for_injected_instructions,
    verify_arithmetic,
)
from agent.strategy import (
    BudgetPacer,
    ReplicaChoice,
    ResultCache,
    cheap_mask,
    disciplined_round_cost,
    careless_round_cost,
    is_catalog_trap,
    pick_replica,
    should_delegate,
    successor_of,
)
from agent.telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    # gateway.py
    "Command",
    "Decision",
    "Gateway",
    "GatewayContext",
    # guardrails.py
    "GroundingResult",
    "check_grounding",
    "InjectionScanResult",
    "scan_for_injected_instructions",
    "RedactionResult",
    "redact",
    "ArithmeticCheckResult",
    "verify_arithmetic",
    "abstention_policy",
    # strategy.py
    "BudgetPacer",
    "ReplicaChoice",
    "ResultCache",
    "cheap_mask",
    "disciplined_round_cost",
    "careless_round_cost",
    "is_catalog_trap",
    "pick_replica",
    "should_delegate",
    "successor_of",
    # telemetry.py
    "RecordingGatewayContext",
    "Telemetry",
]


if __name__ == "__main__":
    print("=== agent package: public surface ===")
    for name in __all__:
        print(f"  {name}")
    assert {"Command", "Decision", "Gateway", "GatewayContext"} <= set(__all__)
    assert {"BudgetPacer", "ResultCache", "should_delegate"} <= set(__all__)
    assert {"GroundingResult", "check_grounding", "abstention_policy"} <= set(__all__)
    assert {"Telemetry", "RecordingGatewayContext"} <= set(__all__)
    print(f"\n  {len(__all__)} public names, all import cleanly from `agent`.")

    print("\n=== one Gateway, built through the package's own surface ===")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401", sub="agent:demo-team", scopes=frozenset({"wiki.read"}),
        credits=100, round=1, call_index=0,
    )
    gw = Gateway(ctx)
    cmd = Command(
        cmd_id="cmd:0000", kind="discover", raw="DISCOVER registry.list_servers fields=name",
        server="registry", tool="list_servers", args={}, fields=("name",), headers={},
        lease_id=None, call_index=0,
    )
    decision = gw.decide(cmd)
    print(f"  gw.decide(cmd) -> verdict={decision.verdict!r}")
    assert decision.verdict in {"forward", "rewrite"}

    print("\nagent/__init__.py import-and-export check passed.")
