"""MoM Generator - tray tool that records a meeting and drafts Minutes of Meeting with Claude."""

APP_NAME = "MoM Generator"
__version__ = "1.0.2"  # also the installer and website version: change it here, build.ps1 reads it

import importlib.util
import sys
import types

if importlib.util.find_spec("av") is None:
    # faster-whisper imports PyAV only to decode audio files. This app always passes it raw samples, so the
    # installer leaves PyAV out (~70 MB) and this empty module satisfies the import.
    sys.modules["av"] = types.ModuleType("av")
