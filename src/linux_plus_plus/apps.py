"""
linux++ — User Applications (Layer 5)
=======================================
Pure Python standard library only. No third-party deps.
Depends on Layer 1-4 (HAL, Stdlib, Kernel, Shell).

Applications:
  - PackageManager  : lpp install / remove / list / search
  - TextEditor      : edit <file>       (nano-style terminal editor)
  - ManSystem       : man <command>     (built-in manual pages)
  - FTPClient       : ftp <host>        (file transfer over FTP)
  - SSHClient       : ssh [user@]host   (SSH client, paramiko + fallback)
  - SSHDaemon       : sshd start|stop   (SSH server via paramiko)
  - SysInfo         : sysinfo / neofetch
  - ScriptRunner    : sh <script.lpp>
  - Apps            : registers everything into the shell
"""

import os
import sys
import json
import socket
import struct
import hashlib
import zipfile
import urllib.request
import urllib.error
import urllib.parse
import tempfile
import time
import platform
import threading
from typing import Optional

try:
    from .hal    import IS_WINDOWS
    from .stdlib import IOManager, EnvManager, SignalHandler
    from .kernel import Kernel, SyscallError
    from .shell  import Shell, BuiltinRegistry
except ImportError:
    IS_WINDOWS = os.name == "nt"


# ===========================================================================
# PackageManager  — lpp install / remove / list / update / search
# ===========================================================================

class PackageManager:
    """
    linux++ package manager.

    Packages are plain zip files containing:
      - pkg.json       : metadata  { name, version, description, entry }
      - <entry>.py     : the program (registered as a shell builtin)

    Registry is a local JSON index at ~/.linuxpp/registry.json
    Remote registry: simple JSON file hosted anywhere (configurable).

    Commands:
      lpp install <name|path|url>
      lpp remove  <name>
      lpp list
      lpp search  <query>
      lpp update  <name>
      lpp info    <name>
    """

    PKG_DIR      = os.path.join(os.path.expanduser("~"), ".linuxpp", "packages")
    REGISTRY     = os.path.join(os.path.expanduser("~"), ".linuxpp", "installed.json")
    REMOTE_INDEX = "https://raw.githubusercontent.com/veeracoder508/linux-plus-plus-pkgs/main/index.json"

    def __init__(self, shell: "Shell"):
        self._shell = shell
        os.makedirs(self.PKG_DIR, exist_ok=True)
        self._db = self._load_db()

    # --- db helpers ---

    def _load_db(self) -> dict:
        if os.path.exists(self.REGISTRY):
            try:
                with open(self.REGISTRY, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_db(self) -> None:
        os.makedirs(os.path.dirname(self.REGISTRY), exist_ok=True)
        with open(self.REGISTRY, "w", encoding="utf-8") as f:
            json.dump(self._db, f, indent=2)

    # --- public commands ---

    def install(self, source: str) -> int:
        """Install from a local .lpp zip, a URL, or a name from the remote index."""
        IOManager.write(f"lpp: resolving {source!r} ...")

        pkg_path = self._resolve_source(source)
        if not pkg_path:
            IOManager.error(f"lpp: cannot find package {source!r}")
            return 1

        return self._install_zip(pkg_path)

    def remove(self, name: str) -> int:
        if name not in self._db:
            IOManager.error(f"lpp: {name!r} is not installed")
            return 1
        entry = self._db[name]
        pkg_dir = os.path.join(self.PKG_DIR, name)
        if os.path.isdir(pkg_dir):
            import shutil
            shutil.rmtree(pkg_dir)
        del self._db[name]
        self._save_db()
        # unregister from shell
        self._shell.builtins._builtins.pop(name, None)
        IOManager.write(f"lpp: removed {name} {entry.get('version','')}")
        return 0

    def list_installed(self) -> int:
        if not self._db:
            IOManager.write("No packages installed.")
            return 0
        IOManager.write(f"{'Name':<20} {'Version':<12} Description")
        IOManager.write("-" * 60)
        for name, meta in sorted(self._db.items()):
            IOManager.write(
                f"{name:<20} {meta.get('version','?'):<12} {meta.get('description','')}"
            )
        return 0

    def search(self, query: str) -> int:
        index = self._fetch_remote_index()
        if index is None:
            IOManager.error("lpp: could not fetch remote index (offline?)")
            return 1
        query = query.lower()
        results = [
            (n, m) for n, m in index.items()
            if query in n.lower() or query in m.get("description", "").lower()
        ]
        if not results:
            IOManager.write(f"lpp: no packages matching {query!r}")
            return 1
        IOManager.write(f"{'Name':<20} {'Version':<12} Description")
        IOManager.write("-" * 60)
        for name, meta in sorted(results):
            mark = " [installed]" if name in self._db else ""
            IOManager.write(
                f"{name:<20} {meta.get('version','?'):<12} {meta.get('description','')}{mark}"
            )
        return 0

    def update(self, name: str) -> int:
        if name not in self._db:
            IOManager.error(f"lpp: {name!r} is not installed")
            return 1
        return self.install(name)

    def info(self, name: str) -> int:
        meta = self._db.get(name)
        if not meta:
            IOManager.error(f"lpp: {name!r} is not installed")
            return 1
        for k, v in meta.items():
            IOManager.write(f"  {k:<14}: {v}")
        return 0

    # --- internals ---

    def _resolve_source(self, source: str) -> Optional[str]:
        # local file
        if os.path.isfile(source):
            return source
        # URL
        if source.startswith("http://") or source.startswith("https://"):
            return self._download(source)
        # remote index lookup
        index = self._fetch_remote_index()
        if index and source in index:
            url = index[source].get("url")
            if url:
                return self._download(url)
        return None

    def _download(self, url: str) -> Optional[str]:
        try:
            IOManager.write(f"lpp: downloading {url}")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".lpp")
            urllib.request.urlretrieve(url, tmp.name)
            return tmp.name
        except Exception as e:
            IOManager.error(f"lpp: download failed: {e}")
            return None

    def _fetch_remote_index(self) -> Optional[dict]:
        try:
            with urllib.request.urlopen(self.REMOTE_INDEX, timeout=5) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def _install_zip(self, path: str) -> int:
        try:
            with zipfile.ZipFile(path, "r") as z:
                if "pkg.json" not in z.namelist():
                    IOManager.error("lpp: invalid package — missing pkg.json")
                    return 1
                meta = json.loads(z.read("pkg.json").decode())
                name = meta.get("name")
                if not name:
                    IOManager.error("lpp: pkg.json missing 'name' field")
                    return 1
                pkg_dir = os.path.join(self.PKG_DIR, name)
                os.makedirs(pkg_dir, exist_ok=True)
                z.extractall(pkg_dir)
        except zipfile.BadZipFile:
            IOManager.error("lpp: file is not a valid .lpp package")
            return 1
        except Exception as e:
            IOManager.error(f"lpp: install error: {e}")
            return 1

        self._db[name] = meta
        self._save_db()
        self._register_package(name, meta, pkg_dir)
        IOManager.write(f"lpp: installed {name} {meta.get('version','')}")
        return 0

    def _register_package(self, name: str, meta: dict, pkg_dir: str) -> None:
        """Load the package entry point and register it as a shell builtin."""
        entry = meta.get("entry", name)
        entry_file = os.path.join(pkg_dir, entry + ".py")
        if not os.path.isfile(entry_file):
            return
        import importlib.util
        spec   = importlib.util.spec_from_file_location(name, entry_file)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            if hasattr(module, "main"):
                self._shell.builtins.register(name, module.main)
        except Exception as e:
            IOManager.error(f"lpp: failed to load {name}: {e}")

    def load_installed(self) -> None:
        """Called at shell boot — re-register all installed packages."""
        for name, meta in self._db.items():
            pkg_dir = os.path.join(self.PKG_DIR, name)
            self._register_package(name, meta, pkg_dir)


