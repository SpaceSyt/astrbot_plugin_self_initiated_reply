"""Fixed Decisions batches retain their deadline through sustained arrivals."""

from __future__ import annotations

import asyncio
from importlib import import_module

import pytest

from ._support import make_event
from .host_stubs import with_plugin
from .test_message_ingress import UMO, _feed
from .test_session_scheduler import _make_scheduler


def controlled_deadlines(scheduler):
    deadlines = asyncio.Queue()

    async def wait(umo, delay_sec, generation):
        release = asyncio.Event()
        deadlines.put_nowait((delay_sec, release))
        await release.wait()
        return scheduler._should_run() and scheduler._gate.is_current(umo, generation)

    scheduler._wait_initial_delay_and_validate = wait
    return deadlines


async def next_deadline(deadlines):
    return await asyncio.wait_for(deadlines.get(), timeout=1)


def schedule(scheduler, generation):
    scheduler.schedule_delayed_check(
        UMO, delay_sec=30, trigger="message_delay", force=False, generation=generation
    )
    return scheduler._delay_tasks[UMO]


async def test_sustained_arrivals_reuse_first_deadline_and_ignore_minimum_silence(tmp_path):
    _, models, scheduler, _, checks = _make_scheduler(
        tmp_path, {"decision_backend": "decisions", "min_silence_sec": 999}
    )
    deadlines = controlled_deadlines(scheduler)
    generation = scheduler._gate.advance(UMO)
    first = schedule(scheduler, generation)
    delay, release = await next_deadline(deadlines)
    assert delay == 30
    state = scheduler._state_for(UMO)
    for _ in range(20):
        state.last_active_at = models.now_ts()
        scheduler.notify_activity(UMO)
        assert schedule(scheduler, generation) is first
    assert deadlines.empty()
    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert checks == [(UMO, "message_delay", False, generation)]


async def test_arrivals_during_check_open_next_window_and_old_cleanup_preserves_it(tmp_path):
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"decision_backend": "decisions"})
    deadlines = controlled_deadlines(scheduler)
    checking = asyncio.Event()
    finish_first = asyncio.Event()
    calls = []

    async def check(umo, **kwargs):
        calls.append(umo)
        if len(calls) == 1:
            checking.set()
            await finish_first.wait()
        return "silence"

    scheduler._check_session = check
    generation = scheduler._gate.advance(UMO)
    first = schedule(scheduler, generation)
    _, release_first = await next_deadline(deadlines)
    release_first.set()
    await asyncio.wait_for(checking.wait(), timeout=1)
    assert UMO not in scheduler._delay_tasks
    second = schedule(scheduler, generation)
    assert second is not first
    _, release_second = await next_deadline(deadlines)
    assert schedule(scheduler, generation) is second
    finish_first.set()
    await asyncio.wait_for(first, timeout=1)
    assert scheduler._delay_tasks[UMO] is second
    release_second.set()
    await asyncio.wait_for(second, timeout=1)
    assert calls == [UMO, UMO]


def test_direct_call_cancels_old_window_and_next_ordinary_message_starts_fresh(tmp_path):
    async def scenario(plugin, _main):
        deadlines = controlled_deadlines(plugin._scheduler)
        checks = []

        async def check(umo, **kwargs):
            checks.append(umo)
            return "silence"

        plugin._scheduler._check_session = check
        await _feed(plugin, make_event(message_str="First ordinary message"))
        first = plugin._delay_tasks[UMO]
        generation = plugin._gate.current(UMO)
        await next_deadline(deadlines)
        await _feed(plugin, make_event(message_str="Another ordinary message"))
        assert plugin._delay_tasks[UMO] is first
        assert plugin._gate.current(UMO) == generation
        direct = make_event(message_str="@Bot test")
        direct.is_at_or_wake_command = True
        await _feed(plugin, direct)
        await asyncio.gather(first, return_exceptions=True)
        assert UMO not in plugin._delay_tasks
        assert plugin._gate.current(UMO) != generation
        await _feed(plugin, make_event(message_str="New ordinary message"))
        second = plugin._delay_tasks[UMO]
        assert second is not first
        _, release = await next_deadline(deadlines)
        release.set()
        await asyncio.wait_for(second, timeout=1)
        assert checks == [UMO]

    with_plugin(tmp_path, scenario, decision_backend="decisions", message_delay_sec=30)


@pytest.mark.parametrize("operation", ["disable", "remove"])
def test_disabling_or_removing_session_cancels_pending_window(tmp_path, operation):
    async def scenario(plugin, _main):
        deadlines = controlled_deadlines(plugin._scheduler)
        checks = []

        async def check(umo, **kwargs):
            checks.append(umo)
            return "silence"

        plugin._scheduler._check_session = check
        await _feed(plugin, make_event(message_str="Pending ordinary message"))
        pending = plugin._delay_tasks[UMO]
        _, release = await next_deadline(deadlines)
        if operation == "disable":
            package = plugin.__class__.__module__.rsplit(".", 1)[0]
            state_module = import_module(f"{package}.plugin_state")
            await plugin._persist_enabled(False)
            await state_module.apply_enabled_topology(plugin, enabled=False)
        else:
            await plugin._remove_whitelist_session(UMO)
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert UMO not in plugin._delay_tasks
        assert not checks

    with_plugin(tmp_path, scenario, decision_backend="decisions", message_delay_sec=30)
