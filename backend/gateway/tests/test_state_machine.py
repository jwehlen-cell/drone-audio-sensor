"""Transition rules for the device lifecycle.

The full matrix the gateway and admin UI must agree on is small enough
to enumerate; this test fails loudly if anyone tweaks one side without
the other.
"""

from __future__ import annotations

import pytest

from gateway.state_machine import (
    ALL_STATES,
    InvalidTransitionError,
    STATE_ACTIVE,
    STATE_LOST,
    STATE_REVOKED,
    STATE_WIPE_REQUESTED,
    STATE_WIPE_SENT,
    allowed_next_states,
    is_connectable,
    may_publish_audio,
    normalize,
    requires_extra_confirmation,
    validate_admin_transition,
)


EXPECTED_ADMIN_MATRIX = {
    STATE_ACTIVE: {STATE_LOST, STATE_REVOKED, STATE_WIPE_REQUESTED},
    STATE_LOST: {STATE_ACTIVE, STATE_REVOKED, STATE_WIPE_REQUESTED},
    STATE_REVOKED: {STATE_ACTIVE},
    STATE_WIPE_REQUESTED: set(),
    STATE_WIPE_SENT: set(),
}


@pytest.mark.parametrize("state", sorted(ALL_STATES))
def test_allowed_next_states_match_matrix(state: str) -> None:
    assert set(allowed_next_states(state)) == EXPECTED_ADMIN_MATRIX[state]


def test_validate_admin_transition_accepts_allowed() -> None:
    for current, targets in EXPECTED_ADMIN_MATRIX.items():
        for target in targets:
            # Should not raise.
            validate_admin_transition(current, target)


def test_validate_admin_transition_rejects_disallowed() -> None:
    for current, targets in EXPECTED_ADMIN_MATRIX.items():
        disallowed = ALL_STATES - targets - {current}
        for target in disallowed:
            with pytest.raises(InvalidTransitionError):
                validate_admin_transition(current, target)


def test_wipe_sent_is_not_admin_writable() -> None:
    for current in ALL_STATES:
        with pytest.raises(InvalidTransitionError):
            validate_admin_transition(current, STATE_WIPE_SENT)


def test_extra_confirmation_required_for_dangerous_moves() -> None:
    assert requires_extra_confirmation(STATE_REVOKED, STATE_ACTIVE)
    assert requires_extra_confirmation(STATE_ACTIVE, STATE_WIPE_REQUESTED)
    assert requires_extra_confirmation(STATE_LOST, STATE_WIPE_REQUESTED)
    # Mundane moves do not.
    assert not requires_extra_confirmation(STATE_ACTIVE, STATE_LOST)
    assert not requires_extra_confirmation(STATE_LOST, STATE_ACTIVE)


def test_connectable_excludes_revoked_and_wipe_sent() -> None:
    assert is_connectable(STATE_ACTIVE)
    assert is_connectable(STATE_LOST)
    assert is_connectable(STATE_WIPE_REQUESTED)  # one last connection to receive wipe
    assert not is_connectable(STATE_REVOKED)
    assert not is_connectable(STATE_WIPE_SENT)


def test_audio_publishes_only_in_active() -> None:
    assert may_publish_audio(STATE_ACTIVE)
    for s in ALL_STATES - {STATE_ACTIVE}:
        assert not may_publish_audio(s)


def test_normalize_handles_legacy_and_unknown_values() -> None:
    assert normalize(None) == STATE_ACTIVE
    assert normalize("") == STATE_ACTIVE
    assert normalize("offline") == STATE_ACTIVE  # legacy connection-status word
    assert normalize("ACTIVE") == STATE_ACTIVE
    assert normalize("  lost  ") == STATE_LOST
    assert normalize("nonsense") == STATE_ACTIVE
