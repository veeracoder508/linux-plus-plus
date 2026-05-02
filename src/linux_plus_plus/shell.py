"""
linux++ — Shell / REPL (Layer 4)
==================================
Pure Python standard library only. No third-party deps.
Depends on Layer 1 (HAL), Layer 2 (Stdlib), Layer 3 (Kernel).

Components:
  - Lexer          : tokenise raw input into words, operators, strings
  - Parser         : build a CommandAST (pipes, redirects, &&, ||, ;, &)
  - Expander       : variable/tilde/glob expansion before execution
  - Dispatcher     : route commands to builtins or kernel exec
  - History        : persistent command history (~/.linuxpp_history)
  - Shell          : the REPL loop — prompt, read, parse, execute
"""

import os
import sys
import glob
import fnmatch
import atexit
import re

# ---------------------------------------------------------------------------
# Cross-platform readline shim
# readline   -> Unix/macOS (built into CPython)
# pyreadline3 -> Windows (pip install pyreadline3)
# fallback   -> no history/completion, but shell still works
# ---------------------------------------------------------------------------
try:
    import readline
    _READLINE = True
except ImportError:
    try:
        import pyreadline3 as readline  # type: ignore
        _READLINE = True
    except ImportError:
        # Stub so the rest of the code doesn't need guards everywhere
        class _ReadlineStub:
            def read_history_file(self, path): pass
            def write_history_file(self, path): pass
            def set_history_length(self, n): pass
            def get_current_history_length(self): return 0
            def get_history_item(self, i): return ""
            def set_completer(self, fn): pass
            def parse_and_bind(self, s): pass

        readline = _ReadlineStub()  # type: ignore
        _READLINE = False
from typing  import Optional
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Color — ANSI color helper (works on Linux, macOS, and Windows 10+)
# ---------------------------------------------------------------------------

class Color:
    """
    Central color palette for linux++ output.
    All builtins use this — never raw ANSI strings.
    On Windows, colors are enabled by Shell._enable_windows_ansi() at boot.
    """
    # styles
    BOLD      = "\033[1m"
    DIM       = "\033[2m"
    UNDERLINE = "\033[4m"
    RESET     = "\033[0m"

    # foreground colors
    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"

    # bright foreground
    BRED    = "\033[91m"
    BGREEN  = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE   = "\033[94m"
    BMAGENTA= "\033[95m"
    BCYAN   = "\033[96m"
    BWHITE  = "\033[97m"

    # background
    BG_RED    = "\033[41m"
    BG_GREEN  = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"
    BG_CYAN   = "\033[46m"

    @staticmethod
    def c(code: str, text: str) -> str:
        """Wrap text in a color code and reset."""
        return f"{code}{text}{Color.RESET}"

    # convenience wrappers
    @staticmethod
    def red(t):     return Color.c(Color.RED,     t)
    @staticmethod
    def green(t):   return Color.c(Color.GREEN,   t)
    @staticmethod
    def yellow(t):  return Color.c(Color.YELLOW,  t)
    @staticmethod
    def blue(t):    return Color.c(Color.BLUE,    t)
    @staticmethod
    def cyan(t):    return Color.c(Color.CYAN,    t)
    @staticmethod
    def magenta(t): return Color.c(Color.MAGENTA, t)
    @staticmethod
    def bold(t):    return Color.c(Color.BOLD,    t)
    @staticmethod
    def dim(t):     return Color.c(Color.DIM,     t)
    @staticmethod
    def bred(t):    return Color.c(Color.BRED,    t)
    @staticmethod
    def bgreen(t):  return Color.c(Color.BGREEN,  t)
    @staticmethod
    def byellow(t): return Color.c(Color.BYELLOW, t)
    @staticmethod
    def bcyan(t):   return Color.c(Color.BCYAN,   t)

    @staticmethod
    def error(msg: str) -> str:
        return f"{Color.BOLD}{Color.RED}error:{Color.RESET} {msg}"

    @staticmethod
    def success(msg: str) -> str:
        return f"{Color.BOLD}{Color.GREEN}✔{Color.RESET} {msg}"

    @staticmethod
    def warn(msg: str) -> str:
        return f"{Color.BOLD}{Color.YELLOW}warn:{Color.RESET} {msg}"

    @staticmethod
    def info(msg: str) -> str:
        return f"{Color.BOLD}{Color.CYAN}info:{Color.RESET} {msg}"

    @staticmethod
    def header(msg: str) -> str:
        return f"{Color.BOLD}{Color.BWHITE}{msg}{Color.RESET}"

    @staticmethod
    def strip(text: str) -> str:
        """Remove all ANSI codes from a string (for pipes/redirects)."""
        return re.sub(r'\033\[[0-9;]*m', '', text)
from enum    import Enum, auto

try:
    from .hal    import IS_WINDOWS
    from .stdlib import IOManager, EnvManager, SignalHandler, AliasStore
    from .kernel import Kernel, SyscallError, INodeType
except ImportError:
    IS_WINDOWS = os.name == "nt"
    Kernel = None


# ===========================================================================
# Lexer
# ===========================================================================

class TT(Enum):
    """Token types."""
    WORD      = auto()   # any word / argument
    PIPE      = auto()   # |
    REDIR_OUT = auto()   # >
    REDIR_APP = auto()   # >>
    REDIR_IN  = auto()   # <
    AND       = auto()   # &&
    OR        = auto()   # ||
    SEMI      = auto()   # ;
    BG        = auto()   # &  (background)
    LPAREN    = auto()   # (
    RPAREN    = auto()   # )
    EOF       = auto()


@dataclass
class Token:
    type:  TT
    value: str = ""

    def __repr__(self):
        return f"Token({self.type.name}, {self.value!r})"


class LexerError(Exception):
    pass


