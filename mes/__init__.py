"""Automotive Manufacturing Execution System (MES) Traceability Package.

Provides discrete manufacturing traceability, PLC-MES handshake protocols,
route interlocks, poka-yoke part verification, and quality analytics.
"""

from mes.plc import REASON_CODES, TagServer, handshake, tag
from mes.traceability import ROOT, TraceabilityMES

__all__ = [
    "ROOT",
    "TraceabilityMES",
    "TagServer",
    "handshake",
    "tag",
    "REASON_CODES",
]
