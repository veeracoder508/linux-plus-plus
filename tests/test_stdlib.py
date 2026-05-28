import os
from linux_plus_plus.stdlib import IOManager, EnvManager, AliasStore


def test_file_write_read_tmp(tmp_path):
    p = tmp_path / "test.txt"
    IOManager.write_file(str(p), "hello\n")
    IOManager.append_file(str(p), "world\n")

    assert IOManager.file_exists(str(p))
    content = IOManager.read_file(str(p))
    assert "hello" in content and "world" in content

    lines = IOManager.read_lines(str(p))
    assert lines == ["hello", "world"]


def test_pipe_roundtrip():
    r, w = IOManager.make_pipe()
    IOManager.pipe_write(w, "ping")
    result = IOManager.pipe_read(r)
    assert result == "ping"


def test_env_and_aliases(monkeypatch):
    # Ensure a clean local env
    EnvManager.unset("LPP_TEST")
    EnvManager.set("LPP_TEST", "42")
    assert EnvManager.get("LPP_TEST") == "42"
    EnvManager.unset("LPP_TEST")
    assert EnvManager.get("LPP_TEST") == ""

    AliasStore.set("ll", "ls -la")
    assert AliasStore.get("ll") == "ls -la"
    expanded = AliasStore.expand(["ll", "/tmp"])
    assert expanded == ["ls", "-la", "/tmp"]
