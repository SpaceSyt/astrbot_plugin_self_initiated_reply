"""The API branch selects only captured events and does not call host LLM hooks."""

from __future__ import annotations

import asyncio

import pytest

from .host_stubs import FakeEvent
from .test_decision_maker import _make_decision


def setup_case(tmp_path, monkeypatch, **config):
    module, models, maker, clock, calls = _make_decision(
        tmp_path,
        config={
            "decision_backend": "decisions",
            "decisions_interests": "音乐和游戏，自然接话",
            **config,
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    state = models.SessionState()
    umo = "fake:GroupMessage:123"
    for index, text in enumerate(("最近在听新专辑", "今天下雨了")):
        event = FakeEvent(umo=umo, sender_id=f"user{index}", message_str=text)
        target = models.ReplyTarget(
            event=event,
            message_id=str(-123 - index),
            sender_id=f"user{index}",
            text=text,
            name=f"user{index}",
            umo=umo,
            at=990.0 + index,
        )
        state.reply_targets.append(target)
        state.recent.append(
            models.MessageRecord(
                role="user",
                text=text,
                name=target.name,
                sender_id=target.sender_id,
                at=target.at,
            )
        )
    return module, models, maker, clock, calls, state, umo


async def test_decisions_selects_earlier_message_and_closes_client(tmp_path, monkeypatch):
    module, _, maker, _, calls, state, umo = setup_case(tmp_path, monkeypatch)
    seen = {}

    class Client:
        def __init__(self, *args, **kwargs):
            seen["config"] = kwargs

        async def decide(self, **kwargs):
            seen["request"] = kwargs
            return {
                "action": "reply",
                "target_id": "m0",
                "confidence": 0.9,
                "usage": {"input_tokens": 20},
                "reason_code": "selected",
            }

        async def aclose(self):
            seen["closed"] = True

    monkeypatch.setattr(module, "DecisionsClient", Client)
    result = await maker.decide(umo, state, trigger="message_delay", force=False)
    assert result["target"] is state.reply_targets[0]
    assert result["target"].message_id == "-123"
    assert result["usage"] == {"input_tokens": 20}
    assert result["quote"] is True
    assert [m["id"] for m in seen["request"]["messages"]] == ["m0", "m1"]
    assert seen["closed"]
    assert all(calls[key] == 0 for key in ("model", "provider", "history", "image"))


@pytest.mark.parametrize("change", ["clear", "expire", "other_session"])
async def test_target_invalidated_while_judging_stays_silent(tmp_path, monkeypatch, change):
    module, _, maker, clock, _, state, umo = setup_case(tmp_path, monkeypatch)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def decide(self, **kwargs):
            if change == "clear":
                state.reply_targets.clear()
            elif change == "expire":
                clock[0] += 700
            else:
                state.reply_targets[0].event.unified_msg_origin = "fake:GroupMessage:other"
            return {
                "action": "reply",
                "target_id": "m0",
                "confidence": 0.9,
                "usage": {},
                "reason_code": "selected",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(module, "DecisionsClient", Client)
    result = await maker.ask_decision_model(umo, state, trigger="message_delay")
    assert result["should_reply"] is False
    assert result["reason"] == "decisions_stale_target"


@pytest.mark.parametrize("missing", ["key", "interests", "disabled", "consumed"])
async def test_no_api_when_not_ready(tmp_path, monkeypatch, missing):
    module, _, maker, _, _, state, umo = setup_case(tmp_path, monkeypatch)
    if missing == "key":
        monkeypatch.delenv("OPENAI_API_KEY")
    elif missing == "interests":
        maker.settings.decisions_interests = ""
    elif missing == "disabled":
        maker.settings.decision_model_enabled = False
    else:
        state.last_proactive_observed_at = 999

    def unexpected(*args, **kwargs):
        pytest.fail("Should not create an API client")

    monkeypatch.setattr(module, "DecisionsClient", unexpected)
    result = await maker.ask_decision_model(umo, state, trigger="patrol")
    assert result["should_reply"] is False


async def test_cancellation_closes_client_and_propagates(tmp_path, monkeypatch):
    module, _, maker, _, _, state, umo = setup_case(tmp_path, monkeypatch)
    closed = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def decide(self, **kwargs):
            raise asyncio.CancelledError

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(module, "DecisionsClient", Client)
    with pytest.raises(asyncio.CancelledError):
        await maker.ask_decision_model(umo, state, trigger="message_delay")
    assert closed == [True]
