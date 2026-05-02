import os
import sys

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager
except ImportError:
    IS_WINDOWS = os.name == "nt"


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
        