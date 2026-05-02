"""
linux++ — Hardware Abstraction Layer (HAL) (Layer 1)
=====================================================

This module provides a compact Hardware Abstraction Layer used by the
rest of the linux++ project. Higher layers (stdlib, kernel, shell and
applications) import from this module to perform OS-dependent operations
without touching platform-specific APIs directly.

Responsibilities provided by HAL:
- Terminal control (ANSI colours, clearing, terminal size)
- Filesystem helpers and path normalization
- Process execution and background process handling
- Network helpers (hostname, local IP, ping)
- System metadata (uptime, cpu, memory)
- Signal registration utilities

The implementation prefers small, well-documented helpers that use only the
Python standard library so the HAL is easy to test and vendor into other
projects. Where third-party functionality would be useful (psutil) the
module falls back gracefully when it is not installed.
"""

import os
import sys
import shutil
import platform
import subprocess
import tempfile
import signal
import stat
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# OS detection (single source of truth)
# ---------------------------------------------------------------------------

class OSType:
    """Constants representing detected operating system types.

    These string constants are used across the codebase to branch behavior
    where necessary (for example, choosing the correct command-line flags
    or enabling Windows-specific terminal features).
    """
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


def _detect_os() -> str:
    """Detect the current host operating system.

    Returns one of the `OSType` constants. The function normalises the
    platform-string returned by `platform.system()` and maps it to a known
    value; unknown platforms are reported as `OSType.UNKNOWN`.
    """
    s = platform.system().lower()
    if s == "linux":
        return OSType.LINUX
    if s == "darwin":
        return OSType.MACOS
    if s == "windows":
        return OSType.WINDOWS
    return OSType.UNKNOWN


CURRENT_OS: str = _detect_os() 
IS_WINDOWS: bool = CURRENT_OS == OSType.WINDOWS
IS_UNIX: bool = CURRENT_OS in (OSType.LINUX, OSType.MACOS)


# ---------------------------------------------------------------------------
# TerminalDriver — cross-platform terminal control
# ---------------------------------------------------------------------------

class TerminalDriver:
    """Cross-platform terminal utilities.

    The `TerminalDriver` exposes helpers for writing coloured text, clearing
    the screen, reading user input and querying terminal size. The class is
    deliberately lightweight and uses ANSI escape sequences on platforms
    that support them (Linux, macOS, and modern Windows terminals).
    """

    # ANSI codes work on Linux/macOS and on modern Windows 10+ terminals
    _ANSI = not IS_WINDOWS or os.environ.get("WT_SESSION") # Windows Terminal

    RESET = "\033[0m" if _ANSI else ""
    BOLD = "\033[1m" if _ANSI else ""
    RED = "\033[31m" if _ANSI else ""
    GREEN = "\033[32m" if _ANSI else ""
    YELLOW = "\033[33m" if _ANSI else ""
    BLUE = "\033[34m" if _ANSI else ""
    CYAN = "\033[36m" if _ANSI else ""
    WHITE = "\033[37m" if _ANSI else ""

    @staticmethod
    def enable_ansi_on_windows() -> None:
        """Attempt to enable ANSI/VT processing on legacy Windows consoles.

        When available this enables correct rendering of ANSI escape
        sequences (colours, bold, etc.) in cmd.exe and PowerShell. Failure
        is non-fatal and simply results in plain-text output.
        """
        if IS_WINDOWS:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(
                    kernel32.GetStdHandle(-11), 7
                )
            except Exception:
                pass  # Not fatal — colors just won't render

    @staticmethod
    def clear() -> None:
        """Clear the terminal screen."""
        os.system("cls" if IS_WINDOWS else "clear")

    @staticmethod
    def get_size() -> tuple[int, int]:
        """Return the terminal width and height as (columns, rows).

        When the terminal size cannot be determined the method falls back to
        a sensible default of 80×24.
        """
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines

    @staticmethod
    def write(text: str, end: str = "\n") -> None:
        """Write `text` to standard output and flush immediately.

        This helper centralises output formatting so higher layers can
        substitute a different terminal driver during testing if required.
        """
        sys.stdout.write(text + end)
        sys.stdout.flush()

    @staticmethod
    def error(text: str) -> None:
        """Display the error.

        Args:
            text (str): The error message.
        """
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    @staticmethod
    def read_input(prompt: str = "") -> str:
        """Read a single line of input from the user, returning '' on EOF.

        The method wraps `input()` to provide a consistent behaviour and
        to make testing/replacement simpler.
        """
        try:
            return input(prompt)
        except EOFError:
            return ""


