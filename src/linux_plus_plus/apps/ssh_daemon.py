import os
import sys
import socket
import threading
import hashlib
from typing import Optional

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager, EnvManager
    from ..shell import Shell as Shell
except ImportError:
    IS_WINDOWS = None


#===========================================================================
# SSHDaemon  — SSH server using paramiko
# ===========================================================================

class SSHDaemon:
    """
    A real SSH server built on paramiko's Transport layer.

    Features:
      - RSA host key (auto-generated, saved to ~/.linuxpp/ssh/host_rsa_key)
      - Password auth via OS database (PAM → spwd → single-user fallback)
      - Public key auth via ~/.ssh/authorized_keys
      - Each client gets an isolated linux++ shell session
      - Runs in a background daemon thread — non-blocking
      - Multiple concurrent clients supported

    Commands:
      sshd start [port]   start (default port 2222)
      sshd stop           stop and disconnect all clients
      sshd status         show port and connected clients
      sshd keygen         regenerate the host key

    Requires: pip install paramiko
    """

    KEY_DIR       = os.path.join(os.path.expanduser("~"), ".linuxpp", "ssh")
    HOST_KEY_PATH = os.path.join(KEY_DIR, "host_rsa_key")
    DEFAULT_PORT  = 2222
    BANNER        = b"linux++ sshd\r\n"

    def __init__(self, shell: "Shell"):
        self._shell       = shell
        self._port        = self.DEFAULT_PORT
        self._server_sock: Optional[socket.socket] = None
        self._thread:      Optional[threading.Thread] = None
        self._running     = False
        self._clients:    list[dict] = []
        self._host_key    = None
        self._lock        = threading.Lock()

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    def start(self, port: int = DEFAULT_PORT) -> int:
        if self._running:
            IOManager.error(f"sshd: already running on port {self._port}")
            return 1
        try:
            import paramiko  # type: ignore
        except ImportError:
            IOManager.error(
                "sshd: paramiko is required.\n"
                "      pip install paramiko"
            )
            return 1

        self._port     = port
        self._host_key = self._load_or_generate_key(paramiko)
        if self._host_key is None:
            return 1

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(10)
            sock.settimeout(1.0)
            self._server_sock = sock
        except OSError as e:
            IOManager.error(f"sshd: cannot bind port {port}: {e}")
            return 1

        self._running = True
        self._thread  = threading.Thread(
            target=self._accept_loop,
            args=(paramiko,),
            daemon=True,
            name="sshd-accept",
        )
        self._thread.start()

        IOManager.write(
            f"sshd: listening on 0.0.0.0:{port}\n"
            f"sshd: host key fingerprint: {self._fingerprint(self._host_key)}\n"
            f"sshd: connect with:  ssh {EnvManager.username()}@localhost -p {port}"
        )
        return 0

    def stop(self) -> int:
        if not self._running:
            IOManager.error("sshd: not running")
            return 1
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        with self._lock:
            for c in self._clients:
                try:
                    c["transport"].close()
                except Exception:
                    pass
            self._clients.clear()
        IOManager.write("sshd: stopped")
        return 0

    def status(self) -> int:
        if not self._running:
            IOManager.write("sshd: not running")
            return 0
        with self._lock:
            clients = list(self._clients)
        IOManager.write(
            f"sshd: running on port {self._port}\n"
            f"sshd: {len(clients)} client(s) connected"
        )
        for c in clients:
            addr = c.get("addr", ("?", 0))
            user = c.get("user", "?")
            IOManager.write(f"       {user}@{addr[0]}:{addr[1]}")
        return 0

    def keygen(self) -> int:
        try:
            import paramiko  # type: ignore
        except ImportError:
            IOManager.error("sshd: paramiko required — pip install paramiko")
            return 1
        os.makedirs(self.KEY_DIR, exist_ok=True)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(self.HOST_KEY_PATH)
        if not IS_WINDOWS:
            os.chmod(self.HOST_KEY_PATH, 0o600)
        IOManager.write(
            f"sshd: new host key → {self.HOST_KEY_PATH}\n"
            f"sshd: fingerprint:   {self._fingerprint(key)}"
        )
        return 0

    # ------------------------------------------------------------------
    # Accept loop
    # ------------------------------------------------------------------

    def _accept_loop(self, paramiko) -> None:
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client,
                args=(paramiko, conn, addr),
                daemon=True,
                name=f"sshd-{addr[0]}:{addr[1]}",
            ).start()

    # ------------------------------------------------------------------
    # Per-client handler
    # ------------------------------------------------------------------

    def _handle_client(self, paramiko, conn: socket.socket, addr: tuple) -> None:
        transport = None
        iface     = _SSHServerInterface()
        entry     = {"addr": addr, "transport": None, "user": "?"}

        try:
            transport = paramiko.Transport(conn)
            transport.add_server_key(self._host_key)
            transport.start_server(server=iface)

            entry["transport"] = transport
            with self._lock:
                self._clients.append(entry)

            chan = transport.accept(30)
            if chan is None:
                return

            iface.shell_event.wait(10)
            entry["user"] = iface.username or "?"

            if iface.exec_command:
                self._run_command(chan, iface.exec_command)
            else:
                self._run_shell(chan)

        except Exception:
            pass
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._clients[:] = [c for c in self._clients if c is not entry]

    def _run_command(self, chan, command: str) -> None:
        """Execute one command in a fresh shell and send output back."""
        import io as _io
        from ..kernel import Kernel
        from ..shell  import Shell as _Shell

        k  = Kernel()
        sh = _Shell(k)
        buf = _io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            rc = sh.execute_line(command)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        output = buf.getvalue().replace("\n", "\r\n")
        try:
            if output:
                chan.send(output.encode("utf-8", errors="replace"))
            chan.send_exit_status(rc)
        except Exception:
            pass
        finally:
            chan.close()

    def _run_shell(self, chan) -> None:
        """Interactive shell session over the SSH channel."""
        import io as _io
        import re as _re
        from ..kernel import Kernel
        from ..shell  import Shell as _Shell

        k  = Kernel()
        sh = _Shell(k)

        chan.send(self.BANNER)

        try:
            while sh.running:
                # build and strip ANSI from prompt (client renders its own)
                raw_prompt = sh._prompt()
                plain_prompt = _re.sub(r'\033\[[0-9;]*[mGKHF]', '', raw_prompt)
                # also strip readline \001..\002 markers
                plain_prompt = plain_prompt.replace("\001", "").replace("\002", "")
                chan.send(plain_prompt.encode("utf-8", errors="replace"))

                # read one line char-by-char
                line = ""
                while True:
                    try:
                        data = chan.recv(1)
                    except Exception:
                        return
                    if not data:
                        return
                    ch = data.decode("utf-8", errors="replace")

                    if ch in ("\r", "\n"):
                        chan.send(b"\r\n")
                        break
                    elif ch == "\x03":       # Ctrl+C
                        chan.send(b"^C\r\n")
                        line = ""
                        break
                    elif ch == "\x04":       # Ctrl+D / EOF
                        chan.send(b"logout\r\n")
                        return
                    elif ch in ("\x7f", "\x08"):  # Backspace
                        if line:
                            line = line[:-1]
                            chan.send(b"\x08 \x08")
                    elif ord(ch) >= 32:
                        line += ch
                        chan.send(ch.encode())

                if not line.strip():
                    continue

                # capture output
                buf = _io.StringIO()
                old_out, old_err = sys.stdout, sys.stderr
                sys.stdout = sys.stderr = buf
                try:
                    sh.execute_line(line)
                finally:
                    sys.stdout, sys.stderr = old_out, old_err

                out = buf.getvalue()
                if out:
                    # convert bare \n to \r\n for SSH terminals
                    out = out.replace("\n", "\r\n")
                    chan.send(out.encode("utf-8", errors="replace"))

        except Exception:
            pass
        finally:
            try:
                chan.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def _load_or_generate_key(self, paramiko):
        os.makedirs(self.KEY_DIR, exist_ok=True)
        if os.path.isfile(self.HOST_KEY_PATH):
            try:
                key = paramiko.RSAKey(filename=self.HOST_KEY_PATH)
                IOManager.write(f"sshd: loaded host key from {self.HOST_KEY_PATH}")
                return key
            except Exception as e:
                IOManager.write(f"sshd: could not load host key ({e}), regenerating...")

        IOManager.write("sshd: generating RSA host key ...")
        try:
            key = paramiko.RSAKey.generate(2048)
            key.write_private_key_file(self.HOST_KEY_PATH)
            if not IS_WINDOWS:
                os.chmod(self.HOST_KEY_PATH, 0o600)
            IOManager.write(f"sshd: host key saved to {self.HOST_KEY_PATH}")
            return key
        except Exception as e:
            IOManager.error(f"sshd: failed to generate host key: {e}")
            return None

    @staticmethod
    def _fingerprint(key) -> str:
        data   = key.asbytes()
        digest = hashlib.md5(data).hexdigest()
        return ":".join(digest[i:i+2] for i in range(0, 32, 2))
    