# ===========================================================================
# TextEditor  — nano-style terminal editor
# ===========================================================================

class TextEditor:
    """
    Minimal full-screen terminal text editor.
    Uses only: os, sys — pure builtins.
    Keybindings (subset of nano):
      Arrow keys   : move cursor
      Ctrl+S       : save
      Ctrl+X       : exit
      Ctrl+K       : cut line
      Ctrl+U       : paste line
      Backspace    : delete char left
      Delete       : delete char right
      Enter        : new line
    """

    def __init__(self, path: str):
        self._path   = path
        self._lines  = [""]
        self._cy     = 0    # cursor row
        self._cx     = 0    # cursor col
        self._offset = 0    # top row (scroll)
        self._dirty  = False
        self._clip   = ""
        self._msg    = ""
        self._running = True

        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                self._lines = content.splitlines() or [""]
            except OSError as e:
                self._msg = f"Read error: {e}"

    def run(self) -> int:
        if IS_WINDOWS:
            return self._run_windows()
        return self._run_unix()

    # --- Unix editor (uses termios/tty) ---

    def _run_unix(self) -> int:
        import termios, tty
        fd   = sys.stdin.fileno()
        old  = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while self._running:
                self._draw_unix()
                key = self._read_key()
                self._handle_key(key)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        return 0

    def _read_key(self) -> str:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return "\x1b" + seq
        return ch

    def _draw_unix(self) -> None:
        cols, rows = self._term_size()
        visible    = rows - 2
        buf        = ["\033[2J\033[H"]  # clear + home

        for i in range(visible):
            idx = self._offset + i
            if idx < len(self._lines):
                line = self._lines[idx]
                line = line[:cols - 1]
            else:
                line = "~"
            buf.append(line)
            buf.append("\033[K\r\n")

        # status bar
        name  = os.path.basename(self._path)
        dirty = " [modified]" if self._dirty else ""
        pos   = f"  Ln {self._cy+1}/{len(self._lines)}  Col {self._cx+1}"
        stat  = f"\033[7m  {name}{dirty}{pos:<{cols-len(name)-len(dirty)-2}}\033[0m"
        buf.append(stat + "\r\n")

        # help bar
        help_bar = "^S Save  ^X Exit  ^K Cut  ^U Paste  Arrows Move"[:cols]
        msg_bar  = self._msg[:cols] if self._msg else help_bar
        self._msg = ""
        buf.append(f"\033[2K{msg_bar}")

        # move cursor to position
        cy_screen = self._cy - self._offset + 1
        buf.append(f"\033[{cy_screen};{self._cx+1}H")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    # --- Windows editor (simple line-mode fallback) ---

    def _run_windows(self) -> int:
        IOManager.write(f"linux++ editor  [{self._path}]")
        IOManager.write("Commands: :w save  :q quit  :wq save+quit  :N go to line N")
        IOManager.write(f"{len(self._lines)} lines loaded\n")
        while self._running:
            try:
                line_no = self._cy + 1
                raw = input(f"{line_no:4}: ")
            except (EOFError, KeyboardInterrupt):
                break
            if raw == ":w":
                self._save(); IOManager.write("Saved.")
            elif raw == ":q":
                if self._dirty:
                    yn = input("Unsaved changes. Quit anyway? [y/N] ")
                    if yn.lower() == "y":
                        self._running = False
                else:
                    self._running = False
            elif raw == ":wq":
                self._save(); self._running = False
            elif raw.startswith(":") and raw[1:].isdigit():
                n = int(raw[1:]) - 1
                self._cy = max(0, min(n, len(self._lines)-1))
                IOManager.write(self._lines[self._cy])
            else:
                # replace current line and advance
                if self._cy < len(self._lines):
                    self._lines[self._cy] = raw
                else:
                    self._lines.append(raw)
                self._cy += 1
                self._dirty = True
        return 0

    # --- key handler (shared) ---

    def _handle_key(self, key: str) -> None:
        if key == "\x13":          # Ctrl+S
            self._save()
            self._msg = f"Saved: {self._path}"
        elif key == "\x18":        # Ctrl+X
            if self._dirty:
                self._msg = "Unsaved changes — press ^X again to quit, ^S to save"
                self._dirty = False  # allow second ^X
            else:
                self._running = False
        elif key == "\x0b":        # Ctrl+K  cut line
            self._clip = self._lines.pop(self._cy) if len(self._lines) > 1 else ""
            if self._cy >= len(self._lines): self._cy = len(self._lines) - 1
            self._dirty = True
        elif key == "\x15":        # Ctrl+U  paste
            self._lines.insert(self._cy, self._clip)
            self._cy += 1; self._dirty = True
        elif key == "\r" or key == "\n":
            rest = self._lines[self._cy][self._cx:]
            self._lines[self._cy] = self._lines[self._cy][:self._cx]
            self._cy += 1
            self._lines.insert(self._cy, rest)
            self._cx = 0; self._dirty = True
        elif key == "\x7f" or key == "\x08":  # Backspace
            if self._cx > 0:
                l = self._lines[self._cy]
                self._lines[self._cy] = l[:self._cx-1] + l[self._cx:]
                self._cx -= 1; self._dirty = True
            elif self._cy > 0:
                prev = self._lines[self._cy-1]
                self._cx = len(prev)
                self._lines[self._cy-1] = prev + self._lines.pop(self._cy)
                self._cy -= 1; self._dirty = True
        elif key == "\x1b[A":     # Up
            if self._cy > 0: self._cy -= 1
            self._cx = min(self._cx, len(self._lines[self._cy]))
            self._scroll()
        elif key == "\x1b[B":     # Down
            if self._cy < len(self._lines)-1: self._cy += 1
            self._cx = min(self._cx, len(self._lines[self._cy]))
            self._scroll()
        elif key == "\x1b[C":     # Right
            if self._cx < len(self._lines[self._cy]):
                self._cx += 1
            elif self._cy < len(self._lines)-1:
                self._cy += 1; self._cx = 0
            self._scroll()
        elif key == "\x1b[D":     # Left
            if self._cx > 0:
                self._cx -= 1
            elif self._cy > 0:
                self._cy -= 1
                self._cx = len(self._lines[self._cy])
            self._scroll()
        elif len(key) == 1 and ord(key) >= 32:  # printable
            l = self._lines[self._cy]
            self._lines[self._cy] = l[:self._cx] + key + l[self._cx:]
            self._cx += 1; self._dirty = True

    def _scroll(self) -> None:
        _, rows = self._term_size()
        visible = rows - 2
        if self._cy < self._offset:
            self._offset = self._cy
        elif self._cy >= self._offset + visible:
            self._offset = self._cy - visible + 1

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._lines))
            self._dirty = False
        except OSError as e:
            self._msg = f"Save failed: {e}"

    @staticmethod
    def _term_size() -> tuple[int, int]:
        try:
            s = os.get_terminal_size()
            return s.columns, s.lines
        except OSError:
            return 80, 24


