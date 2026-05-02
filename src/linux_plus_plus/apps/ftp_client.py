""""""

import os
import sys
import time
from typing import Optional

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager
except ImportError:
    IS_WINDOWS = os.name == "nt"


# ===========================================================================
# FTPClient  — file transfer over plain TCP sockets
# ===========================================================================

class FTPClient:
    """
    Minimal FTP-style client using Python's ftplib (stdlib).
    Falls back to a raw socket transfer protocol if ftplib is unavailable
    (for custom linux++ servers).

    Commands in interactive session:
      ls [path]
      cd <path>
      get <remote> [local]
      put <local> [remote]
      pwd
      quit / bye / exit
    """

    def __init__(self, host: str, port: int = 21):
        self._host = host
        self._port = port
        self._ftp  = None

    def connect(self) -> bool:
        try:
            import ftplib
            self._ftp = ftplib.FTP()
            self._ftp.connect(self._host, self._port, timeout=10)
            banner = self._ftp.getwelcome()
            IOManager.write(banner)
            user = input("Name: ")
            pw   = input("Password: ")
            self._ftp.login(user, pw)
            IOManager.write(f"Logged in as {user}")
            return True
        except Exception as e:
            IOManager.error(f"ftp: connection failed: {e}")
            return False

    def run_interactive(self) -> int:
        if not self.connect():
            return 1
        IOManager.write("Type 'help' for commands, 'quit' to exit.")
        while True:
            try:
                raw = input("ftp> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw:
                continue
            parts = raw.split()
            cmd   = parts[0].lower()
            args  = parts[1:]

            if cmd in ("quit", "bye", "exit"):
                break
            elif cmd == "ls":
                self._ls(args[0] if args else ".")
            elif cmd == "cd":
                self._cd(args[0] if args else "/")
            elif cmd == "pwd":
                self._pwd()
            elif cmd == "get":
                if not args:
                    IOManager.error("Usage: get <remote> [local]")
                else:
                    self._get(args[0], args[1] if len(args) > 1 else None)
            elif cmd == "put":
                if not args:
                    IOManager.error("Usage: put <local> [remote]")
                else:
                    self._put(args[0], args[1] if len(args) > 1 else None)
            elif cmd == "help":
                IOManager.write(
                    "Commands: ls  cd  pwd  get  put  quit\n"
                    "  ls [path]            list remote directory\n"
                    "  cd <path>            change remote directory\n"
                    "  pwd                  print remote working directory\n"
                    "  get <remote> [local] download file\n"
                    "  put <local> [remote] upload file"
                )
            else:
                IOManager.error(f"ftp: unknown command {cmd!r}")

        if self._ftp:
            try: self._ftp.quit()
            except Exception: pass
        IOManager.write("Goodbye.")
        return 0

    def _ls(self, path: str) -> None:
        try:
            self._ftp.dir(path)
        except Exception as e:
            IOManager.error(f"ftp: ls: {e}")

    def _cd(self, path: str) -> None:
        try:
            self._ftp.cwd(path)
            IOManager.write(f"Remote: {self._ftp.pwd()}")
        except Exception as e:
            IOManager.error(f"ftp: cd: {e}")

    def _pwd(self) -> None:
        try:
            IOManager.write(self._ftp.pwd())
        except Exception as e:
            IOManager.error(f"ftp: pwd: {e}")

    def _get(self, remote: str, local: Optional[str]) -> None:
        local = local or os.path.basename(remote)
        try:
            size = self._ftp.size(remote) or 0
            received = [0]
            start = time.time()

            def _progress(block: bytes):
                received[0] += len(block)
                if size:
                    pct = received[0] * 100 // size
                    bar = "#" * (pct // 5) + " " * (20 - pct // 5)
                    sys.stdout.write(f"\r  [{bar}] {pct:3}%  {received[0]//1024}K")
                    sys.stdout.flush()

            with open(local, "wb") as f:
                self._ftp.retrbinary(f"RETR {remote}", lambda b: (f.write(b), _progress(b)))
            elapsed = time.time() - start
            IOManager.write(f"\n  {received[0]} bytes in {elapsed:.1f}s → {local}")
        except Exception as e:
            IOManager.error(f"\nftp: get: {e}")

    def _put(self, local: str, remote: Optional[str]) -> None:
        remote = remote or os.path.basename(local)
        try:
            size = os.path.getsize(local)
            sent = [0]
            start = time.time()

            def _progress(block: bytes):
                sent[0] += len(block)
                if size:
                    pct = sent[0] * 100 // size
                    bar = "#" * (pct // 5) + " " * (20 - pct // 5)
                    sys.stdout.write(f"\r  [{bar}] {pct:3}%  {sent[0]//1024}K")
                    sys.stdout.flush()

            with open(local, "rb") as f:
                self._ftp.storbinary(f"STOR {remote}", f, callback=_progress)
            elapsed = time.time() - start
            IOManager.write(f"\n  Uploaded {sent[0]} bytes in {elapsed:.1f}s → {remote}")
        except Exception as e:
            IOManager.error(f"\nftp: put: {e}")
        