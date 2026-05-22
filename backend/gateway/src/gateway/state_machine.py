"""Device lifecycle state machine.

Five states make up the device lifecycle. The same definitions are
referenced by the gateway (to enforce behavior on each stream) and by
the admin service (to validate user-driven transitions).

  active           - normal device; streams audio + health + location
  lost             - location-only; gateway accepts handshake/health/
                     location but never publishes audio frames
  revoked          - gateway refuses connections
  wipe_requested   - gateway will send a wipe command on next connect
  wipe_sent        - terminal; gateway refuses further connections

Transitions allowed FROM the admin UI / CLI (i.e. user-initiated):

  active           -> {lost, revoked, wipe_requested}
  lost             -> {active, revoked, wipe_requested}
  revoked          -> {active}                # requires extra confirmation
  wipe_requested   -> {}                      # admin can no longer change
  wipe_sent        -> {}                      # terminal

Transitions allowed from the gateway (internal only):

  wipe_requested   -> wipe_sent               # after sending the wipe command
"""

from __future__ import annotations

from typing import Final

# Canonical state strings stored in Firestore `devices/{id}.state`.
STATE_ACTIVE: Final = "active"
STATE_LOST: Final = "lost"
STATE_REVOKED: Final = "revoked"
STATE_WIPE_REQUESTED: Final = "wipe_requested"
STATE_WIPE_SENT: Final = "wipe_sent"

ALL_STATES: Final = frozenset(
    {STATE_ACTIVE, STATE_LOST, STATE_REVOKED, STATE_WIPE_REQUESTED, STATE_WIPE_SENT}
)

# Admin-driven transitions. wipe_requested -> wipe_sent is intentionally
# NOT in this map because only the gateway is allowed to make that flip.
_ADMIN_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATE_ACTIVE: frozenset({STATE_LOST, STATE_REVOKED, STATE_WIPE_REQUESTED}),
    STATE_LOST: frozenset({STATE_ACTIVE, STATE_REVOKED, STATE_WIPE_REQUESTED}),
    STATE_REVOKED: frozenset({STATE_ACTIVE}),
    STATE_WIPE_REQUESTED: frozenset(),
    STATE_WIPE_SENT: frozenset(),
}

# Transitions that require an extra confirmation in the admin UI/CLI.
# These are STILL allowed transitions in _ADMIN_TRANSITIONS — this set
# just adds a second-look prompt.
EXTRA_CONFIRM: Final = frozenset(
    {
        (STATE_REVOKED, STATE_ACTIVE),
        (STATE_ACTIVE, STATE_WIPE_REQUESTED),
        (STATE_LOST, STATE_WIPE_REQUESTED),
    }
)

# States in which a device may open a gRPC session at all.
CONNECTABLE_STATES: Final = frozenset(
    {STATE_ACTIVE, STATE_LOST, STATE_WIPE_REQUESTED}
)

# States in which the gateway should publish audio frames to Redis.
PUBLISH_AUDIO_STATES: Final = frozenset({STATE_ACTIVE})

# Terminal states that the admin UI must never let the user move out of.
TERMINAL_STATES: Final = frozenset({STATE_WIPE_SENT})


class InvalidTransitionError(ValueError):
    """Raised when an admin requests a state change not in the allow-list."""


def normalize(state: str | None) -> str:
    """Coerce a stored string to a canonical state.

    Older device docs may carry the legacy 'status' field with values
    'active', 'revoked', or 'offline'. We treat 'offline' as a connection
    status, not a lifecycle state, and map missing/unknown to active so
    we don't lock out devices during a migration.
    """
    if not state:
        return STATE_ACTIVE
    s = state.strip().lower()
    if s in ALL_STATES:
        return s
    if s == "offline":
        return STATE_ACTIVE
    return STATE_ACTIVE


def allowed_next_states(current: str) -> frozenset[str]:
    """The set of states an admin may transition `current` to."""
    return _ADMIN_TRANSITIONS.get(normalize(current), frozenset())


def requires_extra_confirmation(current: str, target: str) -> bool:
    return (normalize(current), normalize(target)) in EXTRA_CONFIRM


def validate_admin_transition(current: str, target: str) -> None:
    """Raise InvalidTransitionError if (current -> target) is not allowed."""
    cur = normalize(current)
    tgt = normalize(target)
    if tgt not in ALL_STATES:
        raise InvalidTransitionError(f"unknown target state: {target!r}")
    if tgt == STATE_WIPE_SENT:
        raise InvalidTransitionError("wipe_sent is gateway-internal; admin cannot set it")
    if tgt not in _ADMIN_TRANSITIONS.get(cur, frozenset()):
        raise InvalidTransitionError(
            f"transition not allowed: {cur} -> {tgt}"
        )


def is_connectable(state: str) -> bool:
    return normalize(state) in CONNECTABLE_STATES


def may_publish_audio(state: str) -> bool:
    return normalize(state) in PUBLISH_AUDIO_STATES


def is_terminal(state: str) -> bool:
    return normalize(state) in TERMINAL_STATES