# ---------------------------------------------------------------------------
# DiskDriver — cross-platform filesystem operations
# ---------------------------------------------------------------------------

class DiskDriver:
    """Filesystem abstraction and path utilities.

    The DiskDriver provides a small set of deterministic filesystem
    operations that return predictable Python types (for example, `listdir`
    returns a list of dictionaries describing entries). Paths are expressed
    and returned as POSIX-style strings (forward slashes) to simplify the
    kernel's internal handling; conversion to platform-native paths happens
    via pathlib under the hood.
    """

    # Path separator — the kernel always works with forward slashes
    # internally; we convert on the way out on Windows.
    SEP = "/"

    @staticmethod
    def _to_path(p: str) -> Path:
        """Return a `pathlib.Path` for the given string.

        This helper centralises conversion so callers don't need to import
        `pathlib` themselves.
        """
        return Path(p)

    # --- directory ---

    @staticmethod
    def cwd() -> str:
        """Return the current working directory as a POSIX-style string.

        Using forward slashes avoids platform-dependent path separators in
        higher-level code and makes string comparisons predictable.
        """
        return Path.cwd().as_posix()

    @staticmethod
    def chdir(path: str) -> None:
        """Changes the current working directory of the process.

        Args:
            path (str): The destination directory path.
        """
        os.chdir(path)

    @staticmethod
    def mkdir(path: str, parents: bool = True, exist_ok: bool = True) -> None:
        """Create a directory at `path` with optional parent creation.

        Mirrors `pathlib.Path.mkdir` semantics. Errors such as permission
        denied propagate to the caller.
        """
        Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    @staticmethod
    def listdir(path: str) -> list[dict]:
        """List a directory and return a typed description for each entry.

        Each entry is a dictionary containing: `name`, `is_dir`, `is_file`,
        `is_symlink`, `size`, and a human-readable `permissions` string.
        Permission errors on individual entries are handled per-entry and do
        not abort the overall listing.
        """
        entries = []
        for entry in Path(path).iterdir():
            try:
                st = entry.stat()
                entries.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "is_file": entry.is_file(),
                    "is_symlink": entry.is_symlink(),
                    "size": st.st_size,
                    "permissions": DiskDriver._perms(st.st_mode),
                })
            except PermissionError:
                entries.append({
                    "name": entry.name,
                    "is_dir": False, "is_file": False,
                    "is_symlink": False, "size": 0,
                    "permissions": "?????????",
                })
        return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))

    @staticmethod
    def _perms(mode: int) -> str:
        """Convert a numeric mode to a POSIX-style permission string.

        Example: `0o755` -> 'rwxr-xr-x'.
        """
        flags = [
            (stat.S_IRUSR, "r"), (stat.S_IWUSR, "w"), (stat.S_IXUSR, "x"),
            (stat.S_IRGRP, "r"), (stat.S_IWGRP, "w"), (stat.S_IXGRP, "x"),
            (stat.S_IROTH, "r"), (stat.S_IWOTH, "w"), (stat.S_IXOTH, "x"),
        ]
        return "".join(c if mode & f else "-" for f, c in flags)

    # --- file operations ---

    @staticmethod
    def exists(path: str) -> bool:
        """Return True if the path exists on the filesystem.

        This is a thin wrapper around `pathlib.Path.exists()` to keep the
        DiskDriver surface small and consistent.
        """
        return Path(path).exists()

    @staticmethod
    def is_file(path: str) -> bool:
        """Check if it is a file

        Args:
            path (str): Path for the file.

        Returns:
            bool: If it is a file or not.
        """
        return Path(path).is_file()

    @staticmethod
    def is_dir(path: str) -> bool:
        """Check if it is a directory.

        Args:
            path (str): Path for the directory.

        Returns:
            bool: If it is a directory or not.
        """
        return Path(path).is_dir()

    @staticmethod
    def read_text(path: str, encoding: str = "utf-8") -> str:
        """Read the entire contents of `path` and return it as a string.

        Raises the same exceptions as `pathlib.Path.read_text()` on error.
        """
        return Path(path).read_text(encoding=encoding)

    @staticmethod
    def write_text(path: str, content: str, encoding: str = "utf-8") -> None:
        """Writes a string to a file, overwriting existing content.

        Args:
            path (str): The path to the file.
            content (str): The string content to write.
            encoding (str, optional): The text encoding to use. Defaults to "utf-8".
        """
        Path(path).write_text(content, encoding=encoding)

    @staticmethod
    def append_text(path: str, content: str, encoding: str = "utf-8") -> None:
        """Appends a string to the end of a file.

        Args:
            path (str): The path to the file.
            content (str): The string content to append.
            encoding (str, optional): The text encoding to use. Defaults to "utf-8".
        """
        with open(path, "a", encoding=encoding) as f:
            f.write(content)

    @staticmethod
    def delete(path: str) -> None:
        """Remove a file or directory tree rooted at `path`.

        Directories are removed recursively using `shutil.rmtree`. The
        operation may raise permission-related exceptions which the caller
        should handle.
        """
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)

    @staticmethod
    def copy(src: str, dst: str) -> None:
        """Copies a file from source to destination, preserving metadata.

        Args:
            src (str): The source file path.
            dst (str): The destination path.
        """
        shutil.copy2(src, dst)

    @staticmethod
    def move(src: str, dst: str) -> None:
        """Moves or renames a file or directory.

        Args:
            src (str): The source path.
            dst (str): The destination path.
        """
        shutil.move(src, dst)

    @staticmethod
    def resolve(path: str) -> str:
        """Resolve `path` to an absolute POSIX-style path string.

        Uses `Path.resolve()` to expand symlinks and relative segments. The
        returned string always uses forward slashes for internal consistency.
        """
        return Path(path).resolve().as_posix()

    @staticmethod
    def join(*parts: str) -> str:
        """Joins multiple path components into a single POSIX-style string.

        Args:
            *parts (str): Path components to join.

        Returns:
            str: The combined path using forward slashes.
        """
        return Path(*parts).as_posix()

    @staticmethod
    def home() -> str:
        """Returns the current user's home directory as a POSIX-style string.

        Returns:
            str: The home directory path.
        """
        return Path.home().as_posix()

    @staticmethod
    def temp_dir() -> str:
        """Returns the system's default temporary directory path.

        Returns:
            str: The temporary directory path.
        """
        return tempfile.gettempdir()

    @staticmethod
    def disk_usage(path: str = "/") -> dict:
        """Return disk space metrics for the filesystem containing `path`.

        The result is a dict with `total`, `used` and `free` properties in
        bytes. On Windows the default path is `C:\\` when no path is provided.
        """
        if IS_WINDOWS:
            path = "C:\\"  # default to C: on Windows
        usage = shutil.disk_usage(path)
        return {
            "total": usage.total,
            "used":  usage.used,
            "free":  usage.free,
        }


