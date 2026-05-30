from src.linux_plus_plus.app import register_all
from src.linux_plus_plus.kernel import Kernel
from src.linux_plus_plus.shell import Shell
from sys import exit


def main():
    k = Kernel()
    k.boot()

    shell = Shell(k)
    register_all(shell)

    exit(shell.run())


if __name__ == "__main__":
    main()
