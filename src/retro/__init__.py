from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("retro-agent-memory")
except PackageNotFoundError:
    __version__ = "0.1.0"