class Lexer:
    """
    Tokenise a shell input line.
    Handles:
      - single-quoted strings   'no $expansion'
      - double-quoted strings   "with $expansion"
      - escape sequences        \n  \t  \\  \"
      - all shell operators     | || > >> < && ; & ( )
      - comments                # …
    """

    def tokenise(self, text: str) -> list[Token]:
        tokens: list[Token] = []
        i = 0
        n = len(text)

        while i < n:
            c = text[i]

            # whitespace
            if c in " \t\r":
                i += 1
                continue

            # comment
            if c == "#":
                break

            # single-char operators that need peek-ahead
            if c == "|":
                if i + 1 < n and text[i+1] == "|":
                    tokens.append(Token(TT.OR, "||")); i += 2
                else:
                    tokens.append(Token(TT.PIPE, "|")); i += 1
                continue

            if c == "&":
                if i + 1 < n and text[i+1] == "&":
                    tokens.append(Token(TT.AND, "&&")); i += 2
                else:
                    tokens.append(Token(TT.BG, "&")); i += 1
                continue

            if c == ">":
                if i + 1 < n and text[i+1] == ">":
                    tokens.append(Token(TT.REDIR_APP, ">>")); i += 2
                else:
                    tokens.append(Token(TT.REDIR_OUT, ">")); i += 1
                continue

            if c == "<":
                tokens.append(Token(TT.REDIR_IN, "<")); i += 1
                continue

            if c == ";":
                tokens.append(Token(TT.SEMI, ";")); i += 1
                continue

            if c == "(":
                tokens.append(Token(TT.LPAREN, "(")); i += 1
                continue

            if c == ")":
                tokens.append(Token(TT.RPAREN, ")")); i += 1
                continue

            # quoted string
            if c == "'":
                word, i = self._read_single_quote(text, i + 1, n)
                tokens.append(Token(TT.WORD, word))
                continue

            if c == '"':
                word, i = self._read_double_quote(text, i + 1, n)
                tokens.append(Token(TT.WORD, word))
                continue

            # bare word
            word, i = self._read_word(text, i, n)
            tokens.append(Token(TT.WORD, word))

        tokens.append(Token(TT.EOF))
        return tokens

    def _read_word(self, text: str, i: int, n: int) -> tuple[str, int]:
        buf = []
        stops = set(" \t\r|&>;()<\"'#")
        while i < n and text[i] not in stops:
            if text[i] == "\\":
                i += 1
                if i < n:
                    buf.append(text[i]); i += 1
            else:
                buf.append(text[i]); i += 1
        return "".join(buf), i

    def _read_single_quote(self, text: str, i: int, n: int) -> tuple[str, int]:
        buf = []
        while i < n and text[i] != "'":
            buf.append(text[i]); i += 1
        if i < n:
            i += 1  # consume closing '
        return "".join(buf), i

    def _read_double_quote(self, text: str, i: int, n: int) -> tuple[str, int]:
        buf = []
        while i < n and text[i] != '"':
            if text[i] == "\\" and i + 1 < n:
                nxt = text[i+1]
                if nxt in ('"', '\\', '$', '\n'):
                    buf.append(nxt); i += 2
                else:
                    buf.append(text[i]); i += 1
            else:
                buf.append(text[i]); i += 1
        if i < n:
            i += 1  # consume closing "
        return "".join(buf), i


# ===========================================================================
# Syntax Highlighter
# ===========================================================================

class SyntaxHighlighter:
    """
    Token-based syntax highlighter for the linux++ shell.
    Preserves whitespace and highlights according to command context.
    """

    @staticmethod
    def highlight(line: str, builtins: set = None) -> str:
        if builtins is None:
            builtins = set()

        # Define token types for highlighting
        token_spec = [
            ('COMMENT',  r'#.*'),
            ('STRING',   r'"[^"]*"|\'[^\']*\''),
            ('VAR',      r'\$[A-Za-z0-9_?{}$#]+'),
            ('FLAG',     r'--?[A-Za-z0-9-]+'),
            ('OPERATOR', r'\|\||&&|>>|>|<|\||;|&|\(|\)'),
            ('WORD',     r'[^\s|&>;()<"\']+'),
            ('SPACE',    r'\s+'),
        ]
        tok_regex = '|'.join('(?P<%s>%s)' % pair for pair in token_spec)
        
        result = []
        is_first_word = True
        
        for mo in re.finditer(tok_regex, line):
            kind = mo.lastgroup
            val  = mo.group(kind)
            
            if kind == 'COMMENT':
                result.append(Color.dim(val))
            elif kind == 'STRING':
                result.append(Color.byellow(val))
            elif kind == 'VAR':
                result.append(Color.bmagenta(val))
            elif kind == 'FLAG':
                result.append(Color.cyan(val))
            elif kind == 'OPERATOR':
                result.append(Color.bcyan(val))
                is_first_word = True # Command likely follows
            elif kind == 'WORD':
                if is_first_word and val in builtins:
                    result.append(Color.bgreen(val))
                else:
                    result.append(val)
                is_first_word = False
            else: # SPACE
                result.append(val)
        
        return "".join(result)


# ===========================================================================
# AST nodes
# ===========================================================================

@dataclass
class Redirect:
    type:   TT     # REDIR_OUT | REDIR_APP | REDIR_IN
    target: str    # filename


@dataclass
class SimpleCommand:
    words:     list[str]         = field(default_factory=list)
    redirects: list[Redirect]    = field(default_factory=list)
    background: bool             = False


@dataclass
class Pipeline:
    commands:   list[SimpleCommand] = field(default_factory=list)
    background: bool                = False


@dataclass
class CommandList:
    """A list of pipelines joined by ;  &&  ||"""
    items: list[tuple[str, Pipeline]] = field(default_factory=list)
    # items = [ ("",  pipeline0),
    #           ("&&",pipeline1),
    #           ("||",pipeline2), … ]


# ===========================================================================
# Parser
# ===========================================================================

class ParseError(Exception):
    pass


class Parser:
    """
    Recursive-descent parser for the token stream produced by Lexer.

    Grammar (simplified):
        command_list  := pipeline ( (';'|'&&'|'||') pipeline )*
        pipeline      := simple_cmd ( '|' simple_cmd )* ['&']
        simple_cmd    := WORD* redirect*
        redirect      := ('>'|'>>'|'<') WORD
    """

    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._pos    = 0

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _consume(self, expected: Optional[TT] = None) -> Token:
        tok = self._tokens[self._pos]
        if expected and tok.type != expected:
            raise ParseError(f"Expected {expected.name}, got {tok.type.name}")
        self._pos += 1
        return tok

    def parse(self) -> CommandList:
        cl = CommandList()
        if self._peek().type == TT.EOF:
            return cl

        # first pipeline has no leading operator
        pl = self._parse_pipeline()
        cl.items.append(("", pl))

        while self._peek().type in (TT.SEMI, TT.AND, TT.OR):
            op = self._consume().value
            if self._peek().type == TT.EOF:
                break
            pl = self._parse_pipeline()
            cl.items.append((op, pl))

        return cl

    def _parse_pipeline(self) -> Pipeline:
        pl = Pipeline()
        pl.commands.append(self._parse_simple())

        while self._peek().type == TT.PIPE:
            self._consume(TT.PIPE)
            pl.commands.append(self._parse_simple())

        if self._peek().type == TT.BG:
            self._consume(TT.BG)
            pl.background = True

        return pl

    def _parse_simple(self) -> SimpleCommand:
        cmd = SimpleCommand()

        while self._peek().type not in (
            TT.PIPE, TT.AND, TT.OR, TT.SEMI, TT.BG, TT.EOF, TT.RPAREN
        ):
            tok = self._peek()
            if tok.type == TT.WORD:
                cmd.words.append(self._consume().value)
            elif tok.type in (TT.REDIR_OUT, TT.REDIR_APP, TT.REDIR_IN):
                rtype = self._consume().type
                if self._peek().type != TT.WORD:
                    raise ParseError("Expected filename after redirect")
                target = self._consume().value
                cmd.redirects.append(Redirect(rtype, target))
            else:
                break

        return cmd


