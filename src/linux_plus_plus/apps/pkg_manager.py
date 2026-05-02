import os
import json
import tempfile
import urllib.request
import zipfile
from typing import Optional

try:
    from ..hal    import IS_WINDOWS
    from ..stdlib import IOManager, EnvManager, SignalHandler, AliasStore
    from ..kernel import Kernel, SyscallError, INodeType
    from ..shell import Shell
except ImportError:
    IS_WINDOWS = os.name == "nt"
    Kernel = None


# ===========================================================================
# Package Manager
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
