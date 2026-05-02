""""""

import os

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager, EnvManager
    from ..shell import Shell
except ImportError:
    IS_WINDOWS = os.name == "nt"


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
    