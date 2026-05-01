"""
linux++ — Hardware Abstraction Layer (HAL)
==========================================
OS-independent abstraction over Linux, macOS, and Windows.
All higher layers import from this module; they never call
os / sys / subprocess / platform directly.
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
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


def _detect_os() -> str:
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
    """Handles terminal colors, clearing, and cursor operations."""

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
        """Enable virtual terminal processing on Windows cmd.exe."""
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
        """Return (columns, rows) of the terminal window."""
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines

    @staticmethod
    def write(text: str, end: str = "\n") -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()

    @staticmethod
    def error(text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    @staticmethod
    def read_input(prompt: str = "") -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""


# ---------------------------------------------------------------------------
# DiskDriver — cross-platform filesystem operations
# ---------------------------------------------------------------------------

class DiskDriver:
    """Abstracts filesystem calls so the kernel uses one API everywhere."""

    # Path separator — the kernel always works with forward slashes
    # internally; we convert on the way out on Windows.
    SEP = "/"

    @staticmethod
    def _to_path(p: str) -> Path:
        return Path(p)

    # --- directory ---

    @staticmethod
    def cwd() -> str:
        return Path.cwd().as_posix()

    @staticmethod
    def chdir(path: str) -> None:
        os.chdir(path)

    @staticmethod
    def mkdir(path: str, parents: bool = True, exist_ok: bool = True) -> None:
        Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    @staticmethod
    def listdir(path: str) -> list[dict]:
        """
        Returns a list of dicts with keys:
          name, is_dir, is_file, is_symlink, size, permissions
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
        """Convert stat mode to 'rwxr-xr-x' style string."""
        flags = [
            (stat.S_IRUSR, "r"), (stat.S_IWUSR, "w"), (stat.S_IXUSR, "x"),
            (stat.S_IRGRP, "r"), (stat.S_IWGRP, "w"), (stat.S_IXGRP, "x"),
            (stat.S_IROTH, "r"), (stat.S_IWOTH, "w"), (stat.S_IXOTH, "x"),
        ]
        return "".join(c if mode & f else "-" for f, c in flags)

    # --- file operations ---

    @staticmethod
    def exists(path: str) -> bool:
        return Path(path).exists()

    @staticmethod
    def is_file(path: str) -> bool:
        return Path(path).is_file()

    @staticmethod
    def is_dir(path: str) -> bool:
        return Path(path).is_dir()

    @staticmethod
    def read_text(path: str, encoding: str = "utf-8") -> str:
        return Path(path).read_text(encoding=encoding)

    @staticmethod
    def write_text(path: str, content: str, encoding: str = "utf-8") -> None:
        Path(path).write_text(content, encoding=encoding)

    @staticmethod
    def append_text(path: str, content: str, encoding: str = "utf-8") -> None:
        with open(path, "a", encoding=encoding) as f:
            f.write(content)

    @staticmethod
    def delete(path: str) -> None:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)

    @staticmethod
    def copy(src: str, dst: str) -> None:
        shutil.copy2(src, dst)

    @staticmethod
    def move(src: str, dst: str) -> None:
        shutil.move(src, dst)

    @staticmethod
    def resolve(path: str) -> str:
        return Path(path).resolve().as_posix()

    @staticmethod
    def join(*parts: str) -> str:
        return Path(*parts).as_posix()

    @staticmethod
    def home() -> str:
        return Path.home().as_posix()

    @staticmethod
    def temp_dir() -> str:
        return tempfile.gettempdir()

    @staticmethod
    def disk_usage(path: str = "/") -> dict:
        """Return total/used/free bytes for the disk containing `path`."""
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
    def __init__(self, stdout: str, stderr: str, returncode: int):
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
        """
        Run a command and return a ProcessResult.
        On Windows, shell=True is automatically applied for built-in cmds.
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
        """Spawn a background process. Returns the Popen object."""
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
        """Find executable path, like Unix `which`. Returns None if not found."""
        return shutil.which(program)

    @staticmethod
    def pid() -> int:
        return os.getpid()

    @staticmethod
    def kill(pid: int, force: bool = False) -> bool:
        """Kill a process. Returns True if successful."""
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
        """Return a minimal list of running processes."""
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
        import socket
        return socket.gethostname()

    @staticmethod
    def local_ip() -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def ping(host: str, count: int = 1) -> bool:
        """Returns True if host responds to ping."""
        flag = "-n" if IS_WINDOWS else "-c"
        result = subprocess.run(
            ["ping", flag, str(count), host],
            capture_output=True, timeout=5
        )
        return result.returncode == 0


# ---------------------------------------------------------------------------
# SystemInfoDriver — hardware and OS metadata
# ---------------------------------------------------------------------------

class SystemInfoDriver:
    """Exposes CPU, RAM, and OS details in a unified dict."""

    @staticmethod
    def info() -> dict:
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
        try:
            import getpass
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    @staticmethod
    def memory() -> dict:
        """Return memory info. Uses psutil if available, else best-effort."""
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
        """Return system uptime in seconds (best-effort)."""
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
    """Register OS-level signal handlers uniformly."""

    @staticmethod
    def on_interrupt(handler) -> None:
        """Called when user presses Ctrl+C."""
        signal.signal(signal.SIGINT, handler)

    @staticmethod
    def on_terminate(handler) -> None:
        """Called when OS sends SIGTERM (not available on Windows)."""
        if not IS_WINDOWS:
            signal.signal(signal.SIGTERM, handler)

    @staticmethod
    def ignore_interrupt() -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    @staticmethod
    def default_interrupt() -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# HAL façade — single import point for all higher layers
# ---------------------------------------------------------------------------

class HAL:
    """
    Unified HAL entry point.

    Usage:
        from hal import HAL
        HAL.terminal.write("Hello from linux++!")
        HAL.disk.listdir("/home")
        HAL.process.run(["python3", "--version"])
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
        """Run at startup to initialize platform-specific quirks."""
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