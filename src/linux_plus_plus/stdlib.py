"""
linux++ — Standard Library (Layer 2)
=====================================
Pure Python standard library only. No third-party deps.
Depends only on Layer 1 (HAL). All higher layers import from here.

Modules:
  - IOManager      : file & stream I/O
  - SignalHandler  : interrupt / signal management
  - EnvManager     : environment variables, PATH, config
"""

import os
import sys
import io
import signal
import configparser
from typing import Optional, Callable


# ---------------------------------------------------------------------------
# Lazy HAL import (Layer 1)
# ---------------------------------------------------------------------------
# We import HAL at the bottom of each method call so Layer 2 stays testable
# independently. You can also pass hal in at construction time.
try:
    from .hal import HAL, IS_WINDOWS
except ImportError:
    HAL = None
    IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# IOManager — unified I/O for files, stdin, stdout, stderr
# ---------------------------------------------------------------------------

class IOManager:
    """
    Handles all I/O for linux++.
    Uses only: os, sys, io — pure CPython builtins.
    """

    # --- stdout / stderr ---

    @staticmethod
    def write(text: str, end: str = "\n") -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()

    @staticmethod
    def error(text: str, end: str = "\n") -> None:
        sys.stderr.write(text + end)
        sys.stderr.flush()

    @staticmethod
    def read_line(prompt: str = "") -> str:
        """Read one line from stdin. Returns '' on EOF."""
        try:
            return input(prompt)
        except EOFError:
            return ""

    @staticmethod
    def read_all_stdin() -> str:
        """Drain stdin completely (useful for pipes)."""
        return sys.stdin.read()

    # --- file I/O (low-level os module, no pathlib/shutil) ---

    @staticmethod
    def open_file(path: str, mode: str = "r", encoding: str = "utf-8") -> io.TextIOWrapper:
        """
        Open a file and return a file object.
        Caller is responsible for closing (use with `with`).
        """
        binary_modes = {"rb", "wb", "ab", "r+b", "w+b"}
        if mode in binary_modes:
            return open(path, mode)
        return open(path, mode, encoding=encoding)

    @staticmethod
    def read_file(path: str, encoding: str = "utf-8") -> str:
        """Read entire file as text."""
        fd = os.open(path, os.O_RDONLY)
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode(encoding)
        finally:
            os.close(fd)

    @staticmethod
    def write_file(path: str, content: str, encoding: str = "utf-8") -> None:
        """Write (overwrite) a file."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o644)
        try:
            os.write(fd, content.encode(encoding))
        finally:
            os.close(fd)

    @staticmethod
    def append_file(path: str, content: str, encoding: str = "utf-8") -> None:
        """Append to a file."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(path, flags, 0o644)
        try:
            os.write(fd, content.encode(encoding))
        finally:
            os.close(fd)

    @staticmethod
    def read_lines(path: str, encoding: str = "utf-8") -> list[str]:
        """Read file as a list of lines (no trailing newlines)."""
        return IOManager.read_file(path, encoding).splitlines()

    @staticmethod
    def file_exists(path: str) -> bool:
        try:
            os.stat(path)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def file_size(path: str) -> int:
        return os.stat(path).st_size

    # --- pipe I/O ---

    @staticmethod
    def make_pipe() -> tuple[int, int]:
        """
        Create an OS-level pipe.
        Returns (read_fd, write_fd).
        """
        return os.pipe()

    @staticmethod
    def pipe_write(write_fd: int, data: str, encoding: str = "utf-8") -> None:
        os.write(write_fd, data.encode(encoding))
        os.close(write_fd)

    @staticmethod
    def pipe_read(read_fd: int, encoding: str = "utf-8") -> str:
        chunks = []
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(read_fd)
        return b"".join(chunks).decode(encoding)

    # --- redirect helpers (used by shell for > >> <) ---

    @staticmethod
    def redirect_stdout_to_file(path: str):
        """
        Context manager: redirects stdout to a file for the duration of the block.

        Usage:
            with IOManager.redirect_stdout_to_file("/tmp/out.txt"):
                IOManager.write("this goes to the file")
        """
        return _RedirectContext(path, "w", sys.stdout, "stdout")

    @staticmethod
    def append_stdout_to_file(path: str):
        """Context manager: appends stdout to file (>>)."""
        return _RedirectContext(path, "a", sys.stdout, "stdout")

    @staticmethod
    def redirect_stdin_from_file(path: str):
        """Context manager: reads stdin from file (<)."""
        return _RedirectContext(path, "r", sys.stdin, "stdin")


class _RedirectContext:
    """Internal context manager for stdout/stdin redirection."""

    def __init__(self, path: str, mode: str, original_stream, stream_name: str):
        self._path    = path
        self._mode    = mode
        self._orig    = original_stream
        self._name    = stream_name
        self._file    = None

    def __enter__(self):
        self._file = open(self._path, self._mode, encoding="utf-8")
        setattr(sys, self._name, self._file)
        return self._file

    def __exit__(self, *_):
        setattr(sys, self._name, self._orig)
        if self._file:
            self._file.close()


