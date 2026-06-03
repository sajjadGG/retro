from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("retro-ai")
except PackageNotFoundError:
    __version__ = "0.1.0"
