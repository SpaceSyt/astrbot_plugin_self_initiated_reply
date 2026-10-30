"""Remember delivered direct replies as context for follow-up decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

from .models import MessageRecord, now_ts
from .utils import (
    clean_chat_text,
    event_extra,
    event_sender_id,
    event_sender_name,
    event_text,
    event_umo,
    is_explicit_direct_call,
    is_self_message,
    session_is_private,
    session_whitelisted,
)

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin

_RECORDED_KEY = "selfreply_normal_reply_recorded"


def record_normal_reply(plugin: SelfInitiatedReplyPlugin, event: AstrMessageEvent) -> None:
    """Open a follow-up window only after a direct LLM reply completes sending."""
    if (
        plugin._stopping
        or not plugin.runtime_enabled
        or plugin.settings.decision_backend != "decisions"
        or event_extra(event, "self_initiated_reply", False)
        or event_extra(event, _RECORDED_KEY, False)
        or is_self_message(event)
    ):
        return
    umo = event_umo(event)
    if not session_whitelisted(umo, plugin.settings.whitelist):
        return
    if session_is_private(umo) and not plugin.settings.enabled_private_sessions:
        return
    text = event_text(event)
    sender_id = event_sender_id(event)
    if (
        not sender_id
        or sender_id in plugin.settings.ignored_sender_ids
        or not is_explicit_direct_call(event, text)
    ):
        return
    # aiocqhttp sets this flag after its send coroutine returns successfully.
    # The host's after-send hook alone also runs after caught send errors.
    platform_name = getattr(event, "get_platform_name", None)
    if (
        not callable(platform_name)
        or platform_name() != "aiocqhttp"
        or not getattr(event, "_has_send_oper", False)
    ):
        return
    result = event.get_result()
    if result is None or not result.is_llm_result():
        return
    reply = result.get_plain_text().strip()
    if not reply:
        return
    state = plugin._state_for(umo)
    at = now_ts()
    state.recent.extend(
        (
            MessageRecord(
                role="user",
                name=event_sender_name(event),
                sender_id=sender_id,
                text=clean_chat_text(text),
                at=at,
            ),
            MessageRecord(role="assistant", name="Bot", text=reply, at=at),
        )
    )
    state.record_followup(sender_id, at)
    event.set_extra(_RECORDED_KEY, True)