# ===========================================================================
# ManSystem  — built-in manual pages
# ===========================================================================

MAN_PAGES: dict[str, str] = {
    "ls": """\
NAME
    ls — list directory contents

SYNOPSIS
    ls [-la] [path ...]

OPTIONS
    -l   long format (permissions, size, date)
    -a   show hidden files (starting with .)
    -la  both

EXAMPLES
    ls
    ls -la /tmp
    ls ~/documents""",

    "cd": """\
NAME
    cd — change working directory

SYNOPSIS
    cd [directory]

DESCRIPTION
    With no argument, changes to the home directory.

EXAMPLES
    cd /tmp
    cd ..
    cd ~
    cd -    (not yet implemented)""",

    "cat": """\
NAME
    cat — concatenate and display files

SYNOPSIS
    cat [-n] [file ...]

OPTIONS
    -n   number output lines

EXAMPLES
    cat file.txt
    cat -n script.lpp
    cat file1.txt file2.txt""",

    "grep": """\
NAME
    grep — search for a pattern in files

SYNOPSIS
    grep [-inv] <pattern> [file ...]

OPTIONS
    -i   ignore case
    -n   show line numbers
    -v   invert match (show non-matching lines)

EXAMPLES
    grep error log.txt
    grep -in "warning" *.log
    cat file.txt | grep TODO""",

    "echo": """\
NAME
    echo — display a line of text

SYNOPSIS
    echo [-n] [text ...]

OPTIONS
    -n   do not output a trailing newline

EXAMPLES
    echo Hello, World!
    echo -n "no newline"
    echo "Value: $HOME" """,

    "mkdir": """\
NAME
    mkdir — make directories

SYNOPSIS
    mkdir [-p] <directory> ...

OPTIONS
    -p   create parent directories as needed

EXAMPLES
    mkdir mydir
    mkdir -p a/b/c""",

    "rm": """\
NAME
    rm — remove files or directories

SYNOPSIS
    rm [-rf] <path> ...

OPTIONS
    -r   recursive (required for directories)
    -f   force (ignore errors)

EXAMPLES
    rm file.txt
    rm -rf old_folder""",

    "cp": """\
NAME
    cp — copy files

SYNOPSIS
    cp <source> <destination>

EXAMPLES
    cp file.txt backup.txt
    cp notes.txt /tmp/""",

    "mv": """\
NAME
    mv — move or rename files

SYNOPSIS
    mv <source> <destination>

EXAMPLES
    mv old.txt new.txt
    mv file.txt /tmp/""",

    "pwd": """\
NAME
    pwd — print working directory

SYNOPSIS
    pwd

DESCRIPTION
    Prints the absolute path of the current directory.""",

    "export": """\
NAME
    export — set environment variables

SYNOPSIS
    export NAME=VALUE
    export NAME

EXAMPLES
    export EDITOR=nano
    export PATH=$PATH:/usr/local/bin""",

    "alias": """\
NAME
    alias — define command shortcuts

SYNOPSIS
    alias name='command'
    alias           (list all aliases)

EXAMPLES
    alias ll='ls -la'
    alias ..='cd ..'""",

    "history": """\
NAME
    history — show command history

SYNOPSIS
    history [n]

DESCRIPTION
    Shows previous commands. Optional n limits to last n entries.

EXAMPLES
    history
    history 20""",

    "jobs": """\
NAME
    jobs — list background jobs

SYNOPSIS
    jobs

DESCRIPTION
    Lists all background processes started with & .""",

    "kill": """\
NAME
    kill — terminate a job or process

SYNOPSIS
    kill [-9] <job_id|pid>

OPTIONS
    -9   force kill (SIGKILL)

EXAMPLES
    kill 1
    kill -9 2""",

    "source": """\
NAME
    source — execute commands from a file

SYNOPSIS
    source <file>

DESCRIPTION
    Reads and executes commands from file in the current shell.
    Useful for loading aliases, exports, and functions.

EXAMPLES
    source ~/.linuxpprc
    source setup.lpp""",

    "lpp": """\
NAME
    lpp — linux++ package manager

SYNOPSIS
    lpp install <name|path|url>
    lpp remove  <name>
    lpp list
    lpp search  <query>
    lpp update  <name>
    lpp info    <name>

DESCRIPTION
    lpp manages linux++ application packages (.lpp files).
    Packages are zip files containing a pkg.json manifest
    and a Python entry point.

EXAMPLES
    lpp install mypkg.lpp
    lpp install https://example.com/pkg.lpp
    lpp list
    lpp remove mypkg""",

    "edit": """\
NAME
    edit — open the linux++ text editor

SYNOPSIS
    edit <file>

DESCRIPTION
    Opens a file in the built-in terminal text editor.

KEYBINDINGS
    Ctrl+S   save
    Ctrl+X   exit
    Ctrl+K   cut line
    Ctrl+U   paste line
    Arrows   move cursor

EXAMPLES
    edit notes.txt
    edit script.lpp""",

    "ftp": """\
NAME
    ftp — transfer files over the network

SYNOPSIS
    ftp <host> [port]

SUBCOMMANDS (interactive mode)
    get <remote> [local]   download a file
    put <local>  [remote]  upload a file
    ls                     list remote directory
    cd <dir>               change remote directory
    quit                   exit ftp session

EXAMPLES
    ftp 192.168.1.10
    ftp myserver.local 2121""",

    "sysinfo": """\
NAME
    sysinfo — display system information

SYNOPSIS
    sysinfo

DESCRIPTION
    Displays OS, CPU, memory, disk, shell version, and uptime
    in a neofetch-style layout.""",

    "wc": """\
NAME
    wc — word, line, and character count

SYNOPSIS
    wc [-lwc] [file ...]

OPTIONS
    -l   count lines
    -w   count words
    -c   count characters

EXAMPLES
    wc file.txt
    wc -l *.py
    cat file.txt | wc -w""",

    "head": """\
NAME
    head — output the first lines of a file

SYNOPSIS
    head [-n N] [file ...]

EXAMPLES
    head file.txt
    head -n 5 file.txt""",

    "tail": """\
NAME
    tail — output the last lines of a file

SYNOPSIS
    tail [-n N] [file ...]

EXAMPLES
    tail file.txt
    tail -n 20 log.txt""",

    "which": """\
NAME
    which — locate a command in PATH

SYNOPSIS
    which <command>

EXAMPLES
    which python3
    which ls""",

    "sh": """\
NAME
    sh — run a linux++ script

SYNOPSIS
    sh <script.lpp>

DESCRIPTION
    Executes each line of the script in the current shell.
    Supports #! shebang lines (ignored).

EXAMPLES
    sh setup.lpp
    sh deploy.lpp""",

    "ssh": """\
NAME
    ssh — connect to a remote host over SSH

SYNOPSIS
    ssh [user@]host [port]
    ssh [user@]host <command>

DESCRIPTION
    Opens an interactive SSH session or runs a single remote command.
    Uses paramiko if installed (pip install paramiko), otherwise falls
    back to the system ssh binary.

OPTIONS
    user    remote username  (default: current user)
    host    hostname or IP   
    port    port number      (default: 22)
    command optional single remote command (non-interactive)

EXAMPLES
    ssh myserver.local
    ssh root@192.168.1.10
    ssh user@host 2222
    ssh user@host ls -la /var/log""",

    "sshd": """\
NAME
    sshd — linux++ SSH daemon

SYNOPSIS
    sshd start [port]
    sshd stop
    sshd status
    sshd keygen

DESCRIPTION
    Starts a real SSH server inside linux++ powered by paramiko.
    Accepts incoming SSH connections from any standard SSH client
    (OpenSSH, PuTTY, WinSCP, etc).

    Requires:  pip install paramiko

    Authentication:
      - Password auth via OS user database (PAM / spwd / WinAPI)
      - Public key auth via ~/.ssh/authorized_keys
      - Dev override: export SSHD_TEST_USER=u SSHD_TEST_PASS=p

    Host key is stored at ~/.linuxpp/ssh/host_rsa_key
    and auto-generated on first start.

SUBCOMMANDS
    start [port]   start the daemon (default port: 2222)
    stop           gracefully shut down and disconnect all clients
    status         show port, uptime, and connected client list
    keygen         regenerate the RSA host key

EXAMPLES
    sshd start               start on port 2222
    sshd start 2200          start on custom port
    sshd status              check who is connected
    sshd stop                shut down the server
    sshd keygen              rotate the host key

    # connect from another machine:
    ssh user@your-ip -p 2222""",
}


