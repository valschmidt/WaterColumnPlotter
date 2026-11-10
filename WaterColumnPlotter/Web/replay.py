"""
Replay of a recorded .kmall / .kmwcd file through the worker, in place of live data.

The file is indexed with pykmall, and its #MWC, #SVP, #IIP and #MRZ datagrams are handed to the worker in time order,
paced by their timestamps times a speed factor (or as fast as they can be processed). The worker reads the file itself,
so playback waits for processing rather than dropping datagrams. Files hold whole datagrams (only UDP output is
partitioned), so no reassembly is needed.

SIS records water column in a .kmwcd file of its own, without the #MRZ soundings that wreck coordinates need
(georef.py); those are in the .kmall of the same name, which is found (see find_companion) and its #MRZ played too.
"""

import glob
import os

import numpy as np
from KMALL.kmall import kmall

# Message types as pykmall's index names them.
MWC, SVP, IIP, MRZ = "b'#MWC'", "b'#SVP'", "b'#IIP'", "b'#MRZ'"
EXTENSIONS = ('.kmall', '.kmwcd', '.s7k')  # .s7k: replay_s7k.py
MAX_GAP_S = 5.0  # pauses in recording longer than this are skipped
MAX_LAG_S = 1.0  # when processing falls further behind than this, playback slows to keep up instead of rushing


def find_companion(path):
    """
    The .kmall recorded with a .kmwcd: the same name in the same directory, the one above it, or another directory
    beside it (SIS setups often keep water column in a directory of its own). None if there isn't one.
    """
    stem = os.path.splitext(os.path.basename(path))[0] + '.kmall'
    here = os.path.dirname(os.path.abspath(path))
    parent = os.path.dirname(here)
    for candidate in [os.path.join(here, stem), os.path.join(parent, stem),
                      *sorted(glob.glob(os.path.join(glob.escape(parent), '*', glob.escape(stem))))]:
        if os.path.isfile(candidate):
            return candidate
    return None


class Replay:
    def __init__(self, path):
        self.path = path
        self.km = kmall(path)
        self.km.index_file()
        self.files = [self.km]
        index = self.km.Index[self.km.Index.MessageType.astype(str).isin((MWC, SVP, IIP, MRZ))]
        times = [index.index.to_numpy(dtype=float)]
        offsets, sizes = [index.ByteOffset.to_numpy()], [index.MessageSize.to_numpy()]
        types = [index.MessageType.astype(str).to_numpy()]
        # Where the #MRZ soundings come from: this file, its companion .kmall, or nowhere.
        self.mrz_source = os.path.basename(path) if (types[0] == MRZ).any() else None
        if self.mrz_source is None and path.lower().endswith('.kmwcd'):
            companion = find_companion(path)
            if companion is not None:
                try:
                    km = kmall(companion)
                    km.index_file()
                    mrz = km.Index[km.Index.MessageType.astype(str) == MRZ]
                    if len(mrz):
                        self.files.append(km)
                        times.append(mrz.index.to_numpy(dtype=float))
                        offsets.append(mrz.ByteOffset.to_numpy())
                        sizes.append(mrz.MessageSize.to_numpy())
                        types.append(mrz.MessageType.astype(str).to_numpy())
                        self.mrz_source = os.path.relpath(companion, os.path.dirname(os.path.dirname(path)))
                    else:
                        km.closeFile()
                except Exception:
                    pass  # no coordinates, but the water column still plays
        # In time order; the water column file first where times are equal.
        file_ids = np.concatenate([np.full(len(t), i) for i, t in enumerate(times)])
        all_times = np.concatenate(times)
        order = np.argsort(all_times, kind='stable')
        self.times, self.file_ids = all_times[order], file_ids[order]
        self.offsets, self.sizes = np.concatenate(offsets)[order], np.concatenate(sizes)[order]
        self.types = np.concatenate(types)[order]
        mwc = self.times[self.types == MWC]
        if not len(mwc):
            self.close()
            raise ValueError(f"{os.path.basename(path)} has no water column (#MWC) datagrams")
        self.start, self.end = float(mwc.min()), float(mwc.max())
        self.pos = 0  # next datagram to play
        self.playing = True
        self.speed = 1.0  # data seconds per second; 0: as fast as possible
        self.anchor = None  # (wall clock, data time) that pacing is measured from

    def close(self):
        for km in self.files:
            km.closeFile()

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
        fid = self.files[self.file_ids[self.pos]].FID
        fid.seek(self.offsets[self.pos])
        data = fid.read(self.sizes[self.pos])
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
                fid = self.files[self.file_ids[earlier[-1]]].FID
                fid.seek(self.offsets[earlier[-1]])
                before.append(fid.read(self.sizes[earlier[-1]]))
        return before

    def status(self):
        return {'name': os.path.basename(self.path), 'start': self.start, 'end': self.end,
                'time': float(self.times[self.pos - 1]) if self.pos else self.start,
                'playing': self.playing, 'speed': self.speed, 'finished': self.finished, 'mrz': self.mrz_source}


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
