"""Worker cancellation must not turn cancellation cleanup into a busy loop."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROBE = """
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from tests.host_stubs import install_astrbot_stubs, load_package

install_astrbot_stubs()
package = 'cancelled_worker_probe'
models = load_package(package, 'models')
storage = load_package(package, 'storage')
plugin_state = load_package(package, 'plugin_state')

async def main():
    started = asyncio.Event()
    release = asyncio.Event()
    workers = []

    async def fake_to_thread(func, *args, **kwargs):
        if func is plugin_state.sessions_payload_matches:
            return False
        workers.append(asyncio.current_task())
        started.set()
        await release.wait()
        return True

    asyncio.to_thread = fake_to_thread
    settings = models.Settings.from_config({})
    if sys.argv[1] == 'config':
        coroutine = storage.apersist_settings_config(Path('unused.json'), {}, settings)
    else:
        plugin = SimpleNamespace(
            _save_lock=asyncio.Lock(), _abandon_disk_writes=False,
            _terminated=False, _spared_critical_tasks=set(), sessions={},
            settings=settings, _storage_path=Path('unused.json'),
        )
        coroutine = plugin_state.save_storage(plugin)
    outer = asyncio.create_task(coroutine)
    await asyncio.wait_for(started.wait(), timeout=1)
    worker = workers[0]
    if sys.argv[2] == 'worker':
        worker.cancel()
    else:
        outer.cancel()
        await asyncio.sleep(0)
        outer.cancel()
        await asyncio.sleep(0)
        assert not worker.cancelled()
        assert not outer.done()
        release.set()
    try:
        await asyncio.wait_for(outer, timeout=1)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('Cancellation must propagate')
    assert outer.cancelled()
    if sys.argv[2] == 'parent':
        assert worker.done() and not worker.cancelled()

asyncio.run(main())
"""


@pytest.mark.parametrize("operation", ["config", "state"])
@pytest.mark.parametrize("cancelled", ["worker", "parent"])
def test_cancelled_writer_converges_in_bounded_process(operation, cancelled):
    # A broken shield loop starves asyncio's own timeout, so enforce a deadline
    # outside the interpreter instead of risking a hung pytest worker.
    result = subprocess.run(
        [sys.executable, "-c", PROBE, operation, cancelled],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
