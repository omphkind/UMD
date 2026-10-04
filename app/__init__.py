"""UMD application services, independent of the user interface."""

try:
    from app._version import version as __version__
except ImportError:
    __version__ = "0.1.0.dev0"
