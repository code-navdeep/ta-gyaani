"""
The watchdog ends the PROCESS when a thread the system needs has died, so
Docker's `restart: unless-stopped` brings the container back — and the EOD
token renewal hands the new token to the running engine.

signal_handler ends in sys.exit(0), which in a non-main thread only ends that
thread. The watchdog used to rely on it, so a dead engine left the dashboard
serving frozen numbers with no restart. It now calls os._exit(1) after
signal_handler, whatever signal_handler raises.

os._exit, signal_handler, time.sleep and the critical log line are stubbed:
the test ends no process, waits for nothing and writes nothing to the real log.
"""
import pytest

import main


class _Thread:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _Engine:
    def __init__(self, fetch_alive=True):
        self.fetch_thread = _Thread(fetch_alive)


@pytest.fixture
def watchdog(monkeypatch):
    calls = {'exit': [], 'handler': 0}

    def handler(sig, frame):
        calls['handler'] += 1
        raise SystemExit(0)            # what the real signal_handler ends with

    monkeypatch.setattr(main.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(main, 'signal_handler', handler)
    monkeypatch.setattr(main.os, '_exit', lambda code: calls['exit'].append(code))
    monkeypatch.setattr(main.logger, 'critical', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'shutdown_in_progress', False)
    monkeypatch.setattr(main, 'engine', _Engine())
    return calls


def _run(engine=_Thread(), metrics=_Thread(), daily=_Thread()):
    main.run_watchdog(engine, metrics, daily)


class TestTheProcessEnds:

    def test_exit_follows_signal_handler_despite_its_system_exit(self, watchdog):
        _run(engine=_Thread(alive=False))
        assert watchdog['handler'] == 1
        assert watchdog['exit'] == [1]

    def test_a_dead_metrics_thread_ends_the_process(self, watchdog):
        _run(metrics=_Thread(alive=False))
        assert watchdog['exit'] == [1]

    def test_a_dead_fetch_thread_ends_the_process(self, watchdog, monkeypatch):
        """run_engine only waits on the fetch thread, so EngineThread can be
        alive while nothing is fetching."""
        monkeypatch.setattr(main, 'engine', _Engine(fetch_alive=False))
        _run()
        assert watchdog['exit'] == [1]

    def test_a_dead_scheduler_thread_ends_the_process(self, watchdog):
        _run(daily=_Thread(alive=False))
        assert watchdog['exit'] == [1]


class TestTokenHandOver:

    def test_job_4_gives_the_running_engine_the_new_token(self, monkeypatch):
        import scripts.renew_token as renew_module

        class _Fetcher:
            access_token = 'OLD'

        class _Eng:
            quote_fetcher = _Fetcher()

        monkeypatch.setattr(renew_module, 'renew_token', lambda: 'NEW')
        monkeypatch.setattr(main, 'engine', _Eng())
        main._renew_token_for_running_engine()
        assert main.engine.quote_fetcher.access_token == 'NEW'
