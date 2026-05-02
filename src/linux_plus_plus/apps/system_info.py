""""""

import os
import platform
import sys
import time
import re

try:
    from ..hal    import IS_WINDOWS, HAL
    from ..stdlib import IOManager, EnvManager
    
except ImportError:
    IS_WINDOWS = os.name == "nt"
    HAL = None


# ===========================================================================
# SysInfo  — neofetch-style system information
# ===========================================================================

class SysInfo:

    @staticmethod
    def display(args: list[str] = None) -> int:
        advanced = False
        if args and ("-a" in args or "--advanced" in args):
            advanced = True
        lines = SysInfo._collect(advanced)
        logo  = SysInfo._logo()

        ansi_re = re.compile(r'\033\[[0-9;]*[mGKHF]')

        def get_visual_len(text: str) -> int:
            return len(ansi_re.sub('', text))

        # Calculate the actual visual width of the logo to ensure the info starts at the same column.
        # Standard len() counts non-printing ANSI codes, which breaks f-string alignment.
        max_logo_w = max((get_visual_len(l) for l in logo), default=0)
        gap = 4

        out = []
        n = max(len(logo), len(lines))
        for i in range(n):
            l_str = logo[i] if i < len(logo) else ""
            r_str = lines[i] if i < len(lines) else ""
            
            # Pad the logo string manually based on its visible character count
            v_len = get_visual_len(l_str)
            padding = " " * (max_logo_w + gap - v_len)
            out.append(f"{l_str}{padding}{r_str}")

        IOManager.write("\n".join(out) + "\n")
        return 0

    @staticmethod
    def _logo() -> list[str]:
        G = "\033[32;1m"; R = "\033[0m"
        return [
            fr"{G}  _ _                    {R}",
            fr"{G} | (_)               ++  {R}",
            fr"{G} | |_ _ __  _   ___  __  {R}",
            fr"{G} | | | '_ \| | | \ \/ / {R}",
            fr"{G} | | | | | | |_| |>  <   {R}",
            fr"{G} |_|_|_| |_|\__,_/_/\_\  {R}",
            fr"{G}                         {R}",
        ]

    @staticmethod
    def _collect(advanced: bool = False) -> list[str]:
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

        # count installed packages
        pkg_count = 0
        pkg_dir = os.path.join(os.path.expanduser("~"), ".linuxpp", "packages")
        if os.path.exists(pkg_dir):
            try:
                pkg_count = len([d for d in os.listdir(pkg_dir) if os.path.isdir(os.path.join(pkg_dir, d))])
            except Exception:
                pass

        sep = f"{C}{user}@{host}{R}"
        div = "─" * len(f"{user}@{host}")
        
        # Base info
        mem_percent = ""
        cpu_usage = ""
        if HAL:
            m = HAL.system.memory()
            if m.get("percent"):
                mem_percent = f" ({m['percent']}%)"
            # Note: real-time CPU % often requires psutil; fallback to 0.0 if unknown
            cpu_usage = f" ({HAL.system.info().get('cpu_usage', '0.0')}%)"

        info = [
            sep, div,
            f"{C}OS{R}          : {os_name} ({arch})",
            f"{C}Host{R}        : {host}",
            f"{C}Kernel{R}      : {platform.release()}",
            f"{C}Uptime{R}      : {uptime}",
            f"{C}Packages{R}    : {pkg_count} (lpp)",
            f"{C}Shell{R}       : {shell}",
            f"{C}Resolution{R}  : {HAL.terminal.get_size()[0]}x{HAL.terminal.get_size()[1]}" if HAL else f"{C}Shell{R}       : {shell}",
            f"{C}Terminal{R}    : {os.environ.get('TERM', 'unknown')}",
        ]

        if advanced and HAL:
            cols, rows = HAL.terminal.get_size()
            ip = HAL.network.local_ip()
            sys_info = HAL.system.info()
            mem_info = HAL.system.memory()
            proc_count = len(HAL.process.list_processes())
            
            up_secs = sys_info.get('uptime_secs', 0)
            boot_time = time.ctime(time.time() - up_secs) if up_secs else "Unknown"

            info.extend([
                f"{C}CPU{R}         : {sys_info.get('processor', 'Unknown')}{cpu_usage}",
                f"{C}Cores{R}       : {os.cpu_count()} threads",
                f"{C}Memory{R}      : {mem}{mem_percent}",
                f"{C}Disk Usage{R}  : {disk}",
                f"{C}Local IP{R}    : {ip}",
                f"{C}Processes{R}   : {proc_count}",
                f"{C}Python{R}      : {py_ver} ({platform.python_implementation()})",
                f"{C}Boot Time{R}   : {boot_time}",
                f"{C}Platform{R}    : {platform.platform()}",
            ])

        info.extend(["", SysInfo._color_blocks()])
        return info

    @staticmethod
    def _color_blocks() -> str:
        blocks = "".join(f"\033[4{i}m   " for i in range(8))
        return blocks + "\033[0m"
