"""The target: a Hardcharger server, loaded through ADBC bulk ingest."""

from .hardcharger import HardchargerTarget, make_target

__all__ = ["HardchargerTarget", "make_target"]
