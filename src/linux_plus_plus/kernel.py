"""
linux++ — Kernel (Layer 3)
===========================
Pure Python standard library only. No third-party deps.
Depends on Layer 1 (HAL) and Layer 2 (Stdlib).

Subsystems:
  - VirtualFilesystem (VFS)  : path resolution, inode tree, mounts
  - ProcessManager           : process tracking, jobs, fork/exec/wait
  - SyscallRouter            : single dispatch point for all kernel calls
"""

import os
import sys
import time
import threading
import subprocess
import stat
from typing import Optional, Callable
from enum import Enum, auto

try:
    from .hal import HAL, IS_WINDOWS
    from .stdlib import Stdlib, IOManager, EnvManager, SignalHandler, AliasStore
except ImportError:
    IS_WINDOWS = os.name == "nt"
    HAL = None
    Stdlib = None


# ===========================================================================
# VirtualFilesystem (VFS)
# ===========================================================================

class INodeType(Enum):
    FILE    = auto()
    DIR     = auto()
    SYMLINK = auto()
    MOUNT   = auto()


class INode:
    """
    Lightweight inode — metadata only, no data stored in memory.
    Data lives on the real host filesystem; we track the path and type.
    """
    __slots__ = ("path", "inode_type", "mount_point")

    def __init__(self, path: str, inode_type: INodeType, mount_point: str = ""):
        self.path        = path
        self.inode_type  = inode_type
        self.mount_point = mount_point

    def __repr__(self):
        return f"<INode {self.inode_type.name} {self.path!r}>"