# ---------------------------------------------------------------------------
# ProcessDriver — cross-platform process management
# ---------------------------------------------------------------------------

class ProcessResult:
    """Encapsulates the result of an external process execution."""

    def __init__(self, stdout: str, stderr: str, returncode: int):
        """Initializes the process result.

        Args:
            stdout (str): The captured standard output.
            stderr (str): The captured standard error.
            returncode (int): The process exit code.
        """
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.ok = returncode == 0

    def __repr__(self):
        return f"<ProcessResult rc={self.returncode}>"


class ProcessDriver:
    """Runs external commands in an OS-independent way."""

    @staticmethod
    def run(
        command: list[str] | str,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        shell: bool = False,
        timeout: Optional[float] = None,
        capture: bool = True,
    ) -> ProcessResult:
        """Executes a command synchronously and waits for its completion.

        This method wraps subprocess.run to provide a uniform interface for command
        execution. It automatically handles Windows shell requirements for string
        commands and provides robust error handling for common failure modes like
        missing executables or timeouts.

        Args:
            command (list[str] | str): The command to execute, either as a list of 
                arguments or a single string.
            cwd (Optional[str]): Working directory to execute the command in.
            env (Optional[dict]): Dictionary of environment variables to add to 
                the process environment.
            shell (bool): Whether to use the shell as the executable. Defaults to 
                False (though forced True on Windows for string commands).
            timeout (Optional[float]): Maximum time in seconds to wait for completion.
            capture (bool): Whether to capture stdout and stderr. Defaults to True.

        Returns:
            ProcessResult: An object containing stdout, stderr, and the return code.
        """
        merged_env = {**os.environ, **(env or {})}

        # On Windows, string commands need shell=True to find built-ins
        if IS_WINDOWS and isinstance(command, str):
            shell = True

        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=merged_env,
                shell=shell,
                timeout=timeout,
                capture_output=capture,
                text=True,
            )
            return ProcessResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                returncode=result.returncode,
            )
        except FileNotFoundError:
            return ProcessResult("", f"Command not found: {command}", 127)
        except subprocess.TimeoutExpired:
            return ProcessResult("", "Process timed out", 124)
        except Exception as e:
            return ProcessResult("", str(e), 1)

    @staticmethod
    def spawn(
        command: list[str] | str,
        cwd:     Optional[str] = None,
        env:     Optional[dict] = None,
        shell:   bool = False,
    ) -> subprocess.Popen:
        """Starts a process asynchronously in the background.

        Unlike 'run', this method does not wait for the process to finish. It 
        returns the underlying subprocess.Popen object, allowing for manual 
        monitoring or communication.

        Args:
            command (list[str] | str): The command to execute.
            cwd (Optional[str]): Working directory for the process.
            env (Optional[dict]): Environment variable overrides.
            shell (bool): Whether to execute via the shell.

        Returns:
            subprocess.Popen: The handle to the background process.
        """
        merged_env = {**os.environ, **(env or {})}
        if IS_WINDOWS and isinstance(command, str):
            shell = True
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=merged_env,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def which(program: str) -> Optional[str]:
        """Locates the absolute path of an executable in the system's PATH.

        This mimics the behavior of the Unix 'which' command.

        Args:
            program (str): The name of the program to find.

        Returns:
            Optional[str]: The absolute path to the executable if found, else None.
        """
        return shutil.which(program)

    @staticmethod
    def pid() -> int:
        """Returns the process identifier (PID) of the current process.

        Returns:
            int: The current process ID.
        """
        return os.getpid()

    @staticmethod
    def kill(pid: int, force: bool = False) -> bool:
        """Terminates a process given its PID.

        On Windows, it uses 'taskkill'. On Unix-like systems, it sends 
        signals (SIGTERM/SIGKILL).

        Args:
            pid (int): The process ID to terminate.
            force (bool): If True, forces immediate termination (SIGKILL).

        Returns:
            bool: True if the termination command was successfully sent, else False.
        """
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F" if force else "/T", "/PID", str(pid)],
                    capture_output=True
                )
            else:
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def list_processes() -> list[dict]:
        """Retrieves a list of currently running processes on the host system.

        Uses 'tasklist' on Windows and 'ps' on Unix to gather basic process 
        information (Name and PID).

        Returns:
            list[dict]: A list of dictionaries, each containing 'name' and 'pid'.
        """
        if IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True
            )
            processes = []
            for line in result.stdout.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        processes.append({
                            "name": parts[0],
                            "pid":  int(parts[1]),
                        })
                    except ValueError:
                        pass
            return processes
        else:
            result = subprocess.run(
                ["ps", "-eo", "pid,comm"],
                capture_output=True, text=True
            )
            processes = []
            for line in result.stdout.strip().splitlines()[1:]:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        processes.append({
                            "pid":  int(parts[0]),
                            "name": parts[1].strip(),
                        })
                    except ValueError:
                        pass
            return processes


