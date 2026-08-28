"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE IMPLEMENTATION SHAPE (read this before extending `decide()`)
----------------------------------------------------------------------------
The gateway is structured as four named jobs — ROUTE, ADMIT, AUTHORIZE, and
BUDGET — and applies each before returning a decision.  Routing and masks are
rewritten only on the trusted command representation; admission and authority
fail closed; budget accounting uses the live context and per-duel state.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
from agent.guardrails import scan_for_injected_instructions
from agent.strategy import is_catalog_trap, successor_of

try:
    from kit.mcp.specs import TOOL_SPECS, cost as _tool_cost
except ImportError:  # pragma: no cover - the gateway still fails closed
    TOOL_SPECS = {}

    def _tool_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        return 5

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are the gateway's per-duel memory.  The arena
    feeds observations back through the small note_* hooks between calls.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory ---------------------------------------------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        self._starting_credits: int | None = None
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()
        self._admitted_cards: dict[str, dict[str, Any]] = {}
        self._etags: dict[str, str] = {}
        self._idempotency_keys: set[str] = set()
        self._seen_cmd_ids: set[str] = set()
        self._round_seen = -1
        self._spent_this_round = 0

    _A2A_SERVERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})
    _WRITE_TOOLS = frozenset({
        ("progress", "record_mastery"),
        ("content", "flag_stale_slide"),
        ("content", "file_content_bug"),
    })
    # The retuned cost table makes the disciplined query + frame + provenance
    # plan cost 9 credits.  Keep a little more headroom in later rounds, where
    # a failed late call is more expensive than an early abstention.
    _ROUND_ALLOWANCE = {1: 9, 2: 9, 3: 9, 4: 9, 5: 10, 6: 10, 7: 10, 8: 11, 9: 11, 10: 12}
    _CHEAP_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
        ("registry", "list_servers"): ("name",),
        ("glossary", "list_terms"): ("term",),
        ("slides", "get_frame"): ("body", "title"),
    }

    @staticmethod
    def _identity(value: Any) -> str:
        """Compare arena identities without being fooled by harmless case drift."""
        return str(value or "").strip().casefold()

    def _deny(self, cmd: Command, reason: str, *, quarantine: bool = False) -> Decision:
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason, quarantine=quarantine)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _a2a_admitted(self, cmd: Command) -> str | None:
        if cmd.server not in self._A2A_SERVERS:
            return None
        card = self._admitted_cards.get(cmd.server)
        if not card or not card.get("verified"):
            return "A2A peer card was not admitted by the registry"
        card_name = card.get("name")
        if card_name is not None and self._identity(card_name) != self._identity(cmd.server):
            return "A2A admitted card identity does not match the target peer"
        if cmd.tool not in set(card.get("skills") or ()):
            return "A2A skill is not declared on the admitted peer card"
        signature = str(cmd.headers.get("x-card-signature", "")).casefold()
        if signature in {"invalid", "forged", "unverified"}:
            return "A2A peer card signature is invalid"
        aud = cmd.headers.get("aud")
        if aud is None:
            return "A2A delegation has no audience"
        if self._identity(aud) not in {
            self._identity(cmd.server), self._identity(f"a2a:{cmd.server}"),
            self._identity(f"mcp:{cmd.server}"),
        }:
            return "A2A delegation audience does not match the target peer"
        if cmd.args.get("peer_unverified"):
            return "A2A peer answer is explicitly unverified"
        return None

    def _target_authorized(self, cmd: Command) -> bool:
        served = self._identity(getattr(self.ctx, "act", ""))
        # The wire sometimes carries ``sv-0417`` while the context carries the
        # typed identity ``learner:sv-0417``.  Compare the subject component,
        # but never discard a genuinely different subject.
        served_subject = served.split(":", 1)[1] if ":" in served else served
        normalized_args = {str(k).casefold(): v for k, v in cmd.args.items()}
        for key in ("learner", "learner_id", "target", "subject", "act"):
            target = normalized_args.get(key)
            if target is None or not served:
                continue
            normal = self._identity(target)
            subject = normal.split(":", 1)[1] if ":" in normal else normal
            if normal != served and subject != served_subject:
                return False
        return True

    def _scope_authorized(self, cmd: Command) -> bool:
        """Require the least scope needed for this command.

        Scope checks belong in the gateway (before a ToolCall exists), not in a
        backend server.  A2A admission is handled separately because its Agent
        Card and delegation token are the authority surface for a peer call.
        """
        scopes = {self._identity(s) for s in (getattr(self.ctx, "scopes", ()) or ())}
        if not scopes:
            return False
        if (cmd.server, cmd.tool) in self._WRITE_TOOLS:
            return (
                f"wiki.write:{self._identity(cmd.server)}" in scopes
                or "wiki.write" in scopes
                or "*" in scopes
            )
        if cmd.server in self._A2A_SERVERS:
            return bool(scopes & {"a2a.delegate", "a2a.read", "wiki.read", "*"})
        return "wiki.read" in scopes or "*" in scopes

    def _estimate_cost(self, cmd: Command) -> int:
        raw_rows = next(
            (value for key, value in cmd.args.items() if str(key).casefold() in {"n_rows", "rows"}),
            1,
        )
        try:
            n_rows = int(raw_rows)
        except (TypeError, ValueError, OverflowError):
            return 10**9  # malformed row counts fail closed at the budget gate
        if isinstance(raw_rows, bool) or n_rows < 1:
            return 10**9
        try:
            return int(_tool_cost(cmd.server, cmd.tool, tuple(cmd.fields), n_rows=n_rows))
        except (KeyError, TypeError, ValueError):
            return 5

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The four jobs below are deliberately ordered so untrusted routing or
        content is rejected before authorization and budget state changes."""
        self._telemetry.decision_seen(cmd)

        # Command ids are minted by the trusted loop and are unique.  Treat a
        # replay as a free denial; this also prevents an idempotent write from
        # being authorised twice when a caller accidentally retries locally.
        if cmd.cmd_id in self._seen_cmd_ids:
            return self._deny(cmd, "command id was already evaluated this duel")
        self._seen_cmd_ids.add(cmd.cmd_id)

        # Reset only the round-local meter. All identity/write state lives for
        # the complete duel, matching the lifetime of this Gateway instance.
        try:
            round_no = int(getattr(self.ctx, "round", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            round_no = 0
        if round_no != self._round_seen:
            self._round_seen = round_no
            self._spent_this_round = 0

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: is this the right SERVER/REPLICA for this command?
        # Day18-style drift is real and measured (CORPUS-FACTS.md
        # section 2) — a `swap_replica` mutation (CONTRACTS.md section 8's
        # closed mutation-op set) can point `cmd` at a stale replica without
        # the model ever noticing. `agent/strategy.py`'s replica-choice
        # helper is where this heuristic belongs; wire its answer in here by
        # REWRITING `cmd.headers["mcp-replica"]` (verdict="rewrite") rather
        # than silently trusting whatever the model asked for.
        routed = cmd
        rewritten = False

        successor = successor_of(cmd.server, cmd.tool)
        if successor is not None:
            routed = replace(routed, server=successor[0], tool=successor[1])
            rewritten = True

        # Canonicalisation deliberately accepts unknown identities so the
        # gateway remains the final trust boundary.  Fail closed here rather
        # than spending credits on a call no registered server can execute.
        if isinstance(TOOL_SPECS, Mapping) and TOOL_SPECS and (routed.server, routed.tool) not in TOOL_SPECS:
            return self._deny(cmd, "server/tool is not present in the trusted registry")
        expected_kind = (
            "a2a" if routed.server in self._A2A_SERVERS
            else "discover" if routed.tool in {"search", "locate", "list_servers", "list_terms"}
            else "mcp"
        )
        if routed.kind != expected_kind:
            return self._deny(cmd, "command kind does not match the canonical server/tool layer")

        headers = {str(k).lower(): v for k, v in routed.headers.items()}
        args = dict(routed.args)

        # A route in the request body is exactly the ambiguity exploited by
        # header_spoof/replica_flip. Route only on the trusted header surface.
        if any(args.get(k) is not None for k in ("route", "_route", "replica")):
            return self._deny(cmd, "replica/route was supplied in the request body")
        if str(headers.get("x-server-fingerprint", "")).casefold() in {
            "unvouched", "invalid", "forged",
        }:
            return self._deny(cmd, "server identity does not match the registry pin")
        replica = headers.get("mcp-replica")
        if replica is not None and str(replica).casefold() not in {"w", "c"}:
            return self._deny(cmd, "mcp-replica must be either 'w' or 'c'")
        headers.pop("x-mcp-body-route", None)
        if routed.server not in self._A2A_SERVERS:
            headers["mcp-replica"] = str(headers.get("mcp-replica", "w")).casefold()

        # An inflated catalog marker is attacker-controlled padding, not a
        # semantic argument. Remove it and force the narrow catalog mask.
        if "catalog" in args and isinstance(args["catalog"], str) and len(args["catalog"]) > 256:
            args.pop("catalog")
            rewritten = True

        try:
            if any(not isinstance(f, str) for f in routed.fields):
                return self._deny(cmd, "field mask entries must be strings")
            fields = tuple(sorted({f.casefold() for f in routed.fields}))
        except TypeError:
            return self._deny(cmd, "field mask is not iterable")
        spec = TOOL_SPECS.get((routed.server, routed.tool)) if isinstance(TOOL_SPECS, Mapping) else None
        if spec is not None and fields != ("*",):
            unknown_fields = set(fields) - set(getattr(spec, "all_fields", ()))
            if unknown_fields:
                return self._deny(cmd, f"unknown field(s) in mask: {sorted(unknown_fields)}")
        if is_catalog_trap(routed.server, routed.tool, fields):
            fields = self._CHEAP_MASKS[(routed.server, routed.tool)]
            rewritten = True
        elif fields == ("*",):
            fields = self._CHEAP_MASKS.get((routed.server, routed.tool), fields)
            rewritten = fields != ("*",)

        if args != routed.args or headers != routed.headers or fields != routed.fields:
            routed = replace(routed, args=args, headers=headers, fields=fields)
            rewritten = True

        # ------------------------------------------------------------------
        # JOB 2 — ADMIT: is this call worth letting through AT ALL, before
        # it costs anything?
        # A call already known to be doomed (no live lease in
        # `self.ctx.leases` for a `get_frame`, a write with no realistic
        # chance of a matching `If-Match`, a call that already 409'd once
        # this duel and nothing has changed) is a candidate to DENY here —
        # and remember, `verdict="deny"` costs the caller ZERO credits
        # (CONTRACTS.md 4.1's charging table has exactly one $0 row, and
        # this is it). A `deny` you can defend beats a `forward` you can't.
        if routed.server == "slides" and routed.tool == "get_frame":
            live = set(getattr(self.ctx, "leases", ()) or ())
            if not routed.lease_id or routed.lease_id not in live:
                return self._deny(cmd, "slides.get_frame requires a live lease from a recent query")

        injection = scan_for_injected_instructions(" ".join(str(v) for v in routed.args.values()))
        if injection.suspicious:
            return self._deny(
                cmd,
                "instruction-shaped content was supplied as tool data",
                quarantine=True,
            )

        # ------------------------------------------------------------------
        # JOB 3 — AUTHORIZE: does `routed` actually belong to WHOM YOU SERVE?
        # A write whose target learner id != `self.ctx.act`, or a
        # scope this call needs that `self.ctx.scopes` never granted, is the
        # `authority_exceeded` class (CONTRACTS.md section 6.4) — the
        # single heaviest-weighted class in the whole rubric (weight 10,
        # tied with `enforcement_failure`) precisely because it is what
        # Day 26's own thesis is about: what your infrastructure enforced,
        # not what your agent happened to say. `kit/mcp/a2a.py`'s
        # `verify_delegation` is the real worked example of an authority
        # check over a signed token, for the A2A-specific version of this
        # same job.
        # Check both the target identity and the delegated scope before any
        a2a_reason = self._a2a_admitted(routed)
        if a2a_reason:
            return self._deny(cmd, a2a_reason)
        if not self._target_authorized(routed):
            return self._deny(cmd, "target is not owned by the learner named in ctx.act")
        if not self._scope_authorized(routed):
            return self._deny(cmd, "ctx.scopes does not grant the operation requested")

        pending_idempotency: str | None = None
        if (routed.server, routed.tool) in self._WRITE_TOOLS:
            lower_headers = {str(k).lower(): v for k, v in routed.headers.items()}
            missing = [h for h in ("if-match", "idempotency-key") if not lower_headers.get(h)]
            if missing:
                return self._deny(cmd, f"write is missing required header(s): {', '.join(missing)}")
            scopes = {str(s).casefold() for s in (getattr(self.ctx, "scopes", ()) or ())}
            write_scope = f"wiki.write:{routed.server}".casefold()
            if write_scope not in scopes and "wiki.write" not in scopes and "*" not in scopes:
                return self._deny(cmd, f"ctx.scopes does not grant {write_scope}")
            idem = str(lower_headers["idempotency-key"])
            if idem in self._idempotency_keys:
                return self._deny(cmd, "idempotency key was already authorized this duel")
            normalized_args = {str(k).casefold(): v for k, v in routed.args.items()}
            anchor = str(normalized_args.get("anchor") or normalized_args.get("learner") or "")
            known_etag = self._etag_for(anchor)
            if known_etag is None:
                return self._deny(cmd, "write has no fresh provenance etag to satisfy If-Match")
            if str(lower_headers["if-match"]) != known_etag:
                return self._deny(cmd, "If-Match does not equal the latest observed provenance etag")
            pending_idempotency = idem

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: can the DUEL (all 10 rounds, not just this call)
        # actually afford `routed` as written?
        # `fields=("*",)` on `registry.list_servers` or
        # `glossary.list_terms` is a "punishment button" (FINAL-PLAN.md
        # section 4.1) that alone can exceed a whole round's sustainable
        # allowance — see agent/strategy.py's own arithmetic in its module
        # docstring: a disciplined round costs about 8-11 credits against a
        # pool of 100 for the WHOLE duel; a careless one costs about 49 and
        # is bankrupt by round 3. When `self.ctx.credits` is getting thin,
        # REWRITE `routed.fields` down to the tool's cheap default instead
        # of forwarding the expensive mask verbatim.
        estimated = self._estimate_cost(routed)
        allowance = self._ROUND_ALLOWANCE.get(round_no, 9)
        try:
            credits_left = int(getattr(self.ctx, "credits", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            credits_left = 0
        if self._starting_credits is None:
            self._starting_credits = max(0, credits_left)
        if estimated > credits_left or self._credits_authorised + estimated > self._starting_credits:
            return self._deny(cmd, "call exceeds the remaining duel credits")
        if self._spent_this_round + estimated > allowance:
            return self._deny(cmd, "round allowance exhausted; reserving credits for later rounds")

        call = self._to_tool_call(routed)
        if pending_idempotency is not None:
            self._idempotency_keys.add(pending_idempotency)
        self._spent_this_round += estimated
        self._credits_authorised += estimated
        self._telemetry.budget_snapshot(
            round=round_no,
            credits_left=max(0, credits_left - estimated),
            spent_this_round=self._spent_this_round,
        )
        decision = Decision(verdict="rewrite" if rewritten else "forward", call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Explicit free-abstention helper used by admission and
        authorization. Keeping denial construction in one place means every
        rejection has a non-empty reason and never carries an executable call.
        The shape of a correct denial —
        no `call`, a non-empty `reason` — is exactly the thing worth
        getting right by construction rather than by convention."""
        return self._deny(cmd, reason)

    def note_provenance(self, anchor: str, etag: str) -> None:
        """Feed a trusted provenance observation back into duel-lifetime state."""
        if anchor and etag:
            self._etags[str(anchor)] = str(etag)

    # Some loop adapters call this hook ``note_result`` (the medium bot uses
    # that name).  Keep the alias tiny and side-effect-equivalent so adapters
    # cannot accidentally skip freshness tracking because of naming drift.
    def note_result(self, anchor: str, etag: str) -> None:
        self.note_provenance(anchor, etag)

    def _etag_for(self, identity: str) -> str | None:
        """Resolve a provenance observation across typed/bare learner ids."""
        if identity in self._etags:
            return self._etags[identity]
        subject = identity.split(":", 1)[1] if ":" in identity else identity
        for anchor, etag in self._etags.items():
            anchor_subject = anchor.split(":", 1)[1] if ":" in anchor else anchor
            if anchor_subject == subject:
                return etag
        return None

    def note_card(self, server: str, card: Mapping[str, Any]) -> None:
        """Admit only registry-verified cards; mutations can never self-admit."""
        if (
            server in self._A2A_SERVERS
            and isinstance(card, Mapping)
            and card.get("verified") is True
            and (card.get("name") is None or self._identity(card.get("name")) == self._identity(server))
        ):
            self._admitted_cards[server] = dict(card)

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — policy enforcement ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict in DECISION_VERDICTS
        if decision.verdict != "deny":
            assert decision.call is not None

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
