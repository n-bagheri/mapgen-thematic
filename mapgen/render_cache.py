"""Opt-in, bounded reuse of exact rasters during interactive Step 7 edits.

Normal pipeline calls have no active context and retain their original path.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock

import numpy as np


class PatternRasterCache:
    def __init__(self, max_bytes=64 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def get(self, key):
        with self._lock:
            image = self._items.get(key)
            if image is None:
                return None
            self._items.move_to_end(key)
            return image.copy()

    def put(self, key, image):
        if image.nbytes > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old.nbytes
            self._items[key] = image.copy()
            self._bytes += image.nbytes
            while self._bytes > self.max_bytes:
                _, removed = self._items.popitem(last=False)
                self._bytes -= removed.nbytes


@dataclass
class RenderReuse:
    patterns: PatternRasterCache
    bases: dict = field(default_factory=dict)
    pattern_hits: int = 0
    pattern_misses: int = 0
    base_hits: int = 0


active_render_reuse = ContextVar("step7_render_reuse", default=None)


@contextmanager
def reuse_step7_rasters(cache):
    context = RenderReuse(cache)
    token = active_render_reuse.set(context)
    try:
        yield context
    finally:
        active_render_reuse.reset(token)