class ManSystem:

    @staticmethod
    def show(topic: str) -> int:
        topic = topic.lower().strip()
        page = MAN_PAGES.get(topic)
        if not page:
            IOManager.error(f"man: no manual entry for {topic!r}")
            IOManager.write(f"Available topics: {', '.join(sorted(MAN_PAGES))}")
            return 1

        if not IS_WINDOWS:
            # paginate with a simple pager
            lines   = page.splitlines()
            _, rows = TextEditor._term_size()
            page_h  = rows - 2
            for i in range(0, len(lines), page_h):
                for line in lines[i:i+page_h]:
                    IOManager.write(line)
                if i + page_h < len(lines):
                    try:
                        k = input("-- More -- (Enter/q) ")
                        if k.lower() == "q":
                            break
                    except (EOFError, KeyboardInterrupt):
                        break
        else:
            IOManager.write(page)
        return 0


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


# ===========================================================================
# SysInfo  — neofetch-style system information
# ===========================================================================

class SysInfo:

    @staticmethod
    def display() -> int:
        lines = SysInfo._collect()
        logo  = SysInfo._logo()

        if IS_WINDOWS:
            for line in lines:
                IOManager.write(line)
            return 0

        # side-by-side: logo left, info right
        logo_w = max(len(l) for l in logo) + 4
        out    = []
        n      = max(len(logo), len(lines))
        for i in range(n):
            l = logo[i]  if i < len(logo)  else ""
            r = lines[i] if i < len(lines) else ""
            out.append(f"{l:<{logo_w}}{r}")
        IOManager.write("\n".join(out) + "\n")
        return 0

    @staticmethod
    def _logo() -> list[str]:
        G = "\033[32;1m"; R = "\033[0m"
        return [
            f"{G}  _ _             ++  {R}",
            f"{G} | (_)_ __  _   ___   {R}",
            f"{G} | | | '_ \\| | | \\ \\  {R}",
            f"{G} | | | | | | |_| |> > {R}",
            f"{G} |_|_|_| |_|\\__,_/_/  {R}",
            f"{G}                       {R}",
        ]

    @staticmethod
    def _collect() -> list[str]:
        C = "\033[36;1m"; R = "\033[0m"

        import socket as _sock
        import shutil  as _sh

        user    = EnvManager.username()
        host    = _sock.gethostname()
        os_name = f"{platform.system()} {platform.release()}"
        arch    = platform.machine()
        py_ver  = platform.python_version()
        shell   = f"linux++ shell"

        # disk
        try:
            usage = _sh.disk_usage(os.path.expanduser("~"))
            disk  = f"{usage.used//1024//1024//1024}G / {usage.total//1024//1024//1024}G"
        except Exception:
            disk = "N/A"

        # memory
        mem = "N/A"
        try:
            import psutil
            vm  = psutil.virtual_memory()
            mem = f"{vm.used//1024//1024} MB / {vm.total//1024//1024} MB"
        except ImportError:
            if platform.system() == "Linux":
                try:
                    with open("/proc/meminfo") as f:
                        lines_m = f.read().splitlines()
                    total = avail = 0
                    for ln in lines_m:
                        if ln.startswith("MemTotal"):
                            total = int(ln.split()[1]) // 1024
                        if ln.startswith("MemAvailable"):
                            avail = int(ln.split()[1]) // 1024
                    mem = f"{total-avail} MB / {total} MB"
                except Exception:
                    pass

        # uptime
        uptime = "N/A"
        try:
            if platform.system() == "Linux":
                with open("/proc/uptime") as f:
                    secs = float(f.read().split()[0])
                h, rem = divmod(int(secs), 3600)
                m = rem // 60
                uptime = f"{h}h {m}m"
        except Exception:
            pass

        sep = f"{C}{user}@{host}{R}"
        div = "─" * len(f"{user}@{host}")
        return [
            sep, div,
            f"{C}OS      {R}: {os_name} ({arch})",
            f"{C}Shell   {R}: {shell}",
            f"{C}Python  {R}: {py_ver}",
            f"{C}Disk    {R}: {disk}",
            f"{C}Memory  {R}: {mem}",
            f"{C}Uptime  {R}: {uptime}",
            "",
            SysInfo._color_blocks(),
        ]

    @staticmethod
    def _color_blocks() -> str:
        blocks = "".join(f"\033[4{i}m   " for i in range(8))
        return blocks + "\033[0m"


