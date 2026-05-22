"""Mirror of backend/gateway state_machine.py, vendored to keep services
independently deployable. Both copies share the same rule set; if you
change one, change the other."""

from __future__ import annotations

from typing import Final

STATE_SETUP_PENDING: Final = "setup_pending"
STATE_ACTIVE: Final = "active"
STATE_LOST: Final = "lost"
STATE_REVOKED: Final = "revoked"
STATE_WIPE_REQUESTED: Final = "wipe_requested"
STATE_WIPE_SENT: Final = "wipe_sent"

ALL_STATES: Final = frozenset(
    {
        STATE_SETUP_PENDING,
        STATE_ACTIVE,
        STATE_LOST,
        STATE_REVOKED,
        STATE_WIPE_REQUESTED,
        STATE_WIPE_SENT,
    }
)

_ADMIN_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATE_SETUP_PENDING: frozenset({STATE_ACTIVE, STATE_REVOKED, STATE_WIPE_REQUESTED}),
    STATE_ACTIVE: frozenset({STATE_LOST, STATE_REVOKED, STATE_WIPE_REQUESTED}),
    STATE_LOST: frozenset({STATE_ACTIVE, STATE_REVOKED, STATE_WIPE_REQUESTED}),
    STATE_REVOKED: frozenset({STATE_ACTIVE}),
    STATE_WIPE_REQUESTED: frozenset(),
    STATE_WIPE_SENT: frozenset(),
}

EXTRA_CONFIRM: Final = frozenset(
    {
        (STATE_REVOKED, STATE_ACTIVE),
        (STATE_ACTIVE, STATE_WIPE_REQUESTED),
        (STATE_LOST, STATE_WIPE_REQUESTED),
        (STATE_SETUP_PENDING, STATE_WIPE_REQUESTED),
    }
)


class InvalidTransitionError(ValueError):
    pass


def normalize(state: str | None) -> str:
    if not state:
        return STATE_ACTIVE
    s = state.strip().lower()
    if s in ALL_STATES:
        return s
    if s == "offline":
        return STATE_ACTIVE
    return STATE_ACTIVE


def allowed_next_states(current: str) -> frozenset[str]:
    return _ADMIN_TRANSITIONS.get(normalize(current), frozenset())


def requires_extra_confirmation(current: str, target: str) -> bool:
    return (normalize(current), normalize(target)) in EXTRA_CONFIRM


def validate_admin_transition(current: str, target: str) -> None:
    cur = normalize(current)
    tgt = normalize(target)
    if tgt not in ALL_STATES:
        raise InvalidTransitionError(f"unknown target state: {target!r}")
    if tgt == STATE_WIPE_SENT:
        raise InvalidTransitionError("wipe_sent is gateway-internal; admin cannot set it")
    if tgt not in _ADMIN_TRANSITIONS.get(cur, frozenset()):
        raise InvalidTransitionError(f"transition not allowed: {cur} -> {tgt}")