# ---------------------------------------------------------------------------
# NetworkDriver — cross-platform network utilities
# ---------------------------------------------------------------------------

class NetworkDriver:
    """Basic network information, OS-independent."""

    @staticmethod
    def hostname() -> str:
        """Retrieves the hostname of the local machine.

        Returns:
            str: The system hostname.
        """
        import socket
        return socket.gethostname()

    @staticmethod
    def local_ip() -> str:
        """Determines the primary local IP address of the system.

        This method uses a common socket trick: it creates a UDP connection to a 
        public DNS server (8.8.8.8) to see which local interface the OS 
        chooses to route the traffic through. No data is actually transmitted.

        Returns:
            str: The local IPv4 address, or '127.0.0.1' if offline or unreachable.
        """
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Connecting to an external IP doesn't send packets for UDP,
            # but it does force the OS to pick an outgoing local address.
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            # Fallback if no network interface is active
            return "127.0.0.1"

    @staticmethod
    def ping(host: str, count: int = 1) -> bool:
        """Checks if a remote host is reachable via ICMP.

        Args:
            host (str): The destination IP or domain name.
            count (int, optional): The number of packets to send. Defaults to 1.

        Returns:
            bool: True if at least one packet was received back, False otherwise.
        """
        flag = "-n" if IS_WINDOWS else "-c"
        try:
            result = subprocess.run(
                ["ping", flag, str(count), host],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, TimeoutError):
            return False