# ===========================================================================
# Expander  — variable, tilde, glob expansion
# ===========================================================================

class Expander:
    """
    Expand words before execution:
      - tilde:    ~ -> home dir
      - variables: $VAR  ${VAR}  $?  $$  $#
      - globs:    *  ?  [abc]
    Uses only: os, glob, re — pure builtins.
    """

    def __init__(self, last_rc: int = 0):
        self.last_rc = last_rc

    def expand_word(self, word: str) -> list[str]:
        """Expand a single word into one or more words (globs can split)."""
        word = self._expand_tilde(word)
        word = self._expand_vars(word)
        expanded = self._expand_glob(word)
        return expanded if expanded else [word]

    def expand_words(self, words: list[str]) -> list[str]:
        result = []
        for w in words:
            result.extend(self.expand_word(w))
        return result

    def _expand_tilde(self, word: str) -> str:
        if word == "~" or word.startswith("~/") or word.startswith("~\\"):
            return os.path.expanduser(word)
        return word

    def _expand_vars(self, word: str) -> str:
        # $?  last return code
        word = word.replace("$?", str(self.last_rc))
        # $$  shell PID
        word = word.replace("$$", str(os.getpid()))

        # ${VAR} and $VAR
        def _replace(m):
            key = m.group(1) or m.group(2)
            return EnvManager.get(key, "")

        word = re.sub(r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)', _replace, word)
        return word

    def _expand_glob(self, word: str) -> list[str]:
        if not any(c in word for c in ("*", "?", "[")):
            return [word]
        matches = glob.glob(word)
        return sorted(matches) if matches else [word]


# ===========================================================================
# Command History
# ===========================================================================

class History:
    """
    Persistent readline history stored in ~/.linuxpp_history.
    Uses only: readline, atexit, os — pure builtins.
    """

    DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".linuxpp_history")
    MAX_ENTRIES  = 1000

    def __init__(self, path: Optional[str] = None):
        self._path = path or self.DEFAULT_PATH
        self._session: list[str] = []

    def load(self) -> None:
        try:
            readline.read_history_file(self._path)
            readline.set_history_length(self.MAX_ENTRIES)
        except FileNotFoundError:
            pass
        atexit.register(self.save)

    def save(self) -> None:
        try:
            readline.write_history_file(self._path)
        except OSError:
            pass

    def add(self, line: str) -> None:
        if line.strip():
            self._session.append(line)

    def get_all(self) -> list[str]:
        out = []
        for i in range(readline.get_current_history_length()):
            out.append(readline.get_history_item(i + 1))
        return out


# ===========================================================================
# Builtin registry
# ===========================================================================