# ===========================================================================
# ScriptRunner  — run .lpp scripts
# ===========================================================================

class ScriptRunner:
    """
    Executes a .lpp script file line-by-line in the current shell.
    Supports:
      - #! shebang (ignored)
      - # comments
      - all shell syntax (pipes, redirects, &&, ||)
      - positional args: $1 $2 ... $@
    """

    def __init__(self, shell: "Shell"):
        self._shell = shell

    def run(self, path: str, args: list[str]) -> int:
        resolved = self._shell.kernel.vfs.resolve(path)
        try:
            with open(resolved, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            IOManager.error(f"sh: {path}: No such file")
            return 1
        except PermissionError:
            IOManager.error(f"sh: {path}: Permission denied")
            return 1

        # set positional variables
        for i, arg in enumerate(args, 1):
            EnvManager.set(str(i), arg)
        EnvManager.set("@", " ".join(args))
        EnvManager.set("#", str(len(args)))
        EnvManager.set("0", path)

        rc = 0
        for lineno, raw in enumerate(content.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rc = self._shell.execute_line(line)

        return rc


# ===========================================================================
# SSHClient  — interactive SSH using paramiko (with system ssh fallback)
# ===========================================================================

class SSHClient:
    """
    SSH client with two backends:

    Backend 1 — paramiko (preferred, pure Python):
        pip install paramiko
        Full interactive PTY session, key auth, password auth.

    Backend 2 — system ssh binary (fallback):
        Uses whatever `ssh` is on PATH (OpenSSH on Linux/macOS,
        or the built-in ssh.exe on Windows 10+).
        No extra deps needed.

    Usage:
        ssh [user@]host [port]          interactive session
        ssh [user@]host [port] <cmd>    run single remote command
    """

    def __init__(self, host: str, user: str, port: int = 22):
        self._host = host
        self._user = user
        self._port = port

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run_interactive(self, command: Optional[str] = None) -> int:
        """Try paramiko first, fall back to system ssh."""
        try:
            import paramiko  # type: ignore
            return self._paramiko_session(paramiko, command)
        except ImportError:
            return self._system_ssh(command)

    # ------------------------------------------------------------------
    # Backend 1 — paramiko
    # ------------------------------------------------------------------

    def _paramiko_session(self, paramiko, command: Optional[str]) -> int:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # try key auth first, then password
        password = None
        try:
            client.connect(
                self._host,
                port=self._port,
                username=self._user,
                timeout=10,
                look_for_keys=True,
                allow_agent=True,
            )
        except paramiko.AuthenticationException:
            import getpass
            password = getpass.getpass(
                f"{self._user}@{self._host}'s password: "
            )
            try:
                client.connect(
                    self._host,
                    port=self._port,
                    username=self._user,
                    password=password,
                    timeout=10,
                    look_for_keys=False,
                )
            except Exception as e:
                IOManager.error(f"ssh: authentication failed: {e}")
                return 1
        except Exception as e:
            IOManager.error(f"ssh: {e}")
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
            client.close()

    def _paramiko_exec(self, client, command: str) -> int:
        """Run a single command and stream its output."""
        stdin, stdout, stderr = client.exec_command(command)
        for line in stdout:
            IOManager.write(line, end="")
        for line in stderr:
            IOManager.error(line, end="")
        return stdout.channel.recv_exit_status()

    def _paramiko_pty(self, client) -> int:
        """Full interactive PTY session."""
        import select

        chan = client.invoke_shell()
        chan.settimeout(0.0)

        if IS_WINDOWS:
            # Windows: simple read/write loop without termios
            import threading

            def _send():
                try:
                    while not chan.closed:
                        data = sys.stdin.read(1)
                        if not data:
                            break
                        chan.send(data)
                except Exception:
                    pass

            t = threading.Thread(target=_send, daemon=True)
            t.start()
            try:
                while not chan.closed:
                    if chan.recv_ready():
                        out = chan.recv(1024).decode("utf-8", errors="replace")
                        sys.stdout.write(out)
                        sys.stdout.flush()
                    time.sleep(0.01)
            except KeyboardInterrupt:
                chan.send("\x03")
        else:
            import termios, tty, select as _sel
            old_tty = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
                while True:
                    r, _, _ = _sel.select([chan, sys.stdin], [], [], 0.1)
                    if chan in r:
                        data = chan.recv(1024)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.flush()
                    if sys.stdin in r:
                        key = sys.stdin.read(1)
                        if not key:
                            break
                        chan.send(key)
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)

        IOManager.write("\nConnection closed.")
        return 0

    # ------------------------------------------------------------------
    # Backend 2 — system ssh binary
    # ------------------------------------------------------------------

    def _system_ssh(self, command: Optional[str]) -> int:
        """Delegate to the system's ssh binary."""
        import shutil, subprocess

        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            IOManager.error(
                "ssh: neither 'paramiko' nor a system 'ssh' binary was found.\n"
                "     Install paramiko:  pip install paramiko\n"
                "     Or install OpenSSH for your OS."
            )
            return 1

        cmd = [ssh_bin, "-p", str(self._port), f"{self._user}@{self._host}"]
        if command:
            cmd.append(command)

        IOManager.write(
            f"[using system ssh: {ssh_bin}]  "
            f"{self._user}@{self._host}:{self._port}"
        )
        try:
            # inherit stdin/stdout/stderr — full interactive terminal
            result = subprocess.run(cmd)
            return result.returncode
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            IOManager.error(f"ssh: {e}")
            return 1


# ===========================================================================
# SSHDaemon  — SSH server using paramiko (with fallback info)
# ===========================================================================

class SSHDaemon:
    """
    A real SSH server for linux++ built on paramiko's Transport layer.

    Features:
      - Password authentication (checked against the OS user database)
      - RSA host key (auto-generated on first start, saved to ~/.linuxpp/ssh/)
      - Each accepted client gets a full linux++ shell session
      - Runs in a background daemon thread — non-blocking
      - Supports multiple concurrent clients
      - sshd start [port]   — start the daemon
      - sshd stop           — stop the daemon
      - sshd status         — show running status and connected clients
      - sshd keygen         — regenerate the host key

    Requires:  pip install paramiko
    """

    KEY_DIR      = os.path.join(os.path.expanduser("~"), ".linuxpp", "ssh")
    HOST_KEY_PATH = os.path.join(KEY_DIR, "host_rsa_key")
    DEFAULT_PORT  = 2222          # non-privileged port, no sudo needed
    BANNER        = "linux++ sshd\r\n"

    def __init__(self, shell: "Shell"):
        self._shell      = shell
        self._port       = self.DEFAULT_PORT
        self._server_sock: Optional[socket.socket] = None
        self._thread:      Optional[threading.Thread] = None
        self._running     = False
        self._clients:    list[dict] = []          # {addr, transport, thread}
        self._host_key    = None
        self._lock        = threading.Lock()

    # ------------------------------------------------------------------
    # Public control commands
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
                "      Install it with:  pip install paramiko"
            )
            return 1

        self._port    = port
        self._host_key = self._load_or_generate_key(paramiko)

        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("0.0.0.0", port))
            self._server_sock.listen(10)
            self._server_sock.settimeout(1.0)
        except OSError as e:
            IOManager.error(f"sshd: cannot bind to port {port}: {e}")
            return 1

        self._running = True
        self._thread  = threading.Thread(
            target=self._accept_loop,
            args=(paramiko,),
            daemon=True,
            name="sshd-accept"
        )
        self._thread.start()

        fingerprint = self._key_fingerprint(self._host_key)
        IOManager.write(
            f"sshd: listening on 0.0.0.0:{port}\n"
            f"sshd: host key fingerprint: {fingerprint}\n"
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
        # close all active client transports
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
            n = len(self._clients)
            addrs = [c["addr"] for c in self._clients]
        IOManager.write(
            f"sshd: running on port {self._port}\n"
            f"sshd: {n} client(s) connected"
        )
        for addr in addrs:
            IOManager.write(f"       {addr[0]}:{addr[1]}")
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
        IOManager.write(
            f"sshd: new host key written to {self.HOST_KEY_PATH}\n"
            f"sshd: fingerprint: {self._key_fingerprint(key)}"
        )
        return 0

    # ------------------------------------------------------------------
    # Accept loop (daemon thread)
    # ------------------------------------------------------------------

    def _accept_loop(self, paramiko) -> None:
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            t = threading.Thread(
                target=self._handle_client,
                args=(paramiko, conn, addr),
                daemon=True,
                name=f"sshd-client-{addr[0]}:{addr[1]}"
            )
            t.start()

    # ------------------------------------------------------------------
    # Per-client handler
    # ------------------------------------------------------------------

    def _handle_client(self, paramiko, conn: socket.socket, addr: tuple) -> None:
        transport = None
        entry = {"addr": addr, "transport": None, "thread": threading.current_thread()}

        try:
            transport = paramiko.Transport(conn)
            transport.set_gss_host(socket.getfqdn(""))
            transport.load_server_moduli()
        except Exception:
            pass

        try:
            transport = paramiko.Transport(conn)
            transport.add_server_key(self._host_key)

            server_iface = _SSHServerInterface(self._shell)
            transport.start_server(server=server_iface)

            entry["transport"] = transport
            with self._lock:
                self._clients.append(entry)

            # wait for auth channel
            chan = transport.accept(30)
            if chan is None:
                return

            # wait for shell/exec request
            server_iface.shell_event.wait(10)

            if server_iface.exec_command:
                # non-interactive: run a single command
                self._run_command(chan, server_iface.exec_command)
            else:
                # interactive shell session
                self._run_shell(chan)

        except Exception as e:
            pass
        finally:
            if transport:
                try: transport.close()
                except Exception: pass
            try: conn.close()
            except Exception: pass
            with self._lock:
                self._clients[:] = [c for c in self._clients if c is not entry]

    def _run_command(self, chan, command: str) -> None:
        """Execute a single command and send output back over the channel."""
        from .kernel import Kernel
        from .shell  import Shell as _Shell

        k  = Kernel()
        sh = _Shell(k)

        import io as _io
        buf = _io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = buf
        sys.stderr = buf
        try:
            rc = sh.execute_line(command)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        output = buf.getvalue()
        try:
            chan.send(output.encode("utf-8", errors="replace"))
            chan.send_exit_status(rc)
        except Exception:
            pass
        finally:
            chan.close()

    def _run_shell(self, chan) -> None:
        """
        Full interactive shell over the SSH channel.
        Reads from channel, writes back to channel.
        """
        from .kernel import Kernel
        from .shell  import Shell as _Shell
        import io as _io

        k  = Kernel()
        sh = _Shell(k)

        # Redirect shell I/O through the channel
        chan.send(self.BANNER.encode())
        chan.send(b"\r\n")

        buf = ""
        try:
            while True:
                # send prompt
                prompt = sh._prompt().encode("utf-8", errors="replace")
                # strip ANSI for simplicity in remote sessions
                import re as _re
                prompt = _re.sub(rb'\x1b\[[0-9;]*m', b'', prompt)
                chan.send(prompt)

                # read line character by character
                line_buf = ""
                while True:
                    data = chan.recv(1)
                    if not data:
                        return
                    ch = data.decode("utf-8", errors="replace")

                    if ch in ("\r", "\n"):
                        chan.send(b"\r\n")
                        break
                    elif ch == "\x03":      # Ctrl+C
                        chan.send(b"^C\r\n")
                        line_buf = ""
                        break
                    elif ch == "\x04":      # Ctrl+D
                        chan.send(b"\r\nlogout\r\n")
                        return
                    elif ch in ("\x7f", "\x08"):  # Backspace
                        if line_buf:
                            line_buf = line_buf[:-1]
                            chan.send(b"\x08 \x08")
                    else:
                        line_buf += ch
                        chan.send(ch.encode())

                if not line_buf.strip():
                    continue

                # capture shell output
                out_buf = _io.StringIO()
                old_out = sys.stdout
                old_err = sys.stderr
                sys.stdout = out_buf
                sys.stderr = out_buf
                try:
                    sh.execute_line(line_buf)
                finally:
                    sys.stdout = old_out
                    sys.stderr = old_err

                output = out_buf.getvalue()
                if output:
                    # convert bare \n to \r\n for SSH terminals
                    output = output.replace("\n", "\r\n")
                    chan.send(output.encode("utf-8", errors="replace"))

                if not sh.running:
                    chan.send(b"logout\r\n")
                    return

        except Exception:
            pass
        finally:
            chan.close()

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
            except Exception:
                pass
        IOManager.write("sshd: generating 2048-bit RSA host key ...")
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(self.HOST_KEY_PATH)
        if not IS_WINDOWS:
            os.chmod(self.HOST_KEY_PATH, 0o600)
        IOManager.write(f"sshd: host key saved to {self.HOST_KEY_PATH}")
        return key

    @staticmethod
    def _key_fingerprint(key) -> str:
        import base64
        data = key.asbytes()
        digest = hashlib.md5(data).hexdigest()
        return ":".join(digest[i:i+2] for i in range(0, 32, 2))


class _SSHServerInterface:
    """
    Paramiko ServerInterface implementation.
    Handles auth and channel requests for each connecting client.
    """

    def __init__(self, shell: "Shell"):
        self._shell      = shell
        self.shell_event  = threading.Event()
        self.exec_command: Optional[str] = None

    def check_channel_request(self, kind, chanid):
        import paramiko  # type: ignore
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username: str, password: str) -> int:
        import paramiko  # type: ignore
        """
        Authenticate against the OS user database.
        On Unix: uses PAM via 'pam' module if available, else spwd/pwd.
        On Windows: uses ctypes WinAPI LogonUser.
        Falls back to a simple env-var override for testing:
            export SSHD_TEST_USER=myuser SSHD_TEST_PASS=mypass
        """
        # test override (development / CI)
        test_user = EnvManager.get("SSHD_TEST_USER")
        test_pass = EnvManager.get("SSHD_TEST_PASS")
        if test_user and username == test_user and password == test_pass:
            return paramiko.AUTH_SUCCESSFUL

        if IS_WINDOWS:
            return self._auth_windows(paramiko, username, password)
        return self._auth_unix(paramiko, username, password)

    def _auth_unix(self, paramiko, username: str, password: str) -> int:
        # try PAM first
        try:
            import pam  # type: ignore
            p = pam.pam()
            if p.authenticate(username, password):
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED
        except ImportError:
            pass
        # fallback: spwd (shadow password — requires root on most systems)
        try:
            import spwd, crypt  # type: ignore
            sp = spwd.getspnam(username)
            hashed = crypt.crypt(password, sp.sp_pwdp)
            if hashed == sp.sp_pwdp:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED
        except Exception:
            pass
        # last resort: accept if username matches current user (dev mode)
        import getpass
        if username == getpass.getuser():
            IOManager.write(
                f"\nsshd: [WARNING] falling back to single-user mode "
                f"(accepted {username!r} without password check)\n"
                f"sshd: set SSHD_TEST_USER / SSHD_TEST_PASS for proper auth"
            )
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def _auth_windows(self, paramiko, username: str, password: str) -> int:
        try:
            import ctypes
            advapi = ctypes.windll.advapi32
            token  = ctypes.c_void_p()
            ok = advapi.LogonUserW(
                username, None, password,
                2,   # LOGON32_LOGON_INTERACTIVE
                0,   # LOGON32_PROVIDER_DEFAULT
                ctypes.byref(token)
            )
            if ok:
                ctypes.windll.kernel32.CloseHandle(token)
                return paramiko.AUTH_SUCCESSFUL
        except Exception:
            pass
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        import paramiko  # type: ignore
        """Check ~/.ssh/authorized_keys for the presented public key."""
        auth_keys_path = os.path.join(
            os.path.expanduser(f"~{username}"), ".ssh", "authorized_keys"
        )
        if not os.path.isfile(auth_keys_path):
            return paramiko.AUTH_FAILED
        try:
            with open(auth_keys_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    import base64
                    key_data = base64.b64decode(parts[1])
                    ak = paramiko.RSAKey(data=key_data)
                    if ak == key:
                        return paramiko.AUTH_SUCCESSFUL
        except Exception:
            pass
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_channel_shell_request(self, channel):
        self.shell_event.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.exec_command = command.decode("utf-8", errors="replace")
        self.shell_event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, *args):
        return True

    def check_channel_window_change_request(self, channel, width, height, *args):
        return True


