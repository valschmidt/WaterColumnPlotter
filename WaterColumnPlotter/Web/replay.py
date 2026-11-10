"""
Replay of a recorded .kmall / .kmwcd file through the worker, in place of live data.

The file is indexed with pykmall, and its #MWC, #SVP and #IIP datagrams are handed to the worker in file order, paced
by their timestamps times a speed factor (or as fast as they can be processed). The worker reads the file itself, so
playback waits for processing rather than dropping datagrams. Files hold whole #MWC datagrams (only UDP output is
partitioned), so no reassembly is needed.
"""

import os

import numpy as np
from KMALL.kmall import kmall

# Message types as pykmall's index names them.
MWC, SVP, IIP = "b'#MWC'", "b'#SVP'", "b'#IIP'"
EXTENSIONS = ('.kmall', '.kmwcd', '.s7k')  # .s7k: replay_s7k.py
MAX_GAP_S = 5.0  # pauses in recording longer than this are skipped
MAX_LAG_S = 1.0  # when processing falls further behind than this, playback slows to keep up instead of rushing


class Replay:
    def __init__(self, path):
        self.path = path
        self.km = kmall(path)
        self.km.index_file()
        index = self.km.Index[self.km.Index.MessageType.astype(str).isin((MWC, SVP, IIP))]
        self.times = index.index.to_numpy(dtype=float)
        self.offsets = index.ByteOffset.to_numpy()
        self.sizes = index.MessageSize.to_numpy()
        self.types = index.MessageType.astype(str).to_numpy()
        mwc = self.times[self.types == MWC]
        if not len(mwc):
            self.km.closeFile()
            raise ValueError(f"{os.path.basename(path)} has no water column (#MWC) datagrams")
        self.start, self.end = float(mwc.min()), float(mwc.max())
        self.pos = 0  # next datagram to play
        self.playing = True
        self.speed = 1.0  # data seconds per second; 0: as fast as possible
        self.anchor = None  # (wall clock, data time) that pacing is measured from

    def close(self):
        self.km.closeFile()

    @property
    def finished(self):
        return self.pos >= len(self.times)

    def due(self, now):
        """Wall-clock time (time.monotonic) at which the next datagram should be played."""
        t = self.times[self.pos]
        if self.anchor is None or self.speed == 0 or \
                (self.pos and abs(t - self.times[self.pos - 1]) > MAX_GAP_S):
            self.anchor = (now, t)
        wall, t0 = self.anchor
        due = wall + (t - t0) / self.speed if self.speed else now
        if now - due > MAX_LAG_S:
            self.anchor = (now, t)
            due = now
        return due

    def next(self):
        """The next datagram's bytes."""
        self.km.FID.seek(self.offsets[self.pos])
        data = self.km.FID.read(self.sizes[self.pos])
        self.pos += 1
        return data

    def set_playing(self, playing):
        self.playing = playing and not self.finished
        self.anchor = None

    def set_speed(self, speed):
        self.speed = speed
        self.anchor = None

    def seek(self, t):
        """
        Move to the first datagram at or after data time t. Returns the latest #IIP and #SVP before it (bytes), which
        the worker needs to process the data from there.
        """
        self.pos = int(np.searchsorted(np.maximum.accumulate(self.times), t))
        self.anchor = None
        before = []
        for kind in (IIP, SVP):
            earlier = np.flatnonzero(self.types[:self.pos] == kind)
            if len(earlier):
                self.km.FID.seek(self.offsets[earlier[-1]])
                before.append(self.km.FID.read(self.sizes[earlier[-1]]))
        return before

    def status(self):
        return {'name': os.path.basename(self.path), 'start': self.start, 'end': self.end,
                'time': float(self.times[self.pos - 1]) if self.pos else self.start,
                'playing': self.playing, 'speed': self.speed, 'finished': self.finished}


def list_directory(directory):
    """
    A directory's subdirectories and recorded files (names, sorted), for browsing from the page. Hidden entries and
    ones that can't be read are left out.
    """
    dirs, files = [], []
    with os.scandir(directory) as entries:
        for e in entries:
            if e.name.startswith('.'):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file() and e.name.lower().endswith(EXTENSIONS):
                    files.append({'name': e.name, 'size': e.stat().st_size})
            except OSError:
                continue
    return sorted(dirs, key=str.lower), sorted(files, key=lambda f: f['name'])
