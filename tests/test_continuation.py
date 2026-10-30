"""Direct replies open follow-up windows only within the opted-in session."""

from types import SimpleNamespace

import pytest

from .host_stubs import FakeEvent, with_plugin

UMO = "fake:group:123"


def reply_event(*, umo=UMO, sent=True, llm=True, reply="草莓和奶油配起来很好吃。"):
    event = FakeEvent(umo=umo, message_str="[At:bot1] 芭菲好吃吗", sender_id="alice")
    event.get_platform_name = lambda: "aiocqhttp"
    event._has_send_oper = sent
    event.set_result(SimpleNamespace(is_llm_result=lambda: llm, get_plain_text=lambda: reply))
    return event


def test_direct_reply_records_context_and_sender_without_proactive_count(tmp_path):
    async def scenario(plugin, main):
        event = reply_event()
        await plugin.on_normal_reply_sent(event)
        state = plugin._state_for(UMO)
        assert state.followup_sender_id == "alice"
        assert state.followup_until > state.followup_reply_at > 0
        assert [(item.role, item.sender_id) for item in state.recent] == [
            ("user", "alice"),
            ("assistant", ""),
        ]
        assert state.recent[0].text == "芭菲好吃吗"
        assert state.recent[1].text == "草莓和奶油配起来很好吃。"
        assert state.daily_count == 0
        assert state.last_proactive_at == 0
        await plugin.on_normal_reply_sent(event)
        assert len(state.recent) == 2

    with_plugin(tmp_path, scenario, decision_backend="decisions")


@pytest.mark.parametrize(
    "case",
    ["failed", "proactive", "other_session", "non_llm", "empty", "ordinary", "other_platform"],
)
def test_nonqualifying_reply_does_not_open_window(tmp_path, case):
    async def scenario(plugin, main):
        event = reply_event()
        if case == "failed":
            event._has_send_oper = False
        elif case == "proactive":
            event.set_extra("self_initiated_reply", True)
        elif case == "other_session":
            event.unified_msg_origin = "fake:group:456"
        elif case == "non_llm":
            event = reply_event(llm=False)
        elif case == "empty":
            event = reply_event(reply="")
        elif case == "ordinary":
            event.message_str = "普通闲聊"
        elif case == "other_platform":
            event.get_platform_name = lambda: "unknown"
        await plugin.on_normal_reply_sent(event)
        assert all(not state.followup_sender_id for state in plugin.sessions.values())
        assert "fake:group:456" not in plugin.sessions

    with_plugin(tmp_path, scenario, decision_backend="decisions")
