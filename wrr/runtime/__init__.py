"""Runtime detection primitives for the WRR v6 control plane."""

from .detect import ProcessSnapshot, RuntimeCapabilities, RuntimeInfo, detect_runtime

__all__ = [
    "ProcessSnapshot",
    "RuntimeCapabilities",
    "RuntimeInfo",
    "detect_runtime",
]