class VirtualFilesystem:
    """
    Maintains the linux++ view of the filesystem:
      - current working directory (cwd)
      - mount table (virtual path -> real path)
      - path resolution (handles . .. ~ symlinks)
      - directory listing with inode info
      - basic file operations routed through os.*

    Uses only: os, stat — pure CPython builtins.
    """

    def __init__(self):
        self._cwd: str = os.path.expanduser("~")
        self._mounts: dict[str, str] = {"/": "/"}   # virtual -> real
        self._lock = threading.Lock()

    # --- cwd ---

    @property
    def cwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        resolved = self.resolve(path)
        if not os.path.isdir(resolved):
            raise FileNotFoundError(f"cd: no such directory: {path}")
        with self._lock:
            self._cwd = resolved
            os.chdir(resolved)

    # --- path resolution ---

    def resolve(self, path: str) -> str:
        """
        Resolve a path to an absolute real path.
        Handles: ~  .  ..  relative paths  symlinks
        """
        path = path.strip()
        if not path:
            return self._cwd

        # tilde expansion
        if path.startswith("~"):
            path = os.path.expanduser(path)

        # make absolute
        if not os.path.isabs(path):
            path = os.path.join(self._cwd, path)

        # normalise . and ..
        path = os.path.normpath(path)

        # resolve symlinks
        try:
            path = os.path.realpath(path)
        except OSError:
            pass

        return path

    def resolve_virtual(self, vpath: str) -> str:
        """
        Translate a virtual mount path to its real host path.
        e.g. if /data is mounted at /mnt/usb, /data/file -> /mnt/usb/file
        """
        for virtual, real in sorted(self._mounts.items(), reverse=True):
            if vpath.startswith(virtual):
                suffix = vpath[len(virtual):]
                return os.path.normpath(real + suffix)
        return vpath

    # --- mounts ---

    def mount(self, virtual_path: str, real_path: str) -> None:
        """Mount real_path at virtual_path."""
        real_path = os.path.realpath(real_path)
        if not os.path.isdir(real_path):
            raise FileNotFoundError(f"mount: {real_path} is not a directory")
        with self._lock:
            self._mounts[virtual_path] = real_path

    def umount(self, virtual_path: str) -> None:
        if virtual_path == "/":
            raise PermissionError("Cannot unmount root")
        self._mounts.pop(virtual_path, None)

    def mounts(self) -> dict[str, str]:
        return dict(self._mounts)

    # --- inode lookup ---

    def stat(self, path: str) -> INode:
        """Return an INode for path. Raises FileNotFoundError if missing."""
        resolved = self.resolve(path)
        st = os.stat(resolved)
        mode = st.st_mode

        if stat.S_ISLNK(mode):
            inode_type = INodeType.SYMLINK
        elif stat.S_ISDIR(mode):
            inode_type = INodeType.DIR
        else:
            inode_type = INodeType.FILE

        return INode(resolved, inode_type)

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except (FileNotFoundError, OSError):
            return False

    # --- directory listing ---

    def listdir(self, path: str = ".") -> list[INode]:
        """List directory contents as INodes."""
        resolved = self.resolve(path)
        entries = []
        try:
            for name in os.listdir(resolved):
                full = os.path.join(resolved, name)
                try:
                    st = os.lstat(full)
                    mode = st.st_mode
                    if stat.S_ISLNK(mode):
                        t = INodeType.SYMLINK
                    elif stat.S_ISDIR(mode):
                        t = INodeType.DIR
                    else:
                        t = INodeType.FILE
                    entries.append(INode(full, t))
                except OSError:
                    continue
        except PermissionError:
            raise PermissionError(f"ls: cannot open directory '{path}': Permission denied")
        return sorted(entries, key=lambda n: (n.inode_type != INodeType.DIR, os.path.basename(n.path).lower()))

    # --- file operations ---

    def mkdir(self, path: str, parents: bool = False) -> None:
        resolved = self.resolve(path)
        if parents:
            os.makedirs(resolved, exist_ok=True)
        else:
            os.mkdir(resolved)

    def remove(self, path: str, recursive: bool = False) -> None:
        resolved = self.resolve(path)
        if os.path.isdir(resolved):
            if not recursive:
                os.rmdir(resolved)          # fails if non-empty — correct
            else:
                import shutil
                shutil.rmtree(resolved)
        else:
            os.unlink(resolved)

    def rename(self, src: str, dst: str) -> None:
        os.rename(self.resolve(src), self.resolve(dst))

    def copy(self, src: str, dst: str) -> None:
        import shutil
        shutil.copy2(self.resolve(src), self.resolve(dst))

    def touch(self, path: str) -> None:
        resolved = self.resolve(path)
        fd = os.open(resolved, os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)

    def read(self, path: str, encoding: str = "utf-8") -> str:
        resolved = self.resolve(path)
        fd = os.open(resolved, os.O_RDONLY)
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

    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        resolved = self.resolve(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(resolved, flags, 0o644)
        try:
            os.write(fd, content.encode(encoding))
        finally:
            os.close(fd)

    def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        resolved = self.resolve(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(resolved, flags, 0o644)
        try:
            os.write(fd, content.encode(encoding))
        finally:
            os.close(fd)


# ===========================================================================
# ProcessManager
# ===========================================================================

class ProcessState(Enum):
    RUNNING    = auto()
    STOPPED    = auto()
    DONE       = auto()
    FAILED     = auto()


class ManagedProcess:
    """Wrapper around a subprocess.Popen with job-control metadata."""

    _id_counter = 0
    _lock = threading.Lock()

    def __init__(self, popen: subprocess.Popen, command: list[str], background: bool = False):
        with ManagedProcess._lock:
            ManagedProcess._id_counter += 1
            self.job_id = ManagedProcess._id_counter

        self.popen      = popen
        self.command    = command
        self.background = background
        self.state      = ProcessState.RUNNING
        self.started_at = time.time()
        self.stdout_buf: list[str] = []
        self.stderr_buf: list[str] = []
        self._collector: Optional[threading.Thread] = None

        if background:
            self._start_collector()

    def _start_collector(self) -> None:
        """Background thread that drains stdout/stderr into buffers."""
        def collect():
            for line in self.popen.stdout:
                self.stdout_buf.append(line)
            self.popen.wait()
            self.state = (ProcessState.DONE
                          if self.popen.returncode == 0
                          else ProcessState.FAILED)

        self._collector = threading.Thread(target=collect, daemon=True)
        self._collector.start()

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def returncode(self) -> Optional[int]:
        return self.popen.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        rc = self.popen.returncode or 0
        self.state = ProcessState.DONE if rc == 0 else ProcessState.FAILED
        return rc

    def kill(self, force: bool = False) -> None:
        try:
            if IS_WINDOWS:
                self.popen.terminate()
            else:
                import signal as _signal
                sig = _signal.SIGKILL if force else _signal.SIGTERM
                os.kill(self.pid, sig)
        except ProcessLookupError:
            pass
        self.state = ProcessState.DONE

    def __repr__(self):
        cmd = " ".join(self.command)
        return f"[{self.job_id}] {self.state.name:<8} {self.pid}  {cmd}"


class ProcessManager:
    """
    Tracks all processes spawned by linux++.
    Provides: run (foreground), spawn (background), kill, jobs, wait.
    Uses only: os, subprocess, threading, signal — pure Python builtins.
    """

    def __init__(self, vfs: VirtualFilesystem):
        self._vfs  = vfs
        self._jobs: dict[int, ManagedProcess] = {}
        self._lock = threading.Lock()

    # --- run foreground ---

    def run(
        self,
        command: list[str],
        stdin:   Optional[str]  = None,
        capture: bool           = False,
        env:     Optional[dict] = None,
        timeout: Optional[float]= None,
    ) -> tuple[int, str, str]:
        """
        Run a command in the foreground.
        Returns (returncode, stdout, stderr).
        stdin: optional string piped to process stdin.
        capture: if True, capture stdout/stderr instead of inheriting terminal.
        """
        merged_env = {**os.environ, **(env or {})}
        kwargs: dict = dict(
            cwd=self._vfs.cwd,
            env=merged_env,
            shell=IS_WINDOWS,
        )

        if capture or stdin is not None:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
            kwargs["stdin"]  = subprocess.PIPE if stdin else None
            kwargs["text"]   = True

        try:
            proc = subprocess.run(
                command,
                **kwargs,
                timeout=timeout,
                input=stdin,
            )
            return (
                proc.returncode,
                proc.stdout or "",
                proc.stderr or "",
            )
        except FileNotFoundError:
            return (127, "", f"{command[0]}: command not found")
        except subprocess.TimeoutExpired:
            return (124, "", "process timed out")
        except KeyboardInterrupt:
            return (130, "", "")

    # --- spawn background ---

    def spawn(
        self,
        command: list[str],
        env: Optional[dict] = None,
    ) -> ManagedProcess:
        """
        Spawn a background job.
        Returns a ManagedProcess. Job is tracked in self._jobs.
        """
        merged_env = {**os.environ, **(env or {})}
        popen = subprocess.Popen(
            command,
            cwd=self._vfs.cwd,
            env=merged_env,
            shell=IS_WINDOWS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        mp = ManagedProcess(popen, command, background=True)
        with self._lock:
            self._jobs[mp.job_id] = mp
        IOManager.write(f"[{mp.job_id}] {mp.pid}")
        return mp

    # --- pipeline ---

    def pipeline(
        self,
        commands: list[list[str]],
        env: Optional[dict] = None,
    ) -> tuple[int, str, str]:
        """
        Run a pipeline: cmd1 | cmd2 | cmd3
        Returns (returncode, final_stdout, final_stderr).
        Uses os.pipe() to wire stdout -> stdin between stages.
        """
        if not commands:
            return (0, "", "")
        if len(commands) == 1:
            return self.run(commands[0], capture=True, env=env)

        merged_env = {**os.environ, **(env or {})}
        procs: list[subprocess.Popen] = []
        prev_stdout = None

        for i, cmd in enumerate(commands):
            is_last = (i == len(commands) - 1)
            proc = subprocess.Popen(
                cmd,
                cwd=self._vfs.cwd,
                env=merged_env,
                shell=IS_WINDOWS,
                stdin=prev_stdout,
                stdout=subprocess.PIPE if not is_last else subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if prev_stdout:
                prev_stdout.close()
            prev_stdout = proc.stdout
            procs.append(proc)

        # Collect output of the last process
        stdout, stderr = procs[-1].communicate()
        for p in procs[:-1]:
            p.wait()

        rc = procs[-1].returncode or 0
        return (rc, stdout, stderr)

    # --- job control ---

    def jobs(self) -> list[ManagedProcess]:
        """Return all tracked jobs."""
        self._reap()
        with self._lock:
            return list(self._jobs.values())

    def kill_job(self, job_id: int, force: bool = False) -> bool:
        with self._lock:
            mp = self._jobs.get(job_id)
        if not mp:
            return False
        mp.kill(force=force)
        return True

    def wait_job(self, job_id: int, timeout: Optional[float] = None) -> Optional[int]:
        with self._lock:
            mp = self._jobs.get(job_id)
        if not mp:
            return None
        return mp.wait(timeout=timeout)

    def _reap(self) -> None:
        """Remove finished jobs from the table."""
        with self._lock:
            done = [
                jid for jid, mp in self._jobs.items()
                if mp.popen.poll() is not None
            ]
            for jid in done:
                mp = self._jobs.pop(jid)
                mp.state = (ProcessState.DONE
                            if mp.popen.returncode == 0
                            else ProcessState.FAILED)
                IOManager.write(f"[{mp.job_id}] {mp.state.name}  {' '.join(mp.command)}")


# ===========================================================================
# SyscallRouter
# ===========================================================================

class SyscallError(Exception):
    def __init__(self, syscall: str, message: str, errno: int = 1):
        super().__init__(message)
        self.syscall = syscall
        self.errno   = errno


class SyscallRouter:
    """
    The single dispatch point for all kernel operations.
    Higher layers (shell, builtins) call Kernel.syscall.*
    and never touch VFS or ProcessManager directly.

    Every method returns a SyscallResult.
    """

    def __init__(self, vfs: VirtualFilesystem, pm: ProcessManager):
        self._vfs = vfs
        self._pm  = pm

    # --- filesystem syscalls ---

    def open(self, path: str, mode: str = "r") -> str:
        """Read a file and return its contents."""
        try:
            return self._vfs.read(path)
        except FileNotFoundError:
            raise SyscallError("open", f"No such file: {path}", 2)
        except PermissionError:
            raise SyscallError("open", f"Permission denied: {path}", 13)

    def write(self, path: str, content: str, append: bool = False) -> None:
        try:
            if append:
                self._vfs.append(path, content)
            else:
                self._vfs.write(path, content)
        except PermissionError:
            raise SyscallError("write", f"Permission denied: {path}", 13)

    def unlink(self, path: str, recursive: bool = False) -> None:
        try:
            self._vfs.remove(path, recursive=recursive)
        except FileNotFoundError:
            raise SyscallError("unlink", f"No such file: {path}", 2)
        except OSError as e:
            raise SyscallError("unlink", str(e), 1)

    def stat(self, path: str) -> INode:
        try:
            return self._vfs.stat(path)
        except FileNotFoundError:
            raise SyscallError("stat", f"No such file: {path}", 2)

    def chdir(self, path: str) -> None:
        try:
            self._vfs.chdir(path)
        except FileNotFoundError:
            raise SyscallError("chdir", f"No such directory: {path}", 2)

    def mkdir(self, path: str, parents: bool = False) -> None:
        try:
            self._vfs.mkdir(path, parents=parents)
        except FileExistsError:
            raise SyscallError("mkdir", f"Already exists: {path}", 17)
        except FileNotFoundError:
            raise SyscallError("mkdir", f"No such directory: {path}", 2)

    def listdir(self, path: str = ".") -> list[INode]:
        try:
            return self._vfs.listdir(path)
        except FileNotFoundError:
            raise SyscallError("listdir", f"No such directory: {path}", 2)
        except PermissionError as e:
            raise SyscallError("listdir", str(e), 13)

    def rename(self, src: str, dst: str) -> None:
        try:
            self._vfs.rename(src, dst)
        except FileNotFoundError:
            raise SyscallError("rename", f"No such file: {src}", 2)

    def copy(self, src: str, dst: str) -> None:
        try:
            self._vfs.copy(src, dst)
        except FileNotFoundError:
            raise SyscallError("copy", f"No such file: {src}", 2)

    def touch(self, path: str) -> None:
        self._vfs.touch(path)

    # --- process syscalls ---

    def exec(
        self,
        command:  list[str],
        stdin:    Optional[str]   = None,
        capture:  bool            = False,
        env:      Optional[dict]  = None,
        timeout:  Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Execute a command. Returns (returncode, stdout, stderr)."""
        return self._pm.run(command, stdin=stdin, capture=capture,
                            env=env, timeout=timeout)

    def fork(self, command: list[str], env: Optional[dict] = None) -> ManagedProcess:
        """Spawn a background job. Returns ManagedProcess."""
        return self._pm.spawn(command, env=env)

    def pipe_exec(
        self,
        commands: list[list[str]],
        env: Optional[dict] = None,
    ) -> tuple[int, str, str]:
        """Execute a pipeline. Returns (returncode, stdout, stderr)."""
        return self._pm.pipeline(commands, env=env)

    def kill(self, job_id: int, force: bool = False) -> bool:
        return self._pm.kill_job(job_id, force=force)

    def wait(self, job_id: int, timeout: Optional[float] = None) -> Optional[int]:
        return self._pm.wait_job(job_id, timeout=timeout)

    def jobs(self) -> list[ManagedProcess]:
        return self._pm.jobs()

    # --- env syscalls ---

    def getenv(self, key: str, default: str = "") -> str:
        return EnvManager.get(key, default)

    def setenv(self, key: str, value: str, export: bool = False) -> None:
        EnvManager.set(key, value, export=export)

    def getcwd(self) -> str:
        return self._vfs.cwd

    def gethome(self) -> str:
        return os.path.expanduser("~")


# ===========================================================================
# Kernel — the façade
# ===========================================================================

class Kernel:
    """
    linux++ Kernel.

    Usage:
        from kernel import Kernel
        k = Kernel()
        k.boot()
        rc, out, err = k.syscall.exec(["ls", "-la"])
    """

    def __init__(self):
        self.vfs     = VirtualFilesystem()
        self.pm      = ProcessManager(self.vfs)
        self.syscall = SyscallRouter(self.vfs, self.pm)

    def boot(self) -> None:
        """Initialize the kernel subsystems."""
        # Register default SIGINT handler
        def _on_interrupt(sig, frame):
            IOManager.write("")   # newline after ^C

        SignalHandler.on_ctrl_c(_on_interrupt)
        IOManager.write(
            f"linux++ kernel ready  |  "
            f"cwd={self.vfs.cwd}  |  "
            f"pid={os.getpid()}"
        )

    def shutdown(self) -> None:
        """Gracefully kill all background jobs."""
        for job in self.pm.jobs():
            job.kill()


# ===========================================================================
# Quick self-test  (python kernel.py)
# ===========================================================================

def main():
    k = Kernel()
    k.boot()

    sc = k.syscall
    io = IOManager

    io.write("\n=== linux++ kernel self-test ===\n")

    # VFS — cwd and chdir
    home = sc.gethome()
    sc.chdir(home)
    assert sc.getcwd() == os.path.realpath(home)
    io.write(f"[VFS]     chdir to home:     OK  ({sc.getcwd()})")

    # VFS — mkdir / touch / stat / unlink
    tmp_dir = os.path.join(home, "_lpp_kernel_test")
    sc.mkdir(tmp_dir)
    tmp_file = os.path.join(tmp_dir, "hello.txt")
    sc.touch(tmp_file)
    sc.write(tmp_file, "kernel test\n")
    node = sc.stat(tmp_file)
    assert node.inode_type == INodeType.FILE
    content = sc.open(tmp_file)
    assert "kernel test" in content
    sc.unlink(tmp_dir, recursive=True)
    io.write(f"[VFS]     mkdir/touch/write/stat/unlink: OK")

    # VFS — listdir
    entries = sc.listdir(home)
    io.write(f"[VFS]     listdir home:      OK  ({len(entries)} entries)")

    # ProcessManager — foreground exec
    py = "python3" if not IS_WINDOWS else "python"
    rc, out, err = sc.exec([py, "--version"], capture=True)
    assert rc == 0
    io.write(f"[ProcMgr] exec python:        OK  ({(out or err).strip()})")

    # ProcessManager — pipeline
    if not IS_WINDOWS:
        rc, out, err = sc.pipe_exec([["echo", "hello world"], ["cat"]])
        assert "hello" in out
        io.write(f"[ProcMgr] pipeline echo|cat:  OK  ({out.strip()})")

    # ProcessManager — background job
    job = sc.fork([py, "-c", "import time; time.sleep(0.2)"])
    io.write(f"[ProcMgr] background job:     OK  (job={job.job_id} pid={job.pid})")
    sc.wait(job.job_id, timeout=2)

    # SyscallError
    try:
        sc.open("/this/does/not/exist")
    except SyscallError as e:
        assert e.errno == 2
        io.write(f"[Syscall] error handling:     OK  (errno={e.errno})")

    io.write("\nAll kernel tests passed.")

    k.shutdown()

if __name__ == "__main__":
    main()