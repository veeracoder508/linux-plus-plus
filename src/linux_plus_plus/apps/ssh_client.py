import os
import sys
import time
import threading
import socket
from typing import Optional

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager
except ImportError:
    IS_WINDOWS = os.name == "nt"


# ===========================================================================
# SSHClient  — interactive SSH (system binary on Unix, paramiko on Windows)
# ===========================================================================

class SSHClient:
    """
    SSH client with two backends:

    Backend 1 — system ssh binary (Unix primary):
        Uses os.execvp to replace the process image with ssh.
        Perfect PTY, terminal resize (SIGWINCH), Ctrl+C, colours — all work.
        No extra deps needed on any Unix/macOS system.

    Backend 2 — paramiko (Windows primary / Unix fallback):
        pip install paramiko
        Used when no system ssh binary is found.

    Usage:
        ssh [user@]host [port]          interactive session
        ssh [user@]host [port] <cmd>    run single remote command
    """

    def __init__(self, host: str, user: str, port: int = 22):
        self._host = host
        self._user = user
        self._port = port

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_interactive(self, command: Optional[str] = None) -> int:
        import shutil
        ssh_bin = shutil.which("ssh")

        # On Unix, system ssh is far more reliable for PTY handling
        if not IS_WINDOWS and ssh_bin:
            return self._system_ssh(ssh_bin, command)

        # Windows or no system ssh → paramiko
        try:
            import paramiko  # type: ignore
            return self._paramiko_session(paramiko, command)
        except ImportError:
            if ssh_bin:
                # Windows fallback: run as subprocess
                return self._system_ssh(ssh_bin, command)
            IOManager.error(
                "ssh: no ssh binary found and paramiko is not installed.\n"
                "     Install paramiko:  pip install paramiko"
            )
            return 1

    # ------------------------------------------------------------------
    # Backend 1 — system ssh binary
    # ------------------------------------------------------------------

    def _system_ssh(self, ssh_bin: str, command: Optional[str]) -> int:
        """
        On Unix: use os.execvp — replaces the current process image with ssh.
        This gives a perfect native terminal experience with no wrapper overhead.
        On Windows: use subprocess.run (execvp not available).
        """
        import subprocess

        cmd = [
            ssh_bin,
            "-p", str(self._port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30",
            f"{self._user}@{self._host}",
        ]
        if command:
            # pass remote command as separate args (not shell-quoted string)
            cmd += ["--"] + command.split()

        try:
            if IS_WINDOWS:
                result = subprocess.run(cmd)
                return result.returncode
            else:
                # execvp replaces this process — no subprocess overhead,
                # no I/O redirection, the real terminal is used directly
                os.execvp(ssh_bin, cmd)
                # execvp never returns on success
        except FileNotFoundError:
            IOManager.error(f"ssh: binary not found: {ssh_bin}")
            return 127
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            IOManager.error(f"ssh: {e}")
            return 1
        return 0

    # ------------------------------------------------------------------
    # Backend 2 — paramiko
    # ------------------------------------------------------------------

    def _paramiko_session(self, paramiko, command: Optional[str]) -> int:
        import getpass

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # try key auth first, then password on second attempt
        connected = False
        for attempt in range(2):
            try:
                kw = dict(
                    hostname=self._host,
                    port=self._port,
                    username=self._user,
                    timeout=15,
                    look_for_keys=(attempt == 0),
                    allow_agent=(attempt == 0),
                )
                if attempt == 1:
                    kw["password"] = getpass.getpass(
                        f"{self._user}@{self._host}'s password: "
                    )
                    kw["look_for_keys"] = False
                client.connect(**kw)
                connected = True
                break
            except paramiko.AuthenticationException:
                if attempt == 1:
                    IOManager.error("ssh: authentication failed")
                    return 1
            except paramiko.SSHException as e:
                IOManager.error(f"ssh: SSH error: {e}")
                return 1
            except socket.timeout:
                IOManager.error(f"ssh: connection timed out: {self._host}")
                return 1
            except OSError as e:
                IOManager.error(f"ssh: {e}")
                return 1

        if not connected:
            return 1

        IOManager.write(
            f"Connected to {self._user}@{self._host}:{self._port} "
            f"(paramiko {paramiko.__version__})"
        )
        try:
            if command:
                return self._paramiko_exec(client, command)
            else:
                return self._paramiko_pty(client)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _paramiko_exec(self, client, command: str) -> int:
        try:
            _, stdout, stderr = client.exec_command(command, get_pty=False)
            while True:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            err = stderr.read().decode("utf-8", errors="replace")
            if err:
                sys.stderr.write(err)
            return stdout.channel.recv_exit_status()
        except Exception as e:
            IOManager.error(f"ssh exec: {e}")
            return 1

    def _paramiko_pty(self, client) -> int:
        if IS_WINDOWS:
            return self._paramiko_pty_windows(client)
        return self._paramiko_pty_unix(client)

    def _paramiko_pty_unix(self, client) -> int:
        import termios, tty, select as _sel, signal as _sig

        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            cols, rows = 80, 24

        chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        chan.settimeout(0.0)

        fd      = sys.stdin.fileno()
        old_tty = termios.tcgetattr(fd)

        def _resize(*_):
            try:
                c, r = os.get_terminal_size()
                chan.resize_pty(width=c, height=r)
            except Exception:
                pass

        old_winch = _sig.getsignal(_sig.SIGWINCH)
        _sig.signal(_sig.SIGWINCH, _resize)

        try:
            tty.setraw(fd)
            while True:
                r, _, _ = _sel.select([chan, sys.stdin], [], [], 0.5)
                if chan in r:
                    try:
                        data = chan.recv(4096)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    except Exception:
                        break
                if sys.stdin in r:
                    try:
                        key = os.read(fd, 256)
                        if not key:
                            break
                        chan.sendall(key)
                    except Exception:
                        break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
            _sig.signal(_sig.SIGWINCH, old_winch)

        sys.stdout.write("\r\nConnection closed.\r\n")
        sys.stdout.flush()
        return 0

    def _paramiko_pty_windows(self, client) -> int:
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            cols, rows = 80, 24

        chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        chan.settimeout(0.0)
        stop = threading.Event()

        def _send():
            try:
                while not stop.is_set():
                    data = sys.stdin.buffer.read(1)
                    if not data:
                        break
                    chan.sendall(data)
            finally:
                stop.set()

        threading.Thread(target=_send, daemon=True).start()
        try:
            while not stop.is_set():
                if chan.recv_ready():
                    data = chan.recv(4096)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                elif chan.closed:
                    break
                else:
                    time.sleep(0.01)
        except KeyboardInterrupt:
            chan.send(b"\x03")
        finally:
            stop.set()

        sys.stdout.write("\r\nConnection closed.\r\n")
        sys.stdout.flush()
        return 0
    