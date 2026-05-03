"""The command line application to use linux++."""

from .app import register_all
from .kernel import Kernel
from .shell import Shell
import sys


def app():
    k = Kernel()
    k.boot()

    shell = Shell(k)
    register_all(shell)

    sys.exit(shell.run())


if __name__ == "__main__":
    app()
