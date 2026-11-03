"""Bounded, stat-validated read views for the browser, never workflow authority."""
from collections import OrderedDict
import threading

import yaml


class ReadCache:
    def __init__(self, max_entries=4096, max_bytes=32 * 1024 * 1024):
        self.entries = OrderedDict()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.bytes = 0
        self.lock = threading.RLock()

    @staticmethod
    def stamp(path):
        try:
            s = path.stat()
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        except FileNotFoundError:
            return None

    def read(self, key, paths, loader):
        with self.lock:
            stamps = tuple(self.stamp(path) for path in paths)
            old = self.entries.pop(key, None)
            if old:
                self.bytes -= old[2]
                if old[0] == stamps:
                    self.entries[key] = old
                    self.bytes += old[2]
                    return old[1]
            value = loader()
            # A replacement during the read must not be cached under its old identity.
            if stamps == tuple(self.stamp(path) for path in paths):
                weight = 4 * sum(stamp[2] for stamp in stamps if stamp) + 256
                if weight <= self.max_bytes:
                    while self.entries and (len(self.entries) >= self.max_entries or self.bytes + weight > self.max_bytes):
                        _, removed = self.entries.popitem(last=False)
                        self.bytes -= removed[2]
                    self.entries[key] = (stamps, value, weight)
                    self.bytes += weight
            return value


def task_row(path):
    row = {'id': path.stem, 'queue': path.parent.name}
    try:
        document = yaml.load(path.read_bytes(), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader)) or {}
        row['title'] = document.get('title', '')
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        row['error'] = 'unreadable_task'
    return row