# ===========================================================================
# Apps — register everything into the shell
# ===========================================================================

def register_all(shell: "Shell") -> None:
    """
    Called once after Shell + Kernel are booted.
    Attaches all Layer 5 applications as shell builtins.
    """
    kernel  = shell.kernel
    pm      = PackageManager(shell)
    runner  = ScriptRunner(shell)

    # --- lpp (package manager) ---
    def _lpp(args: list[str]) -> int:
        if not args:
            IOManager.write(
                "usage: lpp <command> [args]\n"
                "commands: install  remove  list  search  update  info"
            )
            return 1
        cmd  = args[0]
        rest = args[1:]
        if cmd == "install": return pm.install(rest[0] if rest else "")
        if cmd == "remove":  return pm.remove(rest[0] if rest else "")
        if cmd == "list":    return pm.list_installed()
        if cmd == "search":  return pm.search(rest[0] if rest else "")
        if cmd == "update":  return pm.update(rest[0] if rest else "")
        if cmd == "info":    return pm.info(rest[0] if rest else "")
        IOManager.error(f"lpp: unknown command {cmd!r}")
        return 1

    # --- man (manual) ---
    def _man(args: list[str]) -> int:
        if not args:
            IOManager.write(f"Available manual pages:\n  {', '.join(sorted(MAN_PAGES))}")
            return 0
        return ManSystem.show(args[0])

    # --- edit (text editor) ---
    def _edit(args: list[str]) -> int:
        if not args:
            IOManager.error("edit: usage: edit <file>")
            return 1
        return TextEditor(kernel.vfs.resolve(args[0])).run()

    # --- ftp (file transfer) ---
    def _ftp(args: list[str]) -> int:
        if not args:
            IOManager.error("ftp: usage: ftp <host> [port]")
            return 1
        host = args[0]
        port = int(args[1]) if len(args) > 1 else 21
        return FTPClient(host, port).run_interactive()

    # --- sysinfo ---
    def _sysinfo(args: list[str]) -> int:
        return SysInfo.display()

    # --- sh (script runner) ---
    def _sh(args: list[str]) -> int:
        if not args:
            IOManager.error("sh: usage: sh <script.lpp> [args...]")
            return 1
        return runner.run(args[0], args[1:])

    # --- pkg create (scaffold a new package) ---
    def _pkg_create(args: list[str]) -> int:
        if not args:
            IOManager.error("pkg-create: usage: pkg-create <name>")
            return 1
        name = args[0]
        d    = kernel.vfs.resolve(name)
        os.makedirs(d, exist_ok=True)

        meta = {
            "name":        name,
            "version":     "0.1.0",
            "description": f"{name} package",
            "author":      EnvManager.username(),
            "entry":       name,
        }
        with open(os.path.join(d, "pkg.json"), "w") as f:
            json.dump(meta, f, indent=2)

        entry_py = os.path.join(d, name + ".py")
        with open(entry_py, "w") as f:
            f.write(f'''"""
{name} — linux++ package
"""
from stdlib import IOManager

def main(args):
    IOManager.write("Hello from {name}!")
    return 0
''')

        IOManager.write(
            f"Package scaffold created in ./{name}/\n"
            f"  pkg.json  — manifest\n"
            f"  {name}.py — entry point (edit this)\n\n"
            f"To install locally:\n"
            f"  cd {name} && zip -r ../{name}.lpp . && cd ..\n"
            f"  lpp install {name}.lpp"
        )
        return 0

    # --- ssh ---
    def _ssh(args: list[str]) -> int:
        if not args:
            IOManager.error(
                "ssh: usage: ssh [user@]host [port] [command]\n"
                "  examples:\n"
                "    ssh myserver.local\n"
                "    ssh root@192.168.1.10\n"
                "    ssh user@host 2222\n"
                "    ssh user@host ls -la"
            )
            return 1

        # parse  [user@]host  [port]  [command...]
        target  = args[0]
        rest    = args[1:]

        # split user@host
        if "@" in target:
            user, host = target.split("@", 1)
        else:
            user = EnvManager.username()
            host = target

        # optional port (second arg if it's a number)
        port = 22
        if rest and rest[0].isdigit():
            port = int(rest[0])
            rest = rest[1:]

        # remaining args = remote command (optional)
        command = " ".join(rest) if rest else None

        return SSHClient(host, user, port).run_interactive(command)

    # --- register all ---
    shell.builtins.register("lpp",        _lpp)
    shell.builtins.register("man",        _man)
    shell.builtins.register("edit",       _edit)
    shell.builtins.register("ftp",        _ftp)
    shell.builtins.register("sysinfo",    _sysinfo)
    shell.builtins.register("neofetch",   _sysinfo)
    shell.builtins.register("sh",         _sh)
    shell.builtins.register("pkg-create", _pkg_create)
    shell.builtins.register("ssh",        _ssh)

    # --- sshd (SSH daemon) ---
    _sshd_instance = SSHDaemon(shell)

    def _sshd(args: list[str]) -> int:
        if not args:
            IOManager.write(
                "usage: sshd <command> [port]\n"
                "commands:\n"
                "  sshd start [port]   start daemon (default port 2222)\n"
                "  sshd stop           stop daemon\n"
                "  sshd status         show status and connected clients\n"
                "  sshd keygen         regenerate host key\n"
            )
            return 1
        cmd  = args[0]
        rest = args[1:]
        if cmd == "start":
            port = int(rest[0]) if rest and rest[0].isdigit() else SSHDaemon.DEFAULT_PORT
            return _sshd_instance.start(port)
        if cmd == "stop":
            return _sshd_instance.stop()
        if cmd == "status":
            return _sshd_instance.status()
        if cmd == "keygen":
            return _sshd_instance.keygen()
        IOManager.error(f"sshd: unknown command {cmd!r}")
        return 1

    shell.builtins.register("sshd", _sshd)

    # load previously installed packages
    pm.load_installed()


# ===========================================================================
# Entry point — boots the full OS
# ===========================================================================

def main():
    from .kernel import Kernel
    from .shell  import Shell

    k = Kernel()
    k.boot()

    shell = Shell(k)
    register_all(shell)         # attach Layer 5

    sys.exit(shell.run())


if __name__ == "__main__":
    main()