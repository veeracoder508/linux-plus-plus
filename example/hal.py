from src.linux_plus_plus.hal import HAL, IS_WINDOWS


def main():
    HAL.boot()

    t = HAL.terminal
    cols, rows = t.get_size()
    t.write(f"  Terminal: {cols}×{rows}")

    disk = HAL.disk
    t.write(f"  Home dir: {disk.home()}")
    t.write(f"  CWD:      {disk.cwd()}")
    usage = disk.disk_usage()
    gb = 1024 ** 3
    t.write(f"  Disk:     {usage['used']/gb:.1f} GB used / {usage['total']/gb:.1f} GB total")

    proc = HAL.process
    result = proc.run(["python3" if not IS_WINDOWS else "python", "--version"])
    t.write(f"  Python:   {result.stdout.strip() or result.stderr.strip()}")

    net = HAL.network
    t.write(f"  IP:       {net.local_ip()}")

    mem = HAL.system.memory()
    if mem["total"]:
        t.write(f"  RAM:      {mem['used']//1024//1024} MB used / {mem['total']//1024//1024} MB total")

    t.write(f"\n{t.GREEN}HAL self-test passed.{t.RESET}")


if __name__ == "__main__":
    main()
