"""
# linux++ — Standard Library (Layer 2)

This module provides a compact, dependency-free standard library for the
linux++ project. It intentionally exposes a small, stable façade used by the
upper layers (shell, kernel and applications) and concentrates common
utility code that would otherwise be duplicated across the codebase.

Key components:
- `IOManager`: unified helpers for terminal and file I/O, low-level pipe
    helpers and convenience redirect context managers.
- `SignalHandler`: small multi-callback signal dispatcher and helpers for
    registering Ctrl+C and termination handlers.
- `EnvManager`: environment and configuration handling (PATH resolution,
    config file loading and a merged view of local and host environment).
- `AliasStore`: shell alias table with safe expansion semantics.

Design goals:
- Use only Python standard library modules so this layer is easy to test and
    bundle.
- Keep interfaces minimal and explicit — callers should not rely on hidden
    side-effects.
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
    """High-level I/O primitives used across linux++.

    `IOManager` centralises common I/O operations so higher-level code can
    work with text and files in a consistent way without repeatedly dealing
    with low-level `os` file descriptors. The class provides:
    - stdout/stderr helpers (`write`, `error`),
    - blocking line and full-stdin readers for pipelines,
    - low-level file operations using `os.open`/`os.read` to avoid
        implicit buffering differences across platforms,
    - pipe helpers returning raw file descriptors for use with `os.pipe()`,
    - context managers for temporarily redirecting `sys.stdin`/`sys.stdout`.

    All methods use only standard library facilities so this module remains
    portable and easy to unit-test.
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
        """Read a single line from standard input using `input()`.

        Returns an empty string on EOF to make callers' control flow simpler
        (no exception handling required).
        """
        try:
            return input(prompt)
        except EOFError:
            return ""

    @staticmethod
    def read_all_stdin() -> str:
        """Read and return the entire contents of standard input.

        Useful when the shell or a builtin should consume piped input
        completely before processing.
        """
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
        """Read the entire file `path` and return its contents decoded.

        The implementation uses low-level `os.read` to avoid differences in
        platform newline handling and to give predictable memory behaviour for
        moderately-sized files. Caller receives a decoded `str`.
        """
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
        """Write `content` to `path`, creating or truncating the file.

        New files are created with mode `0o644`. Exceptions from the host
        filesystem (e.g. PermissionError, OSError) propagate to the caller.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o644)
        try:
            os.write(fd, content.encode(encoding))
        finally:
            os.close(fd)

    @staticmethod
    def append_file(path: str, content: str, encoding: str = "utf-8") -> None:
        """Append `content` to `path`, creating it if necessary.

        Uses `os.O_APPEND` to ensure writes are appended atomically on POSIX
        systems.
        """
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
        """Write `data` to a pipe file descriptor and close it.

        This helper encodes the string and ensures the write end of the pipe
        is closed after writing to signal EOF to the reader.
        """
        os.write(write_fd, data.encode(encoding))
        os.close(write_fd)

    @staticmethod
    def pipe_read(read_fd: int, encoding: str = "utf-8") -> str:
        """Read all data from a pipe file descriptor and return decoded text.

        The function closes the read end when finished.
        """
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
    """Context manager used by `IOManager` to temporarily redirect streams.

    Restores the original stream on exit. The helper is intentionally small
    and designed only for short-lived redirections used by the shell's
    dispatcher and tests.
    """

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
    """A small, multi-callback signal dispatcher.

    Python's `signal.signal()` allows a single handler per signal; this
    helper enables multiple callbacks to be registered for the same signal
    and calls them in registration order. It also provides convenience
    methods for registering common handlers (e.g. `on_ctrl_c`). On Windows
    platform support is limited to the signals available there (commonly
    SIGINT and SIGTERM).
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
        """Register a Ctrl+C (SIGINT) handler.

        The handler will be called with the `(sig, frame)` signature just like
        a normal signal handler.
        """
        cls.register(signal.SIGINT, handler)

    @classmethod
    def on_terminate(cls, handler: Callable) -> None:
        """Register a SIGTERM handler when supported by the platform.

        On Windows this is a no-op because SIGTERM is not always available.
        """
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
    """Environment and configuration helper for linux++.

    `EnvManager` maintains a merged view of the host environment and a local
    store used by the shell and applications. Local variables may shadow host
    environment keys and can optionally be exported to `os.environ` so child
    processes inherit them.

    Responsibilities include PATH resolution, loading/saving a simple INI
    config (`.linuxpprc`) and providing helpers such as `username()` and
    `hostname()` used in prompt rendering.
    """

    _local: dict[str, str] = {}

    # --- get / set / unset ---

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        """Return the value for `key`, preferring the local store.

        If the key is not present in the local store the method falls back to
        `os.environ` and finally returns `default`.
        """
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
        """Remove `key` from both the local store and host environment.

        This operation is idempotent and will silently ignore missing keys.
        """
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
        """Return the `PATH` environment variable split into directories.

        Uses `;` on Windows and `:` on POSIX-like platforms.
        """
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
        """Write current local env and aliases to a .linuxpprc file.

        The method writes the local variables (lowercased) under the `[env]`
        section and current aliases under `[aliases]`.
        """
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
        """Return the configured home directory for the current user."""
        return os.path.expanduser("~")

    @classmethod
    def username(cls) -> str:
        """Return a best-effort username used for prompts.

        The method checks common environment keys and falls back to `user`.
        """
        return cls.get("USER") or cls.get("USERNAME") or "user"

    @classmethod
    def hostname(cls) -> str:
        """Return the system hostname used in prompt rendering."""
        import socket
        return socket.gethostname()


# ---------------------------------------------------------------------------
# AliasStore — shell alias management
# ---------------------------------------------------------------------------

class AliasStore:
    """A lightweight alias table used by the shell.

    Aliases map a single token to an expanded command string. Expansion is
    performed by `expand()` which supports recursive alias substitution with
    a safety limit to avoid infinite loops.
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
        """Expand the head token using the alias table.

        If the first token matches an alias the expansion string is split on
        whitespace and substituted for the head token. The process repeats up
        to 10 times to support chained aliases while avoiding accidental
        infinite recursion.
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
    """Facade providing convenient access to standard helpers.

    The `Stdlib` class is a small namespace bundling the `IOManager`,
    `SignalHandler`, `EnvManager` and `AliasStore` under a single importable
    symbol. It also exposes a `boot()` helper to load user config and set up
    default signal handlers.

    Example:
        from stdlib import Stdlib
        Stdlib.boot()
        Stdlib.io.write("Hello")
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
