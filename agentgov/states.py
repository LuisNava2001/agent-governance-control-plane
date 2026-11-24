"""The task state machine: an explicit table of (state, event) -> allowed target states.

Commands never set a state directly; they ask `transition` to move along a listed edge. Guards (evidence,
approvals, fingerprints) decide which listed target applies, never whether an unlisted one is reachable.
"""

from __future__ import annotations

from enum import StrEnum

from .errors import ErrorCode, GovernanceError


class State(StrEnum):
    CREATED = "CREATED"
    CLASSIFIED = "CLASSIFIED"
    PLANNED = "PLANNED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


S = State
TERMINAL = frozenset({S.COMPLETED, S.BLOCKED})

# States from which the requester may amend the facts (before any execution has started).
AMENDABLE = (S.CLASSIFIED, S.PLANNED, S.EVIDENCE_PENDING, S.READY_FOR_REVIEW, S.APPROVAL_REQUIRED, S.AUTHORIZED)

TRANSITIONS: dict[tuple[State, str], frozenset[State]] = {
    (S.CREATED, "classify"): frozenset({S.CLASSIFIED, S.BLOCKED}),
    (S.CLASSIFIED, "plan"): frozenset({S.PLANNED}),
    (S.PLANNED, "review"): frozenset({S.READY_FOR_REVIEW, S.EVIDENCE_PENDING}),
    (S.EVIDENCE_PENDING, "review"): frozenset({S.READY_FOR_REVIEW, S.EVIDENCE_PENDING}),
    (S.READY_FOR_REVIEW, "request_authorization"): frozenset({S.AUTHORIZED, S.APPROVAL_REQUIRED}),
    (S.APPROVAL_REQUIRED, "approve"): frozenset({S.APPROVAL_REQUIRED, S.AUTHORIZED}),
    (S.APPROVAL_REQUIRED, "reject"): frozenset({S.BLOCKED}),
    (S.AUTHORIZED, "start_execution"): frozenset({S.EXECUTING}),
    (S.EXECUTING, "verify"): frozenset({S.VERIFIED, S.BLOCKED}),
    (S.VERIFIED, "complete"): frozenset({S.COMPLETED}),
    **{(state, "amend"): frozenset({S.CLASSIFIED, S.BLOCKED}) for state in AMENDABLE},
    **{(state, "block"): frozenset({S.BLOCKED}) for state in State if state not in TERMINAL},
}

EVENTS = sorted({event for _, event in TRANSITIONS})


def allowed_events(state: State) -> list[str]:
    return sorted(event for (source, event) in TRANSITIONS if source == state)


def transition(state: State, event: str, target: State) -> State:
    allowed = TRANSITIONS.get((state, event))
    if allowed is None:
        raise GovernanceError(ErrorCode.INVALID_TRANSITION, f"'{event}' is not allowed in state {state}",
                              {"state": state, "event": event, "allowed_events": allowed_events(state)})
    if target not in allowed:
        raise GovernanceError(ErrorCode.INVALID_TRANSITION, f"'{event}' cannot move {state} to {target}",
                              {"state": state, "event": event, "allowed_targets": sorted(allowed)})
    return target
