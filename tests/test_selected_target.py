"""Selected messages keep their identity across judging, generation and delivery."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from .host_stubs import FakeEvent, install_astrbot_stubs, load_package
from .test_delivery_runner import _make_runner as make_delivery
from .test_generation_runner import _make_runner as make_generation
from .test_pipeline_guards import _reply_decision, _wire_pipeline


def target_for(models, event, *, message_id="-123", at=None):
    return models.ReplyTarget(
        event=event,
        message_id=message_id,
        sender_id=event.get_sender_id(),
        text=event.message_str,
        name=event.get_sender_name(),
        umo=event.unified_msg_origin,
        at=models.now_ts() if at is None else at,
    )


async def test_generation_uses_chosen_event_and_message_not_latest(tmp_path):
    seen = []

    async def hook(event, *_):
        seen.append(event)
        return False

    _, models, runner, runtime, _, _ = make_generation(tmp_path, hook=hook)
    first = FakeEvent(umo="qq:GroupMessage:1", sender_id="alice", message_str="music")
    latest = FakeEvent(umo=first.unified_msg_origin, sender_id="bob", message_str="weather")
    runner._last_events[first.unified_msg_origin] = latest
    target = target_for(models, first)
    state = models.SessionState()
    state.reply_targets.append(target)

    text, _ = await runner.generate(first.unified_msg_origin, state, target=target)

    assert text == "你好呀"
    assert seen == [first]
    assert runtime.build_kwargs[0]["event"] is first
    assert '"text": "music"' in runtime.built_reqs[0].prompt
    assert "主要回应" in runtime.built_reqs[0].prompt
    assert latest.get_extra("self_initiated_reply") is None


async def test_delivery_preserves_negative_quote_id_and_selected_sender(tmp_path):
    _, models, runner, events = make_delivery(
        tmp_path, config={"quote_mode": "model", "mention_mode": "always"}
    )
    first = FakeEvent(umo="qq:GroupMessage:1", sender_id="alice")
    latest = FakeEvent(umo=first.unified_msg_origin, sender_id="bob")
    events[first.unified_msg_origin] = latest
    captured = []

    async def send(result):
        captured.extend(result.chain)

    first.send = send
    target = target_for(models, first)
    outcome = await runner.send_reply(first.unified_msg_origin, "hello", target=target, quote=True)

    assert outcome.delivered
    assert captured[0].qq == "alice"
    assert captured[1].id == "-123"
    assert latest.sent_texts == []


@pytest.mark.parametrize("status", ["delivered", "unknown", "failed_before_submit"])
async def test_only_successful_selected_reply_opens_followup(tmp_path, status):
    _, models, runner, _ = make_delivery(
        tmp_path, sender_status=status, config={"decision_backend": "decisions"}
    )
    event = FakeEvent(umo="qq:GroupMessage:1", sender_id="alice")
    target = target_for(models, event)
    state = models.SessionState()
    state.reply_targets.append(target)
    await runner.deliver_reply(
        event.unified_msg_origin,
        state,
        "hello",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=None,
        force=False,
        trigger="message_delay",
        target=target,
    )
    assert bool(state.followup_sender_id) == (status == "delivered")


@pytest.mark.parametrize("invalid", ["other_session", "expired"])
async def test_invalid_target_never_falls_back_to_latest_event(tmp_path, invalid):
    _, models, runner, events = make_delivery(tmp_path)
    current = FakeEvent(umo="qq:GroupMessage:1")
    events[current.unified_msg_origin] = current
    chosen = FakeEvent(
        umo="qq:GroupMessage:2" if invalid == "other_session" else current.unified_msg_origin
    )
    target = target_for(models, chosen)
    if invalid == "expired":
        target = replace(target, at=models.now_ts() - 601)
    outcome = await runner.send_reply(current.unified_msg_origin, "hello", target=target)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert chosen.sent_texts == current.sent_texts == []


async def test_target_removed_during_hook_is_not_sent(tmp_path):
    state = None

    async def hook(*_):
        state.reply_targets.clear()

    _, models, runner, _ = make_delivery(tmp_path, hook=hook)
    event = FakeEvent(umo="qq:GroupMessage:1")
    target = target_for(models, event)
    state = models.SessionState()
    state.reply_targets.append(target)
    result = await runner.send_reply(
        event.unified_msg_origin, "hello", target=target, target_state=state
    )
    assert result.status is models.SendStatus.SUPPRESSED
    assert event.sent_texts == []


async def test_generation_hook_cannot_send_after_target_invalidation(tmp_path):
    state = None

    async def hook(event, *_):
        state.reply_targets.clear()
        await event.send(SimpleNamespace(type="tool_direct_result", text="tool reply"))
        await event.send(SimpleNamespace(type="plain", text="ordinary reply"))
        return False

    _, models, runner, _, _, _ = make_generation(tmp_path, hook=hook)
    event = FakeEvent(umo="qq:GroupMessage:1")
    target = target_for(models, event)
    state = models.SessionState()
    state.reply_targets.append(target)
    text, _ = await runner.generate(event.unified_msg_origin, state, target=target)
    assert text == ""
    assert event.sent_texts == []


async def test_pipeline_carries_same_target_to_generation_and_delivery():
    install_astrbot_stubs()
    umo = "qq:GroupMessage:1"
    decision = _reply_decision()
    seen = []

    class Generation:
        async def generate(self, *_args, target, ledger, **_kwargs):
            seen.append(target)
            return "reply", ledger

    class Delivery:
        async def deliver_reply(self, *_args, target, **_kwargs):
            seen.append(target)
            return "sent"

    pipeline, models = _wire_pipeline(
        "selected_target_pipeline",
        umo=umo,
        decision=decision,
        generation=Generation(),
        delivery=Delivery(),
    )
    target = target_for(models, FakeEvent(umo=umo))
    pipeline._state_for(umo).reply_targets.append(target)

    async def decide(*_args, **_kwargs):
        return {"should_reply": True, "reason": "interest", "target": target}

    decision.decide = decide
    assert await pipeline.check_session_locked(umo, trigger="message_delay", force=False) == "sent"
    assert seen == [target, target]


def test_candidate_capture_is_bounded_and_not_persisted():
    install_astrbot_stubs()
    package = "selected_target_capture"
    state_mod = load_package(package, "plugin_state")
    models = load_package(package, "models")
    storage = load_package(package, "storage")
    umo = "qq:GroupMessage:1"
    state = models.SessionState()
    plugin = SimpleNamespace(
        _state_for=lambda _: state, settings=SimpleNamespace(decision_backend="decisions")
    )
    for i in range(35):
        event = FakeEvent(umo=umo)
        event.message_obj = SimpleNamespace(message_id=-(i + 1))
        state_mod.append_recent_user_message(
            plugin,
            event,
            umo=umo,
            clean_text="music",
            active_at=100 + i,
            capture_target=True,
        )
    assert len(state.reply_targets) == 30
    assert state.reply_targets[-1].message_id == "-35"
    payload = storage.build_sessions_payload({umo: state}, {umo}, 30)
    assert "reply_targets" not in str(payload)
    assert "FakeEvent" not in str(payload)


def test_direct_call_discards_previous_candidates(tmp_path):
    from ._support import make_event
    from .host_stubs import with_plugin
    from .test_message_ingress import UMO, _feed

    async def scenario(plugin, _main):
        first = make_event(message_str="A topic of interest")
        first.message_obj = SimpleNamespace(message_id=-22)
        await _feed(plugin, first)
        assert plugin._state_for(UMO).reply_targets
        direct = make_event(message_str="@Bot respond")
        direct.is_at_or_wake_command = True
        await _feed(plugin, direct)
        assert not plugin._state_for(UMO).reply_targets

    with_plugin(tmp_path, scenario, decision_backend="decisions")


@pytest.mark.parametrize("backend,enabled", [("chat", True), ("decisions", False)])
def test_inactive_backend_or_disabled_plugin_does_not_capture(tmp_path, backend, enabled):
    from ._support import make_event
    from .host_stubs import with_plugin
    from .test_message_ingress import UMO, _feed

    async def scenario(plugin, _main):
        event = make_event(message_str="A topic of interest")
        event.message_obj = SimpleNamespace(message_id=-23)
        await _feed(plugin, event)
        assert not plugin._state_for(UMO).reply_targets

    with_plugin(tmp_path, scenario, decision_backend=backend, enabled=enabled)


def test_periodic_cleanup_releases_inactive_candidate_events(tmp_path):
    from ._support import make_event
    from .host_stubs import with_plugin
    from .test_message_ingress import UMO, _feed

    async def scenario(plugin, _main):
        event = make_event(message_str="A topic of interest")
        event.message_obj = SimpleNamespace(message_id=-24)
        await _feed(plugin, event)
        state = plugin._state_for(UMO)
        target = state.reply_targets[0]
        state.reply_targets[0] = replace(target, at=target.at - 601)
        await plugin._scheduler.run_image_cleanup()
        assert not state.reply_targets
        # Candidate expiry must not erase persisted conversation records.
        assert state.recent[-1].text == "A topic of interest"

    with_plugin(tmp_path, scenario, decision_backend="decisions")


@pytest.mark.parametrize("operation", ["clear_event", "reset_all", "remove_whitelist"])
def test_lifecycle_cleanup_releases_candidate_events(tmp_path, operation):
    from ._support import make_event
    from .host_stubs import with_plugin
    from .test_message_ingress import UMO, _feed

    async def scenario(plugin, _main):
        event = make_event(message_str="A topic of interest")
        event.message_obj = SimpleNamespace(message_id=-25)
        await _feed(plugin, event)
        state = plugin._state_for(UMO)
        state.record_followup(event.get_sender_id(), main_time := state.last_active_at)
        assert state.followup_until == main_time + 120
        assert state.reply_targets
        if operation == "clear_event":
            plugin._coordinator.clear_event(UMO)
        elif operation == "reset_all":
            plugin._coordinator.reset_all()
        else:
            await plugin._remove_whitelist_session(UMO)
        assert not state.reply_targets
        assert not state.followup_sender_id

    with_plugin(tmp_path, scenario, decision_backend="decisions")
