"""
Replay of a recorded Teledyne RESON .s7k file through the worker, in place of live data.

The file is indexed with pys7k, and the records the worker uses are handed to it as bytes in file order, paced as in
replay.py (whose Replay this extends). Each ping's records come in the order 7000 (sonar settings), 7004 (beam
geometry), 7027 (bottom detections), 7018 (beamformed water column); the worker grids the ping when its 7018
arrives. 1012 (roll, pitch, heave) supplies heave.
"""

import os

import numpy as np
from PYS7K import s7k

from WaterColumnPlotter.Web.replay import Replay

RECORD_TYPES = (1012, 7000, 7004, 7027, 7018)
WATER_COLUMN = 7018
PING_START = 7000  # the first of each ping's records


class S7kReplay(Replay):
    def __init__(self, path):
        self.path = path
        self.s7k = s7k(path)
        self.s7k.index_file()
        index = self.s7k.Index[self.s7k.Index.RecordType.isin(RECORD_TYPES)]
        self.labels = index.index.to_numpy()
        self.times = index.Time.to_numpy(dtype=float)
        self.types = index.RecordType.to_numpy()
        wc = self.times[self.types == WATER_COLUMN]
        if not len(wc):
            self.close()
            raise ValueError(f"{os.path.basename(path)} has no water column (7018 beamformed) records")
        self.start, self.end = float(wc.min()), float(wc.max())
        self.pos = 0  # next record to play
        self.playing = True
        self.speed = 1.0  # data seconds per second; 0: as fast as possible
        self.anchor = None  # (wall clock, data time) that pacing is measured from

    def close(self):
        self.s7k.closeFile()

    def next(self):
        """The next record's bytes."""
        data = self.s7k.read_record_bytes(self.labels[self.pos])
        self.pos += 1
        return data

    def seek(self, t):
        """
        Move to the start of the ping at or before data time t, so the ping's settings, geometry and detections are
        played before its water column. Returns no records: each ping carries what the worker needs.
        """
        self.pos = int(np.searchsorted(np.maximum.accumulate(self.times), t))
        starts = np.flatnonzero(self.types[:self.pos + 1] == PING_START)
        if len(starts) and not self.finished:
            self.pos = int(starts[-1])
        self.anchor = None
        return []
