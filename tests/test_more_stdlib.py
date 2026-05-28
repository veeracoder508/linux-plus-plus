import os
import signal
from linux_plus_plus.stdlib import IOManager, EnvManager, AliasStore, SignalHandler


def test_redirect_stdout_to_file(tmp_path):
    p = tmp_path / "out.txt"
    with IOManager.redirect_stdout_to_file(str(p)):
        IOManager.write("hello")

    content = p.read_text(encoding="utf-8")
    assert "hello" in content


def test_file_size_and_exists(tmp_path):
    p = tmp_path / "f.txt"
    IOManager.write_file(str(p), "abc")
    assert IOManager.file_exists(str(p))
    assert IOManager.file_size(str(p)) == 3


def test_env_resolve_command(monkeypatch, tmp_path):
    # Create a fake command in a temp dir and ensure resolve_command finds it
    d = tmp_path / "bin"
    d.mkdir()
    cmd = d / "mycmd"
    cmd.write_text("#!/bin/sh\necho hi\n")

    # Force Windows-style resolution (returns any stat'd file)
    import linux_plus_plus.stdlib as stdlib
    monkeypatch.setattr(stdlib, 'IS_WINDOWS', True)
    monkeypatch.setattr(stdlib.EnvManager, 'path_dirs', classmethod(lambda cls: [str(d)]))

    found = EnvManager.resolve_command('mycmd')
    assert found is not None and str(d) in found


def test_alias_recursion_stops():
    AliasStore.unset('a'); AliasStore.unset('b')
    AliasStore.set('a', 'b')
    AliasStore.set('b', 'a')
    res = AliasStore.expand(['a'])
    # Should not loop forever; returns a head token after detecting recursion
    assert res == ['a']


def test_signal_handler_dispatch():
    called = []

    def h(sig, frame):
        called.append(sig)

    # Register a custom signal number (use SIGUSR1 where available)
    signum = getattr(signal, 'SIGUSR1', signal.SIGINT)
    SignalHandler.reset(signum)
    SignalHandler.register(signum, h)

    # Invoke dispatch directly
    SignalHandler._dispatch(signum, None)
    assert called and called[0] == signum