class _SSHServerInterface:
    """
    Paramiko ServerInterface — handles auth and channel negotiation.
    Clean rewrite: no duplicate Transport creation, proper auth methods.
    """

    def __init__(self):
        self.shell_event  = threading.Event()
        self.exec_command: Optional[str] = None
        self.username:     Optional[str] = None

    # --- auth ---

    def get_allowed_auths(self, username: str) -> str:
        self.username = username
        return "password,publickey"

    def check_auth_password(self, username: str, password: str) -> int:
        try:
            import paramiko  # type: ignore
        except ImportError:
            return 1   # AUTH_FAILED = 1

        self.username = username

        # dev override via env vars
        tu = EnvManager.get("SSHD_TEST_USER")
        tp = EnvManager.get("SSHD_TEST_PASS")
        if tu and username == tu and password == tp:
            return paramiko.AUTH_SUCCESSFUL

        if IS_WINDOWS:
            return self._auth_windows(paramiko, username, password)
        return self._auth_unix(paramiko, username, password)

    def check_auth_publickey(self, username: str, key) -> int:
        try:
            import paramiko, base64  # type: ignore
        except ImportError:
            return 1

        auth_path = os.path.expanduser(f"~/.ssh/authorized_keys")
        if not os.path.isfile(auth_path):
            return paramiko.AUTH_FAILED

        try:
            with open(auth_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    try:
                        ak = paramiko.RSAKey(data=base64.b64decode(parts[1]))
                        if ak == key:
                            return paramiko.AUTH_SUCCESSFUL
                    except Exception:
                        continue
        except Exception:
            pass
        return paramiko.AUTH_FAILED

    def _auth_unix(self, paramiko, username: str, password: str) -> int:
        # try PAM first
        try:
            import pam  # type: ignore
            if pam.pam().authenticate(username, password):
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED
        except ImportError:
            pass

        # try shadow password
        try:
            import spwd, crypt  # type: ignore
            sp  = spwd.getspnam(username)
            if crypt.crypt(password, sp.sp_pwdp) == sp.sp_pwdp:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED
        except (ImportError, KeyError, PermissionError):
            pass

        # last resort: accept current user without password check (dev mode)
        import getpass
        if username == getpass.getuser():
            IOManager.write(
                "\nsshd: [WARNING] single-user fallback — accepted "
                f"{username!r} without password verification\n"
                "sshd: set SSHD_TEST_USER / SSHD_TEST_PASS for proper auth"
            )
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def _auth_windows(self, paramiko, username: str, password: str) -> int:
        try:
            import ctypes
            token = ctypes.c_void_p()
            ok = ctypes.windll.advapi32.LogonUserW(
                username, None, password, 2, 0, ctypes.byref(token)
            )
            if ok:
                ctypes.windll.kernel32.CloseHandle(token)
                return paramiko.AUTH_SUCCESSFUL
        except Exception:
            pass
        return paramiko.AUTH_FAILED

    # --- channel ---

    def check_channel_request(self, kind: str, chanid: int) -> int:
        try:
            import paramiko  # type: ignore
            return (paramiko.OPEN_SUCCEEDED if kind == "session"
                    else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)
        except ImportError:
            return 0

    def check_channel_shell_request(self, channel) -> bool:
        self.shell_event.set()
        return True

    def check_channel_exec_request(self, channel, command: bytes) -> bool:
        self.exec_command = command.decode("utf-8", errors="replace")
        self.shell_event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, *args) -> bool:
        return True

    def check_channel_window_change_request(self, channel, width, height, *args) -> bool:
        return True
    