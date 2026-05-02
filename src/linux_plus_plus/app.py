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

    from .apps import *
except ImportError:
    IS_WINDOWS = os.name == "nt"


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

OPTIONS
    -a   advanced mode

DESCRIPTION
    Displays system information in a neofetch-style layout.
    Advanced mode adds terminal resolution, local IP, CPU core counts,
    process counts, boot time, and detailed platform metadata.""",

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
        return SysInfo.display(args)

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

    # --- license ---
    def _license(args: list[str]) -> int:
        """Display the project's LICENSE file contents."""
        # try a few likely locations: package root, project root, cwd
        candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "LICENSE")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "LICENSE")),
            os.path.abspath(os.path.join(os.getcwd(), "LICENSE")),
        ]
        for p in candidates:
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        IOManager.write(f.read())
                    return 0
                except Exception as e:
                    IOManager.error(f"license: error reading LICENSE: {e}")
                    return 1
        IOManager.error("license: LICENSE file not found")
        return 1

    shell.builtins.register("license", _license)

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
    register_all(shell) # attach Layer 5

    sys.exit(shell.run())


if __name__ == "__main__":
    main()
