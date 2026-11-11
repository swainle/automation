from unittest.mock import Mock, patch
import subprocess
import sys

import pytest

from automation.pdd.spider import mobile


class Process:
    def __init__(self, codes):
        self.codes = iter(codes)
        self.returncode = None

    def poll(self):
        if self.returncode is None:
            self.returncode = next(self.codes)
        return self.returncode


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(mobile.config, 'BUSINESS_ROOT', tmp_path)
    monkeypatch.setattr(mobile.config, 'RETRY_ATTEMPTS', 3)
    monkeypatch.setattr(mobile.config, 'RETRY_DELAY', 0)


def test_help_does_not_launch_devices():
    from automation.pdd.spider.__main__ import main
    with patch('automation.pdd.spider.ios_mobile.run') as run, pytest.raises(SystemExit) as stopped:
        main(['--help'])
    assert stopped.value.code == 0
    run.assert_not_called()


def test_default_dispatches_full_pipeline():
    from automation.pdd.spider.__main__ import main
    with patch('automation.pdd.spider.commands.execute', return_value=0) as run:
        assert main([]) == 0
    assert run.call_args.kwargs['args'].ios
    assert run.call_args.kwargs['args'].mobile


def test_pipeline_runs_both_collectors_then_drains_product_queue():
    rank, product = Process([None, 0]), Process([None, None, 0])
    with patch.object(mobile, 'start', side_effect=[rank, product]) as start, \
            patch.object(mobile.time, 'sleep'), patch.object(mobile, 'stop') as stop:
        assert mobile.run() == 0
    assert [call.args for call in start.call_args_list] == [
        ('automation.pdd.spider.ranking', '--ios'), ('automation.pdd.spider.product', '--android')]
    assert stop.call_count == 2


def test_failed_rank_is_retried_without_restart_of_healthy_product():
    ranks = [Process([1]), Process([None, 0])]
    product = Process([None, 0])
    with patch.object(mobile, 'start', side_effect=[*ranks, product]) as start, \
            patch.object(mobile.time, 'sleep'), patch.object(mobile, 'stop'):
        assert mobile.run() == 0
    assert start.call_count == 3


def test_exhausted_rank_retries_return_failure_after_product_finishes():
    with patch.object(mobile, 'start', side_effect=[Process([1]), Process([1]), Process([1]), Process([None, 0])]), \
            patch.object(mobile.time, 'sleep'), patch.object(mobile, 'stop'):
        assert mobile.run() == 1


def test_product_process_crash_restarts_from_database():
    worker = mobile.ProductWorker(3, 0)
    with patch.object(mobile, 'start', side_effect=[Process([1]), Process([0])]) as start:
        worker.tick(producing=True)
        worker.tick(producing=True)
        worker.tick(producing=False)
    assert worker.result == 0 and start.call_count == 2


def test_interrupt_stops_owned_collectors():
    rank, product = Mock(), Mock()
    rank.poll.return_value = None
    with patch.object(mobile, 'start', side_effect=[rank, product]), \
            patch.object(mobile.time, 'sleep', side_effect=KeyboardInterrupt), \
            patch.object(mobile, 'stop') as stop:
        assert mobile.run() == 130
    assert [call.args[0] for call in stop.call_args_list] == [rank, product]


def test_mobile_supervisor_keeps_consumer_alive_before_rank_and_between_retries(tmp_path):
    from automation.pdd.spider.concurrency import ranking_active, workflow_lock
    assert not ranking_active()
    with workflow_lock('mobile', timeout=0):
        assert ranking_active()
    assert not ranking_active()


def test_cleanup_signals_both_children_before_waiting_and_masks_repeated_ctrl_c():
    rank, product = Mock(), Mock()
    rank.poll.return_value = product.poll.return_value = None
    actions = []
    handlers = {}
    def set_handler(sig, handler):
        old = handlers.get(sig, mobile.signal.default_int_handler)
        handlers[sig] = handler
        return old
    def signal_child(process):
        actions.append(('signal', process))
    def wait_child(process, **kwargs):
        assert handlers[mobile.signal.SIGINT] == mobile.signal.SIG_IGN
        actions.append(('wait', process))
    with patch.object(mobile.signal, 'signal', side_effect=set_handler), \
            patch.object(mobile, 'request_stop', side_effect=signal_child), \
            patch.object(mobile, 'stop', side_effect=wait_child):
        mobile.stop_all(rank, product)
    assert actions == [('signal', rank), ('signal', product), ('wait', rank), ('wait', product)]
    assert handlers[mobile.signal.SIGINT] == mobile.signal.default_int_handler


def test_interrupted_wait_still_forces_child_exit():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [KeyboardInterrupt, 0]
    mobile.stop(process, requested=True)
    process.terminate.assert_called_once()
    assert process.wait.call_count == 2


def test_cleanup_failure_on_first_child_does_not_skip_second():
    rank, product = Mock(), Mock()
    with patch.object(mobile, 'request_stop'), \
            patch.object(mobile, 'stop', side_effect=[RuntimeError('first'), None]) as stop, \
            pytest.raises(RuntimeError, match='first'):
        mobile.stop_all(rank, product)
    assert [call.args[0] for call in stop.call_args_list] == [rank, product]


def test_real_child_is_reaped_when_graceful_signal_is_unavailable():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             creationflags=subprocess.CREATE_NO_WINDOW if mobile.os.name == 'nt' else 0)
    try:
        # No console event: exercise the forced-exit path against a real process.
        mobile.stop(child, requested=True, timeout=.05)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_managed_tree_kills_windows_launcher_and_real_python_descendants(tmp_path):
    from processkit import Command
    import time
    ready = tmp_path / 'descendant.pid'
    script = ('import subprocess,sys,time; from pathlib import Path; '
              'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
              'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')
    command = Command(sys.executable, ['-c', script, str(ready)]).stdout('inherit').stderr('inherit')
    if mobile.os.name == 'nt':
        command = command.create_no_window()
    process = mobile.ManagedProcess(command)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        assert len(process.group.members()) >= 2
        mobile.stop(process, requested=True, timeout=.05)
        assert process.poll() is not None
        assert not process.group.members()
    finally:
        process.close()
