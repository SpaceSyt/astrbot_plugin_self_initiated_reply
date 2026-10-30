from ._support import make_event
from .host_stubs import with_plugin
from .test_message_ingress import UMO, _feed


def test_silence_consumes_only_current_batch_and_preserves_new_arrival(tmp_path):
    async def scenario(plugin, main):
        event = make_event(message_str="first batch")
        await _feed(plugin, event)
        state = plugin._state_for(UMO)
        observed = state.last_active_at

        async def decide(*args, **kwargs):
            state.last_active_at = observed + 1
            return "判断不回复：decisions_wait_selected"

        plugin._decision.decide = decide
        result = await plugin._pipeline.check_session(
            UMO,
            trigger="message_delay",
            force=False,
            expected_generation=plugin._gate.current(UMO),
        )
        assert "decisions_wait_selected" in result
        assert state.last_proactive_observed_at == observed
        assert state.last_active_at == observed + 1
        assert state.daily_count == 0

    with_plugin(
        tmp_path,
        scenario,
        decision_backend="decisions",
        enabled_message_trigger=False,
        cooldown_sec=180,
    )


def test_cooldown_allows_decision_but_prevents_generation(tmp_path):
    async def scenario(plugin, main):
        event = make_event(message_str="fresh topic")
        await _feed(plugin, event)
        state = plugin._state_for(UMO)
        state.last_proactive_at = state.last_active_at
        calls = []

        async def decide(*args, **kwargs):
            calls.append("decision")
            return dict(should_reply=True, reason="decisions_reply_selected")

        async def generate(*args, **kwargs):
            raise AssertionError("Cooldown must prevent reply generation")

        plugin._decision.decide = decide
        plugin._generation.generate = generate
        result = await plugin._pipeline.check_session(
            UMO,
            trigger="message_delay",
            force=False,
            expected_generation=plugin._gate.current(UMO),
        )
        assert calls == ["decision"]
        assert "冷却中" in result
        assert state.last_proactive_observed_at == state.last_active_at

    with_plugin(
        tmp_path,
        scenario,
        decision_backend="decisions",
        enabled_message_trigger=False,
        cooldown_sec=180,
    )