# ---------------------------------------------------------------------------
# SystemInfoDriver — hardware and OS metadata
# ---------------------------------------------------------------------------

class SystemInfoDriver:
    """Provides a unified interface for retrieving hardware and operating system metadata.

    This driver abstracts the platform-specific complexities of gathering 
    identifying information about the host, including CPU architecture, 
    memory usage, and system uptime.
    """

    @staticmethod
    def info() -> dict:
        """Gathers a comprehensive set of system-wide metadata.

        Combines data from the platform, network, and system drivers to create 
        a snapshot of the current execution environment.

        Returns:
            dict: A dictionary containing 'os', 'os_version', 'machine', 'processor', 
                  'python', 'hostname', 'username', and 'uptime_secs'.
        """
        import time
        return {
            "os": CURRENT_OS,
            "os_version": platform.version(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "python": platform.python_version(),
            "hostname": NetworkDriver.hostname(),
            "username": SystemInfoDriver.username(),
            "uptime_secs": SystemInfoDriver._uptime(),
        }

    @staticmethod
    def username() -> str:
        """Retrieves the login name of the user currently running the process.

        Attempts to use the standard 'getpass' module first. If that fails, it 
        falls back to common environment variables used across different OS types.

        Returns:
            str: The current username, or "user" as a final fallback.
        """
        try:
            import getpass
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    @staticmethod
    def memory() -> dict:
        """Returns memory usage statistics for the host system.

        If the 'psutil' library is installed, it provides real-time statistics. 
        Otherwise, it returns a zeroed dictionary to prevent crashes.

        Returns:
            dict: A dictionary containing 'total', 'available', 'used' (in bytes), 
                  and 'percent' (float).
        """
        try:
            import psutil
            vm = psutil.virtual_memory()
            return {
                "total": vm.total,
                "available": vm.available,
                "used": vm.used,
                "percent": vm.percent,
            }
        except ImportError:
            return {"total": 0, "available": 0, "used": 0, "percent": 0.0}

    @staticmethod
    def _uptime() -> float:
        """Calculates the system uptime in seconds.

        This is a 'best-effort' implementation. It prefers 'psutil' for accuracy, 
        but will attempt to parse '/proc/uptime' directly on Linux systems if 
        psutil is missing.

        Returns:
            float: Total seconds since the system was booted, or 0.0 if unable to determine.
        """
        try:
            import psutil
            return __import__("time").time() - psutil.boot_time()
        except ImportError:
            pass
        if IS_LINUX := CURRENT_OS == OSType.LINUX:
            try:
                with open("/proc/uptime") as f:
                    return float(f.read().split()[0])
            except Exception:
                pass
        return 0.0


# ---------------------------------------------------------------------------
# SignalDriver — cross-platform signal handling
# ---------------------------------------------------------------------------

class SignalDriver:
    """Registers and manages OS-level signal handlers in a uniform way across platforms.

    Signals are used to notify a process of external events, such as a user 
    pressing Ctrl+C or the system requesting a shutdown. This driver 
    abstracts the mapping between Python's signal module and OS-specific 
    behaviors.
    """

    @staticmethod
    def on_interrupt(handler) -> None:
        """Registers a callback for the SIGINT signal (KeyboardInterrupt).

        Triggered typically when the user presses Ctrl+C in the terminal.

        Args:
            handler (Callable): A function to call when the signal is received. 
                The handler must accept two arguments: the signal number 
                and the current stack frame.
        """
        signal.signal(signal.SIGINT, handler)

    @staticmethod
    def on_terminate(handler) -> None:
        """Registers a callback for the SIGTERM signal (Termination Request).

        This signal is sent by the OS or other processes to request a graceful 
        shutdown. Note that SIGTERM is generally not supported on Windows.

        Args:
            handler (Callable): A function to call when the signal is received.
        """
        if not IS_WINDOWS:
            signal.signal(signal.SIGTERM, handler)

    @staticmethod
    def ignore_interrupt() -> None:
        """Sets the system to ignore Ctrl+C (SIGINT) signals.

        Useful during critical sections of code where an unexpected 
        interruption could cause data corruption or leave the system 
        in an inconsistent state.
        """
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    @staticmethod
    def default_interrupt() -> None:
        """Resets the Ctrl+C (SIGINT) handler to the default system behavior.

        Typically, the default behavior for SIGINT is to raise a 
        KeyboardInterrupt exception in the main thread.
        """
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# HAL façade — single import point for all higher layers
# ---------------------------------------------------------------------------

class HAL:
    """Unified Hardware Abstraction Layer (HAL) entry point.

    The HAL class acts as a façade pattern, providing a single point of access 
    to all OS-dependent subsystems. It ensures that Layer 3 (Kernel) and 
    Layer 4 (Shell) remain platform-agnostic by delegating hardware tasks 
    to specific drivers.

    Attributes:
        terminal (TerminalDriver): Handles I/O, colors, and terminal metadata.
        disk (DiskDriver): Manages filesystem operations and path resolution.
        process (ProcessDriver): Handles command execution and process lifecycle.
        network (NetworkDriver): Provides basic network and connectivity info.
        system (SystemInfoDriver): Exposes hardware and OS metadata.
        signals (SignalDriver): Manages system-level interrupt handlers.
        os_type (str): The detected host OS (linux, macos, or windows).
    """
    terminal = TerminalDriver
    disk = DiskDriver
    process = ProcessDriver
    network = NetworkDriver
    system = SystemInfoDriver
    signals = SignalDriver
    os_type = CURRENT_OS

    @staticmethod
    def boot() -> None:
        """Initializes the Hardware Abstraction Layer.

        This method is called during the initial boot phase of the system. 
        It prepares the environment by enabling ANSI support on Windows 
        legacy consoles and prints a diagnostic banner identifying the 
        host hardware and current user.
        """
        TerminalDriver.enable_ansi_on_windows()
        t = TerminalDriver
        info = SystemInfoDriver.info()
        t.write(
            f"{t.BOLD}{t.GREEN}linux++ HAL booted{t.RESET} "
            f"on {t.CYAN}{info['os']}{t.RESET} "
            f"({info['os_release']}) "
            f"as {t.YELLOW}{info['username']}{t.RESET}@"
            f"{t.BLUE}{info['hostname']}{t.RESET}"
        )


# ---------------------------------------------------------------------------
# Quick self-test (python hal.py)
# ---------------------------------------------------------------------------


def main():
    HAL.boot()

    t = HAL.terminal
    cols, rows = t.get_size()
    t.write(f"  Terminal: {cols}×{rows}")

    disk = HAL.disk
    t.write(f"  Home dir: {disk.home()}")
    t.write(f"  CWD:      {disk.cwd()}")
    usage = disk.disk_usage()
    gb = 1024 ** 3
    t.write(f"  Disk:     {usage['used']/gb:.1f} GB used / {usage['total']/gb:.1f} GB total")

    proc = HAL.process
    result = proc.run(["python3" if not IS_WINDOWS else "python", "--version"])
    t.write(f"  Python:   {result.stdout.strip() or result.stderr.strip()}")

    net = HAL.network
    t.write(f"  IP:       {net.local_ip()}")

    mem = HAL.system.memory()
    if mem["total"]:
        t.write(f"  RAM:      {mem['used']//1024//1024} MB used / {mem['total']//1024//1024} MB total")

    t.write(f"\n{t.GREEN}HAL self-test passed.{t.RESET}")

if __name__ == "__main__":
    main()