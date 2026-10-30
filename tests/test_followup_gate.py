from __future__ import annotations

from dataclasses import replace

import pytest

from .test_decisions_integration import setup_case


def test_followup_bypasses_cooldown_but_keeps_expiry(tmp_path, monkeypatch):
    _, _, maker, clock, _, state, _ = setup_case(
        tmp_path, monkeypatch, cooldown_sec=180, min_silence_sec=10
    )
    state.record_followup("user1", 985)
    state.last_proactive_at = 985
    state.last_active_at = 991
    assert maker.local_gate(state, force=False) == ""
    clock[0] = 994
    assert maker.local_gate(state, force=False) == ""
    clock[0] = 1106
    assert "冷却中" in maker.local_gate(state, force=False)
    clock[0] = 1002
    state.followup_sender_id = "other"
    assert "冷却中" in maker.local_gate(state, force=False)
    assert maker.local_gate(state, force=False, checking=True) == ""


async def test_followup_only_offers_partner_targets_with_assistant_context(tmp_path, monkeypatch):
    module, models, maker, _, _, state, umo = setup_case(tmp_path, monkeypatch)
    state.record_followup("user1", 985)
    state.last_active_at = 991
    state.recent.appendleft(
        models.MessageRecord(role="assistant", name="Bot", text="What game?", at=985)
    )
    seen = {}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def decide(self, **kwargs):
            seen.update(kwargs)
            return dict(action="wait", confidence=0.9, usage={}, reason_code="selected")

        async def aclose(self):
            pass

    monkeypatch.setattr(module, "DecisionsClient", Client)
    result = await maker.ask_decision_model(umo, state, trigger="message_delay")
    assert result["should_reply"] is False
    assert [m["sender_id"] for m in seen["messages"] if m["eligible"]] == ["user1"]
    assert seen["messages"][0]["role"] == "assistant"
    assert "Continuation window" in seen["interests"]


@pytest.mark.parametrize("at", [980, 985, 1106])
def test_pre_reply_or_expired_target_is_not_followup(tmp_path, monkeypatch, at):
    _, _, _, _, _, state, _ = setup_case(tmp_path, monkeypatch)
    state.record_followup("user1", 985)
    state.reply_targets[-1] = replace(state.reply_targets[-1], at=at)
    state.last_active_at = at
    assert not state.is_followup(1000)
