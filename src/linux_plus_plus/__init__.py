"""# linux_plus_plus — A simple terminal operating system.

A small, educational "terminal operating system" package exposing a
minimal set of modules for building and running terminal-style
applications.

## Features
- Lightweight kernel/HAL/shell for experimenting with OS concepts.
- Convenience `app` and `cli` entry points.
- Small standard-library helpers under the `stdlib` module.
- Example applications under the `apps` subpackage.

## Quick start
	from linux_plus_plus import app
	app.run()

## Metadata
- **author**: R A Veeraragavan
- **license**: MIT (_see project LICENSE_)

## Versioning
The module exposes a module-level `__version__` constant. If the
distribution metadata is available at runtime (installed package), we
attempt to read the version from metadata and fall back to the
default below.
"""
from . import app, cli, hal, kernel, shell, stdlib, apps

__all__ = [
	'app',
	'cli',
	'hal',
	'kernel',
	'shell',
	'stdlib',
	'apps',
]

__author__ = 'R A Veeraragavan'
__license__ = 'MIT'

# Default version; keep in sync with project release version when
# updating the package source.
__version__ = '0.0.0b3'

# If the package is installed as a distribution, prefer the distribution
# metadata version. Use importlib.metadata when available, or fallback
# to importlib_metadata for older environments.
try:
	from importlib.metadata import version as _pkg_version, PackageNotFoundError
except Exception:
	try:
		from importlib.metadata import version as _pkg_version, PackageNotFoundError
	except Exception:
		_pkg_version = None
		PackageNotFoundError = Exception

if _pkg_version is not None:
	try:
		# distribution name may differ from the import name; adjust if
		# your distribution is named differently (for example,
		# 'linux-plus-plus').
		__version__ = _pkg_version('linux-plus-plus')
	except PackageNotFoundError:
		# Leave the default version in place when metadata is absent.
		pass

del _pkg_version, PackageNotFoundError
