"""Defaults and bounds of Recordings, heap censuses and History Windows.

A leaf without imports: accessors show and check these option values (the CLI
parser) without loading the measurement stack.
"""

DEFAULT_RECORDING_SECONDS = 300
MAX_RECORDING_SECONDS = 3600
MAX_LABEL_LENGTH = 200
RETAINED_RECORDINGS = 20
DEFAULT_HEAP_TOP = 20
MAX_HEAP_TOP = 100
HISTORY_INTERVAL_SECONDS = 600
HISTORY_RETENTION_DAYS = 14
DEFAULT_HISTORY_WINDOWS = 144
MAX_HISTORY_WINDOWS = 2016
MAX_HISTORY_NAMES = 50