class BuiltinRegistry:
    """
    Holds the built-in commands that run inside the shell process.
    Layer 5 registers additional builtins via register().
    """

    def __init__(self, shell: "Shell"):
        self._shell = shell
        self._builtins: dict[str, callable] = {}
        self._register_core()

    def register(self, name: str, fn: callable) -> None:
        self._builtins[name] = fn

    def has(self, name: str) -> bool:
        return name in self._builtins

    def run(self, name: str, args: list[str]) -> int:
        return self._builtins[name](args)

    # --- core builtins ---

    def _register_core(self) -> None:
        sh = self._shell
        b  = self._builtins

        b["cd"]      = self._cd
        b["pwd"]     = self._pwd
        b["exit"]    = self._exit
        b["quit"]    = self._exit
        b["echo"]    = self._echo
        b["export"]  = self._export
        b["unset"]   = self._unset
        b["env"]     = self._env
        b["alias"]   = self._alias
        b["unalias"] = self._unalias
        b["history"] = self._history
        b["jobs"]    = self._jobs
        b["kill"]    = self._kill
        b["wait"]    = self._wait
        b["source"]  = self._source
        b["help"]    = self._help
        b["clear"]   = self._clear
        b["ls"]      = self._ls
        b["ll"]      = self._ll
        b["cat"]     = self._cat
        b["mkdir"]   = self._mkdir
        b["rm"]      = self._rm
        b["cp"]      = self._cp
        b["mv"]      = self._mv
        b["touch"]   = self._touch
        b["grep"]    = self._grep
        b["head"]    = self._head
        b["tail"]    = self._tail
        b["wc"]      = self._wc
        b["which"]   = self._which
        b["type"]    = self._type
        b["highlight"] = self._highlight

    def _cd(self, args: list[str]) -> int:
        target = args[0] if args else os.path.expanduser("~")
        try:
            self._shell.kernel.syscall.chdir(target)
            EnvManager.set("PWD", self._shell.kernel.syscall.getcwd(), export=True)
            return 0
        except SyscallError as e:
            IOManager.error(Color.error(f"cd: {e}"))
            return 1

    def _pwd(self, args: list[str]) -> int:
        IOManager.write(Color.bgreen(self._shell.kernel.syscall.getcwd()))
        return 0

    def _exit(self, args: list[str]) -> int:
        code = int(args[0]) if args else 0
        self._shell.running = False
        self._shell._exit_code = code
        IOManager.write(Color.dim(f"exit ({code})"))
        return code

    def _echo(self, args: list[str]) -> int:
        newline = True
        if args and args[0] == "-n":
            newline = False
            args = args[1:]
        text = " ".join(args)
        text = text.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        IOManager.write(text, end="\n" if newline else "")
        return 0

    def _export(self, args: list[str]) -> int:
        for arg in args:
            if "=" in arg:
                k, v = arg.split("=", 1)
                EnvManager.set(k, v, export=True)
                IOManager.write(f"  {Color.cyan(k)} = {Color.yellow(v)}")
            else:
                v = EnvManager.get(arg, "")
                EnvManager.set(arg, v, export=True)
        return 0

    def _unset(self, args: list[str]) -> int:
        for arg in args:
            EnvManager.unset(arg)
            IOManager.write(Color.dim(f"  unset {arg}"))
        return 0

    def _env(self, args: list[str]) -> int:
        for k, v in sorted(EnvManager.all().items()):
            IOManager.write(f"{Color.cyan(k)}={Color.yellow(v)}")
        return 0

    def _alias(self, args: list[str]) -> int:
        if not args:
            for k, v in sorted(AliasStore.all().items()):
                IOManager.write(f"{Color.bold('alias')} {Color.green(k)}={Color.yellow(repr(v))}")
            return 0
        for arg in args:
            if "=" in arg:
                k, v = arg.split("=", 1)
                v = v.strip("'\"")
                AliasStore.set(k, v)
                IOManager.write(Color.success(f"alias {k}='{v}'"))
            else:
                v = AliasStore.get(arg)
                if v:
                    IOManager.write(f"{Color.bold('alias')} {Color.green(arg)}={Color.yellow(repr(v))}")
                else:
                    IOManager.error(Color.error(f"alias: {arg}: not found"))
        return 0

    def _unalias(self, args: list[str]) -> int:
        for arg in args:
            AliasStore.unset(arg)
            IOManager.write(Color.dim(f"  unalias {arg}"))
        return 0

    def _history(self, args: list[str]) -> int:
        entries = self._shell.history.get_all()
        n = int(args[0]) if args else len(entries)
        for i, entry in enumerate(entries[-n:], start=max(1, len(entries)-n+1)):
            IOManager.write(f"  {Color.dim(f'{i:4}')}  {entry}")
        return 0

    def _jobs(self, args: list[str]) -> int:
        jobs = self._shell.kernel.syscall.jobs()
        if not jobs:
            IOManager.write(Color.dim("No active jobs."))
            return 0
        for job in jobs:
            state_color = Color.green if "DONE" in str(job) else Color.yellow
            parts = str(job).split(None, 3)
            if len(parts) == 4:
                jid, state, pid, cmd = parts
                IOManager.write(
                    f"{Color.cyan(jid)} {state_color(state):<18} "
                    f"{Color.dim(pid)}  {Color.bold(cmd)}"
                )
            else:
                IOManager.write(str(job))
        return 0

    def _kill(self, args: list[str]) -> int:
        if not args:
            IOManager.error(Color.error("kill: usage: kill [-9] <job_id|pid>"))
            return 1
        force = False
        if args[0] == "-9":
            force = True
            args = args[1:]
        for arg in args:
            try:
                job_id = int(arg.lstrip("%"))
                ok = self._shell.kernel.syscall.kill(job_id, force=force)
                if not ok:
                    import signal as _sig
                    os.kill(job_id, _sig.SIGKILL if force else _sig.SIGTERM)
                IOManager.write(Color.success(f"killed {arg}"))
            except (ValueError, ProcessLookupError, PermissionError) as e:
                IOManager.error(Color.error(f"kill: {e}"))
                return 1
        return 0

    def _wait(self, args: list[str]) -> int:
        if not args:
            IOManager.error(Color.error("wait: usage: wait <job_id>"))
            return 1
        try:
            job_id = int(args[0].lstrip("%"))
            rc = self._shell.kernel.syscall.wait(job_id, timeout=None)
            return rc if rc is not None else 0
        except ValueError:
            IOManager.error(Color.error(f"wait: invalid job id: {args[0]}"))
            return 1

    def _source(self, args: list[str]) -> int:
        if not args:
            IOManager.error(Color.error("source: usage: source <file>"))
            return 1
        path = self._shell.kernel.vfs.resolve(args[0])
        try:
            content = self._shell.kernel.syscall.open(path)
        except SyscallError as e:
            IOManager.error(Color.error(f"source: {e}"))
            return 1
        rc = 0
        for line in content.splitlines():
            rc = self._shell.execute_line(line)
        return rc

    def _help(self, args: list[str]) -> int:
        builtins = sorted(self._builtins.keys())
        IOManager.write(Color.header("linux++ built-in commands") + "\n")
        # display in colored columns
        cols = 4
        max_w = max(len(b) for b in builtins) + 2
        for i in range(0, len(builtins), cols):
            row = builtins[i:i+cols]
            IOManager.write("  " + "".join(Color.green(b.ljust(max_w)) for b in row))
        IOManager.write(
            f"\n{Color.dim('For external commands, linux++ searches PATH.')}\n"
            f"{Color.dim('Use')} {Color.cyan('man <command>')} "
            f"{Color.dim('for detailed help.')}"
        )
        return 0

    def _clear(self, args: list[str]) -> int:
        os.system("cls" if IS_WINDOWS else "clear")
        return 0

    # -----------------------------------------------------------------------
    # Cross-platform filesystem builtins
    # All implemented in pure Python — never call ls/dir/cat/etc externally.
    # -----------------------------------------------------------------------

    def _ls(self, args: list[str]) -> int:
        """ls [-la] [path ...]"""
        import stat as _stat

        long_fmt = False
        all_files = False
        paths = []

        for arg in args:
            if arg.startswith("-"):
                if "l" in arg: long_fmt  = True
                if "a" in arg: all_files = True
            else:
                paths.append(arg)

        if not paths:
            paths = [self._shell.kernel.syscall.getcwd()]

        def _fmt_size(n: int) -> str:
            for unit in ("B", "K", "M", "G", "T"):
                if n < 1024:
                    return f"{n:>6}{unit}"
                n //= 1024
            return f"{n:>6}P"

        def _perms(mode: int) -> str:
            flags = [
                (_stat.S_IFDIR,  "d"), (_stat.S_IFLNK, "l"),
            ]
            kind = "-"
            for f, c in flags:
                if _stat.S_IFMT(mode) == f:
                    kind = c
                    break
            bits = [
                (_stat.S_IRUSR,"r"),(_stat.S_IWUSR,"w"),(_stat.S_IXUSR,"x"),
                (_stat.S_IRGRP,"r"),(_stat.S_IWGRP,"w"),(_stat.S_IXGRP,"x"),
                (_stat.S_IROTH,"r"),(_stat.S_IWOTH,"w"),(_stat.S_IXOTH,"x"),
            ]
            return kind + "".join(c if mode & f else "-" for f, c in bits)

        def _color(name: str, is_dir: bool, is_link: bool, is_exec: bool) -> str:
            if IS_WINDOWS:
                return name
            if is_link:   return f"\033[36m{name}\033[0m"
            if is_dir:    return f"\033[34;1m{name}\033[0m"
            if is_exec:   return f"\033[32m{name}\033[0m"
            return name

        for target in paths:
            resolved = self._shell.kernel.vfs.resolve(target)

            if os.path.isfile(resolved):
                entries = [resolved]
                is_single_file = True
            else:
                try:
                    raw = os.listdir(resolved)
                except PermissionError:
                    IOManager.error(f"ls: cannot open '{target}': Permission denied")
                    return 1
                except FileNotFoundError as e:
                    IOManager.error(Color.error(f"ls: {e}"))
                    return 1
                entries = sorted(raw, key=str.lower)
                is_single_file = False
                

            if len(paths) > 1:
                IOManager.write(f"{target}:")

            if long_fmt:
                import time as _time
                for name in entries:
                    if not all_files and name.startswith("."):
                        continue
                    full = name if is_single_file else os.path.join(resolved, name)
                    try:
                        st      = os.lstat(full)
                        is_dir  = _stat.S_ISDIR(st.st_mode)
                        is_link = _stat.S_ISLNK(st.st_mode)
                        is_exec = bool(st.st_mode & 0o111)
                        perm    = _perms(st.st_mode)
                        size    = _fmt_size(st.st_size)
                        mtime   = _time.strftime("%b %d %H:%M",
                                    _time.localtime(st.st_mtime))
                        display = _color(os.path.basename(full),
                                         is_dir, is_link, is_exec)
                        link_part = ""
                        if is_link:
                            try:
                                link_part = f" -> {os.readlink(full)}"
                            except OSError:
                                pass
                        IOManager.write(
                            f"{perm}  {size}  {mtime}  {display}{link_part}"
                        )
                    except OSError:
                        IOManager.write(f"??????  ??????  ?????? {name}")
            else:
                # columnar output
                visible = [
                    n for n in entries
                    if all_files or not (n if is_single_file
                                         else n).startswith(".")
                ]
                if not visible:
                    continue
                names = []
                for name in visible:
                    full    = name if is_single_file else os.path.join(resolved, name)
                    base    = os.path.basename(full)
                    is_dir  = os.path.isdir(full)
                    is_link = os.path.islink(full)
                    is_exec = os.access(full, os.X_OK) and not is_dir
                    names.append(_color(base, is_dir, is_link, is_exec))

                try:
                    term_w = os.get_terminal_size().columns
                except OSError:
                    term_w = 80

                max_len  = max(len(n) for n in visible) + 2
                cols     = max(1, term_w // max_len)
                for i in range(0, len(names), cols):
                    row = names[i:i+cols]
                    IOManager.write("  ".join(n.ljust(max_len) for n in row))

        return 0

    def _ll(self, args: list[str]) -> int:
        """ll — shortcut for ls -la"""
        return self._ls(["-la"] + args)

    def _cat(self, args: list[str]) -> int:
        """cat [-n] [file ...]"""
        number = False
        files  = []
        for arg in args:
            if arg == "-n":
                number = True
            else:
                files.append(arg)

        if not files:
            try:
                data = sys.stdin.read()
                IOManager.write(data, end="")
            except KeyboardInterrupt:
                pass
            return 0

        for f in files:
            resolved = self._shell.kernel.vfs.resolve(f)
            try:
                fd = os.open(resolved, os.O_RDONLY)
                chunks = []
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                os.close(fd)
                content = b"".join(chunks).decode("utf-8", errors="replace")
                if len(files) > 1:
                    IOManager.write(f"{Color.bold(Color.CYAN + f + Color.RESET)}")
                    IOManager.write(Color.dim("─" * min(len(f) + 4, 60)))
                if number:
                    for i, line in enumerate(content.splitlines(), 1):
                        IOManager.write(f"  {Color.dim(f'{i:6}')}  {line}")
                else:
                    IOManager.write(content, end="")
            except FileNotFoundError:
                IOManager.error(Color.error(f"cat: {f}: No such file or directory"))
                return 1
            except PermissionError:
                IOManager.error(Color.error(f"cat: {f}: Permission denied"))
                return 1
        return 0

    def _mkdir(self, args: list[str]) -> int:
        """mkdir [-p] <dir> ..."""
        parents = False
        dirs    = []
        for arg in args:
            if arg == "-p": parents = True
            else:           dirs.append(arg)
        if not dirs:
            IOManager.error(Color.error("mkdir: missing operand"))
            return 1
        for d in dirs:
            try:
                self._shell.kernel.syscall.mkdir(d, parents=parents)
                IOManager.write(Color.success(f"mkdir: created '{Color.cyan(d)}'"))
            except SyscallError as e:
                IOManager.error(Color.error(f"mkdir: {e}"))
                return 1
        return 0

    def _rm(self, args: list[str]) -> int:
        """rm [-rf] <path> ..."""
        recursive = False
        force     = False
        paths     = []
        for arg in args:
            if arg.startswith("-"):
                if "r" in arg or "R" in arg: recursive = True
                if "f" in arg:               force     = True
            else:
                paths.append(arg)
        if not paths:
            IOManager.error(Color.error("rm: missing operand"))
            return 1
        for p in paths:
            try:
                self._shell.kernel.syscall.unlink(p, recursive=recursive)
                IOManager.write(Color.dim(f"  removed '{p}'"))
            except SyscallError as e:
                if not force:
                    IOManager.error(Color.error(f"rm: {e}"))
                    return 1
        return 0

    def _cp(self, args: list[str]) -> int:
        """cp <src> <dst>"""
        if len(args) < 2:
            IOManager.error(Color.error("cp: missing operand — usage: cp <src> <dst>"))
            return 1
        try:
            self._shell.kernel.syscall.copy(args[-2], args[-1])
            IOManager.write(Color.success(
                f"'{Color.cyan(args[-2])}' → '{Color.cyan(args[-1])}'"
            ))
        except SyscallError as e:
            IOManager.error(Color.error(f"cp: {e}"))
            return 1
        return 0

    def _mv(self, args: list[str]) -> int:
        """mv <src> <dst>"""
        if len(args) < 2:
            IOManager.error(Color.error("mv: missing operand — usage: mv <src> <dst>"))
            return 1
        try:
            self._shell.kernel.syscall.rename(args[-2], args[-1])
            IOManager.write(Color.success(
                f"'{Color.cyan(args[-2])}' → '{Color.cyan(args[-1])}'"
            ))
        except SyscallError as e:
            IOManager.error(Color.error(f"mv: {e}"))
            return 1
        return 0

    def _touch(self, args: list[str]) -> int:
        """touch <file> ..."""
        if not args:
            IOManager.error(Color.error("touch: missing operand"))
            return 1
        for f in args:
            try:
                self._shell.kernel.syscall.touch(f)
                IOManager.write(Color.dim(f"  touched '{f}'"))
            except SyscallError as e:
                IOManager.error(Color.error(f"touch: {e}"))
                return 1
        return 0

    def _grep(self, args: list[str]) -> int:
        """grep [-rin] <pattern> [file ...]"""
        import re as _re

        ignore_case    = False
        show_line_nums = False
        invert         = False
        pattern_set    = False
        pattern        = ""
        files          = []

        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("-") and not pattern_set:
                if "i" in a: ignore_case    = True
                if "n" in a: show_line_nums = True
                if "v" in a: invert         = True
            elif not pattern_set:
                pattern     = a
                pattern_set = True
            else:
                files.append(a)
            i += 1

        if not pattern_set:
            IOManager.error(Color.error("grep: usage: grep [-inv] <pattern> [file ...]"))
            return 1

        flags = _re.IGNORECASE if ignore_case else 0
        try:
            regex = _re.compile(pattern, flags)
        except _re.error as e:
            IOManager.error(Color.error(f"grep: invalid pattern: {e}"))
            return 1

        def _search(lines: list[str], label: str) -> bool:
            found = False
            for lineno, line in enumerate(lines, 1):
                match = bool(regex.search(line))
                if invert:
                    match = not match
                if match:
                    found = True
                    # build prefix
                    parts = []
                    if label:
                        parts.append(Color.magenta(label))
                    if show_line_nums:
                        parts.append(Color.cyan(str(lineno)))
                    prefix = Color.dim(":").join(parts) + Color.dim(":") if parts else ""
                    # highlight the match in the line
                    if not invert:
                        line = regex.sub(
                            lambda m: f"{Color.BOLD}{Color.BRED}{m.group()}{Color.RESET}",
                            line
                        )
                    IOManager.write(prefix + line)
            return found

        found_any = False
        if not files:
            lines = sys.stdin.read().splitlines()
            found_any = _search(lines, "")
        else:
            multi = len(files) > 1
            for f in files:
                resolved = self._shell.kernel.vfs.resolve(f)
                try:
                    fd = os.open(resolved, os.O_RDONLY)
                    data = b""
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk: break
                        data += chunk
                    os.close(fd)
                    lines = data.decode("utf-8", errors="replace").splitlines()
                    if _search(lines, f if multi else ""):
                        found_any = True
                except FileNotFoundError:
                    IOManager.error(Color.error(f"grep: {f}: No such file or directory"))
                except PermissionError:
                    IOManager.error(Color.error(f"grep: {f}: Permission denied"))

        return 0 if found_any else 1

    def _head(self, args: list[str]) -> int:
        """head [-n N] [file ...]"""
        n = 10
        files = []
        i = 0
        while i < len(args):
            if args[i] == "-n" and i + 1 < len(args):
                try: n = int(args[i+1])
                except ValueError: pass
                i += 2
            elif args[i].startswith("-") and args[i][1:].isdigit():
                n = int(args[i][1:])
                i += 1
            else:
                files.append(args[i]); i += 1

        def _print_head(lines, label):
            if label:
                IOManager.write(
                    f"{Color.BOLD}{Color.CYAN}==> {label} <=={Color.RESET}"
                )
            for line in lines[:n]:
                IOManager.write(line)

        if not files:
            _print_head(sys.stdin.read().splitlines(), "")
        else:
            for f in files:
                resolved = self._shell.kernel.vfs.resolve(f)
                try:
                    fd = os.open(resolved, os.O_RDONLY)
                    data = b""
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk: break
                        data += chunk
                    os.close(fd)
                    _print_head(data.decode("utf-8", errors="replace").splitlines(),
                                f if len(files) > 1 else "")
                except FileNotFoundError:
                    IOManager.error(Color.error(f"head: {f}: No such file or directory"))
                    return 1
        return 0

    def _tail(self, args: list[str]) -> int:
        """tail [-n N] [file ...]"""
        n = 10
        files = []
        i = 0
        while i < len(args):
            if args[i] == "-n" and i + 1 < len(args):
                try: n = int(args[i+1])
                except ValueError: pass
                i += 2
            elif args[i].startswith("-") and args[i][1:].isdigit():
                n = int(args[i][1:]); i += 1
            else:
                files.append(args[i]); i += 1

        def _print_tail(lines, label):
            if label:
                IOManager.write(
                    f"{Color.BOLD}{Color.CYAN}==> {label} <=={Color.RESET}"
                )
            for line in lines[-n:]:
                IOManager.write(line)

        if not files:
            _print_tail(sys.stdin.read().splitlines(), "")
        else:
            for f in files:
                resolved = self._shell.kernel.vfs.resolve(f)
                try:
                    fd = os.open(resolved, os.O_RDONLY)
                    data = b""
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk: break
                        data += chunk
                    os.close(fd)
                    _print_tail(data.decode("utf-8", errors="replace").splitlines(),
                                f if len(files) > 1 else "")
                except FileNotFoundError:
                    IOManager.error(Color.error(f"tail: {f}: No such file or directory"))
                    return 1
        return 0

    def _wc(self, args: list[str]) -> int:
        """wc [-lwc] [file ...]"""
        count_lines = "-l" in args or not any(a.startswith("-") for a in args)
        count_words = "-w" in args or not any(a.startswith("-") for a in args)
        count_chars = "-c" in args or not any(a.startswith("-") for a in args)
        files = [a for a in args if not a.startswith("-")]

        def _count(text):
            l = text.count("\n")
            w = len(text.split())
            c = len(text)
            parts = []
            if count_lines: parts.append(Color.cyan(f"{l:>8}"))
            if count_words: parts.append(Color.yellow(f"{w:>8}"))
            if count_chars: parts.append(Color.green(f"{c:>8}"))
            return " ".join(parts), l, w, c

        if not files:
            text = sys.stdin.read()
            summary, *_ = _count(text)
            IOManager.write(summary)
            return 0

        totals = [0, 0, 0]
        # header
        cols = []
        if count_lines: cols.append(Color.dim(f"{'lines':>8}"))
        if count_words: cols.append(Color.dim(f"{'words':>8}"))
        if count_chars: cols.append(Color.dim(f"{'chars':>8}"))
        IOManager.write(" ".join(cols) + Color.dim("  file"))

        for f in files:
            resolved = self._shell.kernel.vfs.resolve(f)
            try:
                fd = os.open(resolved, os.O_RDONLY)
                data = b""
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk: break
                    data += chunk
                os.close(fd)
                text = data.decode("utf-8", errors="replace")
                summary, l, w, c = _count(text)
                IOManager.write(f"{summary}  {Color.bcyan(f)}")
                totals[0] += l; totals[1] += w; totals[2] += c
            except FileNotFoundError:
                IOManager.error(Color.error(f"wc: {f}: No such file or directory"))
                return 1

        if len(files) > 1:
            parts = []
            if count_lines: parts.append(Color.bold(Color.cyan(f"{totals[0]:>8}")))
            if count_words: parts.append(Color.bold(Color.yellow(f"{totals[1]:>8}")))
            if count_chars: parts.append(Color.bold(Color.green(f"{totals[2]:>8}")))
            IOManager.write(" ".join(parts) + Color.dim("  total"))
        return 0

    def _which(self, args: list[str]) -> int:
        """which <command> ..."""
        if not args:
            IOManager.error(Color.error("which: missing argument"))
            return 1
        rc = 0
        for name in args:
            found = EnvManager.resolve_command(name)
            if found:
                IOManager.write(f"{Color.green(name)}: {Color.cyan(found)}")
            else:
                IOManager.error(Color.warn(f"which: {name}: not found"))
                rc = 1
        return rc

    def _type(self, args: list[str]) -> int:
        """type <name> — show whether name is builtin, alias, or external"""
        if not args:
            IOManager.error(Color.error("type: missing argument"))
            return 1
        rc = 0
        for name in args:
            if self.has(name):
                IOManager.write(
                    f"{Color.bold(name)} is a {Color.yellow('shell builtin')}"
                )
            elif AliasStore.get(name):
                exp = AliasStore.get(name)
                IOManager.write(
                    f"{Color.bold(name)} is aliased to "
                    f"{Color.cyan(repr(exp))}"
                )
            else:
                found = EnvManager.resolve_command(name)
                if found:
                    IOManager.write(
                        f"{Color.bold(name)} is {Color.green(found)}"
                    )
                else:
                    IOManager.error(Color.warn(f"type: {name}: not found"))
                    rc = 1
        return rc

    def _highlight(self, args: list[str]) -> int:
        if not args:
            IOManager.error(Color.error("highlight: usage: highlight <cmd>"))
            return 1
        line = " ".join(args)
        IOManager.write(SyntaxHighlighter.highlight(line, set(self._builtins.keys())))


# ===========================================================================
# Dispatcher  — decide where each command goes
# ===========================================================================

class Dispatcher:
    """
    For each SimpleCommand, decides:
      1. Is it a builtin?  -> run inside shell process
      2. Is it in PATH?    -> exec via kernel
      3. Neither           -> error
    Handles redirections around the call.
    """

    def __init__(self, shell: "Shell"):
        self._shell = shell

    def dispatch(
        self,
        cmd:     SimpleCommand,
        stdin:   Optional[str] = None,
        capture: bool          = False,
    ) -> tuple[int, str, str]:
        """
        Returns (returncode, stdout, stderr).
        If capture=False, output goes directly to the terminal.
        """
        sh = self._shell
        words = sh.expander.expand_words(cmd.words)
        words = AliasStore.expand(words)

        if not words:
            return (0, "", "")

        name = words[0]
        args = words[1:]

        # --- apply input redirection ---
        for r in cmd.redirects:
            if r.type == TT.REDIR_IN:
                try:
                    stdin = sh.kernel.syscall.open(r.target)
                except SyscallError as e:
                    return (1, "", str(e))

        # --- builtin ---
        if sh.builtins.has(name):
            out_buf = []
            if capture or any(r.type in (TT.REDIR_OUT, TT.REDIR_APP) for r in cmd.redirects):
                # capture stdout for redirect/pipe
                import io as _io
                old = sys.stdout
                sys.stdout = _io.StringIO()
                rc = sh.builtins.run(name, args)
                captured = sys.stdout.getvalue()
                sys.stdout = old
                self._apply_output_redirects(cmd.redirects, captured)
                return (rc, captured if capture else "", "")
            else:
                rc = sh.builtins.run(name, args)
                return (rc, "", "")

        # --- external command ---
        rc, out, err = sh.kernel.syscall.exec(
            words,
            stdin=stdin,
            capture=capture or bool(cmd.redirects),
            timeout=None,
        )

        if not capture:
            self._apply_output_redirects(cmd.redirects, out)
            if out and not cmd.redirects:
                IOManager.write(out, end="")
            if err:
                IOManager.error(err.rstrip())
            return (rc, "", "")

        return (rc, out, err)

    def _apply_output_redirects(self, redirects: list[Redirect], content: str) -> None:
        for r in redirects:
            if r.type == TT.REDIR_OUT:
                self._shell.kernel.syscall.write(r.target, content)
            elif r.type == TT.REDIR_APP:
                self._shell.kernel.syscall.write(r.target, content, append=True)


# ===========================================================================
# Shell — the REPL
# ===========================================================================

class Shell:
    """
    The linux++ REPL.

    Loop:
      1. render prompt
      2. read input line
      3. lex -> parse -> expand
      4. dispatch (builtin or kernel exec)
      5. update $?  and loop
    """

    VERSION = "0.0.0b3"

    def __init__(self, kernel):
        self.kernel     = kernel
        self.running    = True
        self._exit_code = 0
        self._last_rc   = 0

        self.expander   = Expander(last_rc=0)
        self.lexer      = Lexer()
        self.history    = History()
        self.builtins   = BuiltinRegistry(self)
        self.dispatcher = Dispatcher(self)

    # --- Windows ANSI enabler ---

    @staticmethod
    def _enable_windows_ansi() -> bool:
        """
        Enable ANSI/VT100 escape processing on Windows via SetConsoleMode.
        Requires Windows 10 build 1511+. Works in cmd.exe, PowerShell,
        and Windows Terminal.
        Returns True if successfully enabled, False if not supported.
        """
        if not IS_WINDOWS:
            return True
        try:
            import ctypes, ctypes.wintypes
            kernel32   = ctypes.windll.kernel32
            STD_OUTPUT = -11
            ENABLE_VT  = 0x0004
            handle = kernel32.GetStdHandle(STD_OUTPUT)
            if handle == -1:
                return False
            mode = ctypes.wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT):
                return False
            # Set stdout to UTF-8 so box-drawing chars render correctly
            import io
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8",
                errors="replace", line_buffering=True,
            )
            return True
        except Exception:
            return False

    # --- prompt ---

    def _prompt(self) -> str:
        user = EnvManager.username()
        host = EnvManager.hostname()
        cwd  = self.kernel.syscall.getcwd()
        home = os.path.expanduser("~")

        # shorten home to ~
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]

        # Windows: ANSI codes work directly after _enable_windows_ansi().
        #   No \001..\002 wrappers — pyreadline3 handles width on its own.
        # Unix: \001..\002 wrappers are mandatory so GNU readline counts
        #   only visible characters when calculating cursor position.
        if IS_WINDOWS:
            R  = "\033[0m"
            G  = "\033[32m"
            B  = "\033[34m"
            Y  = "\033[33m"
            RD = "\033[31m"
            try:
                import ctypes
                is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                is_admin = False
            marker = f"{RD}#{R}" if is_admin else f"{G}${R}"
        else:
            def _c(code: str) -> str:
                return f"\001{code}\002"
            R  = _c("\033[0m")
            G  = _c("\033[32m")
            B  = _c("\033[34m")
            Y  = _c("\033[33m")
            RD = _c("\033[31m")
            try:
                is_root = os.geteuid() == 0
            except AttributeError:
                is_root = False
            marker = f"{RD}#{R}" if is_root else f"{G}${R}"

        return f"{G}{user}{R}@{B}{host}{R}:{Y}{cwd}{R}{marker} "

    # --- line execution (called by REPL and `source`) ---

    def execute_line(self, line: str) -> int:
        line = line.strip()
        if not line or line.startswith("#"):
            return 0

        # Syntax highlighting preview (if enabled)
        if EnvManager.get("LPP_SYNTAX") == "1":
            h = SyntaxHighlighter.highlight(line, set(self.builtins._builtins.keys()))
            IOManager.write(f"{Color.dim('  >>')} {h}")

        try:
            tokens = self.lexer.tokenise(line)
        except Exception as e:
            IOManager.error(f"syntax error: {e}")
            return 1

        try:
            ast = Parser(tokens).parse()
        except Exception as e:
            IOManager.error(f"parse error: {e}")
            return 1

        return self._exec_command_list(ast)

    def _exec_command_list(self, cl: "CommandList") -> int:
        rc = 0
        for op, pipeline in cl.items:
            if op == "&&" and rc != 0:
                continue
            if op == "||" and rc == 0:
                continue
            rc = self._exec_pipeline(pipeline)
            self.expander.last_rc = rc
            EnvManager.set("?", str(rc))
        return rc

    def _exec_pipeline(self, pipeline: "Pipeline") -> int:
        cmds = pipeline.commands

        # background single command
        if pipeline.background and len(cmds) == 1:
            words = self.expander.expand_words(cmds[0].words)
            words = AliasStore.expand(words)
            if words:
                self.kernel.syscall.fork(words)
            return 0

        # single command (most common case)
        if len(cmds) == 1:
            rc, _, _ = self.dispatcher.dispatch(cmds[0])
            return rc

        # multi-stage pipeline  cmd1 | cmd2 | cmd3
        # expand each stage first
        expanded_stages = []
        for cmd in cmds:
            words = self.expander.expand_words(cmd.words)
            words = AliasStore.expand(words)
            if words:
                expanded_stages.append(words)

        if not expanded_stages:
            return 0

        # if all stages are external, use kernel pipeline for efficiency
        all_external = all(
            not self.builtins.has(s[0]) for s in expanded_stages
        )
        if all_external:
            rc, out, err = self.kernel.syscall.pipe_exec(expanded_stages)
            if out:
                IOManager.write(out, end="")
            if err:
                IOManager.error(err.rstrip())
            return rc

        # mixed pipeline (builtins + external) — chain via strings
        buf = None
        rc  = 0
        for i, (cmd, words) in enumerate(zip(cmds, expanded_stages)):
            is_last = (i == len(cmds) - 1)
            capture = not is_last
            if self.builtins.has(words[0]):
                rc, buf, _ = self.dispatcher.dispatch(
                    cmd, stdin=buf, capture=capture
                )
            else:
                rc, buf_out, buf_err = self.kernel.syscall.exec(
                    words, stdin=buf, capture=capture
                )
                buf = buf_out
                if buf_err and is_last:
                    IOManager.error(buf_err.rstrip())
        if buf:
            IOManager.write(buf, end="")
        return rc

    # --- readline autocomplete ---

    def _completer(self, text: str, state: int):
        if state == 0:
            line  = readline.get_line_buffer()
            parts = line.split()
            # complete commands if at first word
            if not parts or (len(parts) == 1 and not line.endswith(" ")):
                self._completions = self._complete_command(text)
            else:
                self._completions = self._complete_path(text)
        try:
            return self._completions[state]
        except IndexError:
            return None

    def _complete_command(self, prefix: str) -> list[str]:
        matches = [b for b in self.builtins._builtins if b.startswith(prefix)]
        # search PATH
        for d in EnvManager.path_dirs():
            try:
                for name in os.listdir(d):
                    if name.startswith(prefix):
                        full = os.path.join(d, name)
                        if os.access(full, os.X_OK):
                            matches.append(name)
            except OSError:
                continue
        return sorted(set(matches))

    def _complete_path(self, prefix: str) -> list[str]:
        prefix = os.path.expanduser(prefix)
        if os.path.isdir(prefix):
            directory, partial = prefix, ""
        else:
            directory = os.path.dirname(prefix) or "."
            partial   = os.path.basename(prefix)
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        matches = []
        for name in names:
            if name.startswith(partial):
                full = os.path.join(directory, name)
                matches.append(full + "/" if os.path.isdir(full) else full)
        return sorted(matches)

    # --- banner ---

    def _banner(self) -> None:
        IOManager.write(
            f"\033[1;32mlinux++\033[0m v{self.VERSION}  "
            f"(type \033[1mhelp\033[0m for built-in commands, "
            f"\033[1mexit\033[0m to quit)"
        )

    # --- main REPL loop ---

    def run(self) -> int:
        # Enable ANSI color processing on Windows before the first print
        ansi_ok = self._enable_windows_ansi()

        self._banner()
        self.history.load()

        readline.set_completer(self._completer)
        readline.parse_and_bind(
            "tab: complete" if not IS_WINDOWS else "tab: complete"
        )

        while self.running:
            try:
                line = input(self._prompt())
            except EOFError:
                IOManager.write("")
                break
            except KeyboardInterrupt:
                IOManager.write("")
                continue

            if not line.strip():
                continue

            self.history.add(line)
            self._last_rc = self.execute_line(line)

        self.kernel.shutdown()
        return self._exit_code


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    from .kernel import Kernel
    k = Kernel()
    k.boot()
    shell = Shell(k)
    sys.exit(shell.run())


if __name__ == "__main__":
    main()