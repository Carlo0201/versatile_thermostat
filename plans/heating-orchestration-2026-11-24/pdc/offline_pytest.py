"""Run pure single-threaded tests where Windows denies even socketpair.

No Home Assistant runtime, network, cross-thread wakeups or real device I/O is
supported. This runner is deliberately separate from production and CI tests.
"""

import asyncio
import os
import runpy
import selectors
import sys
import time

import pytest


class NoIOSelector(selectors.BaseSelector):
    def register(self, *args, **kwargs):
        raise RuntimeError("offline tests cannot register I/O")

    def unregister(self, *args, **kwargs):
        raise RuntimeError("offline tests cannot unregister I/O")

    def select(self, timeout=None):
        if timeout and timeout > 0:
            time.sleep(min(timeout, 0.01))
        return []

    def get_map(self):
        return {}


class OfflineLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(NoIOSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        pass


class OfflinePolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return OfflineLoop()


if __name__ == "__main__":
    asyncio.set_event_loop_policy(OfflinePolicy())
    os.environ["VTHERM_OFFLINE_TEST_RUNNER"] = os.path.abspath(__file__)
    if len(sys.argv) > 2 and sys.argv[1] == "--script":
        script = sys.argv[2]
        sys.argv = sys.argv[2:]
        runpy.run_path(script, run_name="__main__")
        raise SystemExit(0)
    raise SystemExit(pytest.main(sys.argv[1:]))