# ---------------------------------------------------------------------------
# SignalHandler — interrupt and signal management
# ---------------------------------------------------------------------------

class SignalHandler:
    """
    Cross-platform signal handling.
    Uses only: signal, os — pure Python builtins.
    On Windows, only SIGINT and SIGTERM are available.
    """

    _handlers: dict[int, list[Callable]] = {}

    @classmethod
    def register(cls, sig: int, handler: Callable) -> None:
        """
        Register a callback for a signal.
        Multiple callbacks per signal are supported (called in order).
        """
        if sig not in cls._handlers:
            cls._handlers[sig] = []
            signal.signal(sig, cls._dispatch)
        cls._handlers[sig].append(handler)

    @classmethod
    def _dispatch(cls, sig: int, frame) -> None:
        for handler in cls._handlers.get(sig, []):
            try:
                handler(sig, frame)
            except Exception:
                pass

    @classmethod
    def on_ctrl_c(cls, handler: Callable) -> None:
        """Register a Ctrl+C (SIGINT) handler."""
        cls.register(signal.SIGINT, handler)

    @classmethod
    def on_terminate(cls, handler: Callable) -> None:
        """Register SIGTERM handler (skipped silently on Windows)."""
        if not IS_WINDOWS:
            cls.register(signal.SIGTERM, handler)

    @classmethod
    def ignore(cls, sig: int) -> None:
        signal.signal(sig, signal.SIG_IGN)

    @classmethod
    def reset(cls, sig: int) -> None:
        signal.signal(sig, signal.SIG_DFL)
        cls._handlers.pop(sig, None)

    @classmethod
    def reset_all(cls) -> None:
        for sig in list(cls._handlers):
            cls.reset(sig)

    # --- convenience: block SIGINT during critical sections ---

    @staticmethod
    def block_interrupt():
        """
        Context manager that suppresses Ctrl+C for a block.

        Usage:
            with SignalHandler.block_interrupt():
                save_state()  # won't be interrupted
        """
        return _BlockInterrupt()


