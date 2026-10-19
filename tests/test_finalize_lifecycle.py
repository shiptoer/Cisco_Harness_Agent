"""Finalize closes staging. No model/API calls."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harness_agent.harness import server


@pytest.fixture
def state(tmp_path):
    state = SimpleNamespace(
        terminated_path=tmp_path / "TERMINATED",
        finalized_path=tmp_path / "finalized.json",
        staged_path=tmp_path / "staged_ops.jsonl",
        trace=Mock(),
        staged=Mock(return_value=[]),
    )
    state.ensure_alive = lambda: server.RunState.ensure_alive(state)
    return state


def test_first_finalize_succeeds(state):
    result = server._finalize(state, {})

    assert result["finalized"] is True
    assert result["staged"] == 0
    assert state.finalized_path.exists()
    state.trace.write.assert_called_once_with("finalized", staged=0)


def test_repeated_finalize_preserves_marker_and_trace(state):
    server._finalize(state, {})
    original = state.finalized_path.read_bytes()
    state.trace.reset_mock()
    state.staged.reset_mock()

    with pytest.raises(server.ToolFailure, match="already finalized"):
        server._finalize(state, {})

    assert state.finalized_path.read_bytes() == original
    state.staged.assert_not_called()
    state.trace.write.assert_not_called()


def test_stage_after_finalize_stops_before_gate(state, monkeypatch):
    server._finalize(state, {})
    state.staged_path.write_text("", encoding="utf-8")
    original_marker = state.finalized_path.read_bytes()
    original_staged = state.staged_path.read_bytes()

    evaluate = Mock(side_effect=AssertionError("Gate must not run after finalize"))
    monkeypatch.setattr(server.gate, "evaluate", evaluate)

    with pytest.raises(server.ToolFailure, match="already finalized"):
        server._stage(
            state,
            "create_vlan",
            {"device": "SW1", "vlan_id": 30, "name": "FINANCE"},
        )

    evaluate.assert_not_called()
    assert state.staged_path.read_bytes() == original_staged
    assert state.finalized_path.read_bytes() == original_marker


def test_terminated_session_cannot_finalize(state):
    state.terminated_path.write_text("rejected", encoding="utf-8")

    with pytest.raises(server.ToolFailure, match="Session terminated"):
        server._finalize(state, {})

    assert not state.finalized_path.exists()
    state.staged.assert_not_called()