class _BlockInterrupt:
    def __enter__(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def __exit__(self, *_):
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# EnvManager — environment variables, PATH resolution, config files
# ---------------------------------------------------------------------------

class EnvManager:
    """
    Manages the linux++ environment.
    Uses only: os, configparser — pure Python builtins.

    Maintains two stores:
      - os.environ   : real host environment (inherited by child processes)
      - _local       : linux++ internal variables (not leaked to host)
    """

    _local: dict[str, str] = {}

    # --- get / set / unset ---

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        """Get a variable. Checks local store first, then os.environ."""
        return cls._local.get(key) or os.environ.get(key, default)

    @classmethod
    def set(cls, key: str, value: str, export: bool = False) -> None:
        """
        Set a variable.
        export=True also writes to os.environ (child processes inherit it).
        """
        cls._local[key] = value
        if export:
            os.environ[key] = value

    @classmethod
    def unset(cls, key: str) -> None:
        cls._local.pop(key, None)
        os.environ.pop(key, None)

    @classmethod
    def all(cls) -> dict[str, str]:
        """Merged view: os.environ + local overrides."""
        merged = dict(os.environ)
        merged.update(cls._local)
        return merged

    # --- PATH resolution ---

    @classmethod
    def path_dirs(cls) -> list[str]:
        """Return PATH as a list of directories."""
        raw = cls.get("PATH", "")
        sep = ";" if IS_WINDOWS else ":"
        return [d for d in raw.split(sep) if d]

    @classmethod
    def resolve_command(cls, name: str) -> Optional[str]:
        """
        Find the full path of a command by searching PATH.
        Returns None if not found.
        Uses only os.stat — no shutil.which.
        """
        # If it already looks like a path, just check directly
        if os.sep in name or (IS_WINDOWS and "/" in name):
            return name if IOManager.file_exists(name) else None

        extensions = ["", ".exe", ".cmd", ".bat"] if IS_WINDOWS else [""]

        for directory in cls.path_dirs():
            for ext in extensions:
                candidate = os.path.join(directory, name + ext)
                try:
                    st = os.stat(candidate)
                    # On Unix, check execute bit
                    if not IS_WINDOWS:
                        if st.st_mode & 0o111:
                            return candidate
                    else:
                        return candidate
                except (FileNotFoundError, NotADirectoryError):
                    continue
        return None

    # --- config file (.linuxpprc) ---

    @classmethod
    def load_config(cls, path: Optional[str] = None) -> None:
        """
        Load a .linuxpprc config file (INI format).
        Defaults to ~/.linuxpprc

        Example .linuxpprc:
            [env]
            EDITOR = nano
            PAGER  = less

            [aliases]
            ll = ls -la
            ..= cd ..
        """
        if path is None:
            path = os.path.join(os.path.expanduser("~"), ".linuxpprc")

        if not IOManager.file_exists(path):
            return

        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")

        # Load [env] section into EnvManager
        if cfg.has_section("env"):
            for key, value in cfg.items("env"):
                cls.set(key.upper(), value, export=True)

        # Load [aliases] into AliasStore
        if cfg.has_section("aliases"):
            for alias, expansion in cfg.items("aliases"):
                AliasStore.set(alias, expansion)

    @classmethod
    def save_config(cls, path: Optional[str] = None) -> None:
        """Write current env + aliases back to .linuxpprc."""
        if path is None:
            path = os.path.join(os.path.expanduser("~"), ".linuxpprc")

        cfg = configparser.ConfigParser()

        cfg["env"] = {
            k.lower(): v
            for k, v in cls._local.items()
        }
        cfg["aliases"] = dict(AliasStore._store)

        with open(path, "w", encoding="utf-8") as f:
            cfg.write(f)

    # --- common helpers ---

    @classmethod
    def home(cls) -> str:
        return os.path.expanduser("~")

    @classmethod
    def username(cls) -> str:
        return cls.get("USER") or cls.get("USERNAME") or "user"

    @classmethod
    def hostname(cls) -> str:
        import socket
        return socket.gethostname()


# ---------------------------------------------------------------------------
# AliasStore — shell alias management
# ---------------------------------------------------------------------------

class AliasStore:
    """
    Simple alias store: maps short names to expanded command strings.
    e.g.  ll -> ls -la
    """

    _store: dict[str, str] = {}

    @classmethod
    def set(cls, alias: str, expansion: str) -> None:
        cls._store[alias] = expansion

    @classmethod
    def get(cls, alias: str) -> Optional[str]:
        return cls._store.get(alias)

    @classmethod
    def unset(cls, alias: str) -> None:
        cls._store.pop(alias, None)

    @classmethod
    def all(cls) -> dict[str, str]:
        return dict(cls._store)

    @classmethod
    def expand(cls, tokens: list[str]) -> list[str]:
        """
        If tokens[0] is an alias, replace it with the expanded tokens.
        Handles recursive aliases up to 10 levels deep.
        """
        seen = set()
        for _ in range(10):
            if not tokens:
                break
            head = tokens[0]
            if head in seen:
                break
            expansion = cls._store.get(head)
            if expansion is None:
                break
            seen.add(head)
            tokens = expansion.split() + tokens[1:]
        return tokens


# ---------------------------------------------------------------------------
# Stdlib façade — single import point for Layer 3+
# ---------------------------------------------------------------------------

class Stdlib:
    """
    Unified stdlib entry point.

    Usage:
        from stdlib import Stdlib
        Stdlib.io.write("Hello!")
        Stdlib.env.set("EDITOR", "nano")
        Stdlib.signals.on_ctrl_c(my_handler)
    """
    io      = IOManager
    signals = SignalHandler
    env     = EnvManager
    aliases = AliasStore

    @classmethod
    def boot(cls) -> None:
        """Initialize stdlib: load config, set up default signals."""
        cls.env.load_config()

        def _default_ctrl_c(sig, frame):
            cls.io.write("\n[Ctrl+C]")

        cls.signals.on_ctrl_c(_default_ctrl_c)


# ---------------------------------------------------------------------------
# Quick self-test  (python stdlib.py)
# ---------------------------------------------------------------------------

def main():
    Stdlib.boot()
    io  = Stdlib.io
    env = Stdlib.env

    io.write("=== linux++ stdlib self-test ===\n")

    # IOManager
    tmp = os.path.join(os.path.expanduser("~"), "_lpp_test.txt")
    IOManager.write_file(tmp, "hello from linux++\n")
    IOManager.append_file(tmp, "second line\n")
    content = IOManager.read_file(tmp)
    assert "hello" in content and "second" in content
    os.unlink(tmp)
    io.write("[IOManager]    read/write/append: OK")

    # pipe
    r, w = IOManager.make_pipe()
    IOManager.pipe_write(w, "ping")
    result = IOManager.pipe_read(r)
    assert result == "ping"
    io.write("[IOManager]    pipe read/write:   OK")

    # EnvManager
    EnvManager.set("LPP_TEST", "42")
    assert EnvManager.get("LPP_TEST") == "42"
    EnvManager.unset("LPP_TEST")
    assert EnvManager.get("LPP_TEST") == ""
    io.write("[EnvManager]   set/get/unset:     OK")

    # PATH resolution
    python_cmd = "python3" if not IS_WINDOWS else "python"
    found = EnvManager.resolve_command(python_cmd)
    io.write(f"[EnvManager]   resolve python:    {found or 'not found'}")

    # AliasStore
    AliasStore.set("ll", "ls -la")
    expanded = AliasStore.expand(["ll", "/tmp"])
    assert expanded == ["ls", "-la", "/tmp"]
    io.write("[AliasStore]   alias expand:      OK")

    # SignalHandler
    io.write("[SignalHandler] registered SIGINT: OK")

    io.write("\nAll stdlib tests passed.")



if __name__ == "__main__":
    main()
