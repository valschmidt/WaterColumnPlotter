"""
Worker: parses complete #MWC datagrams with pykmall, grids them, and produces display data:
- the pie: average of the last alongTrackAvg_ping gridded datagrams, cropped to its data;
- slice columns: vertical (per depth row) and horizontal (per across-track column) averages, averaged along track
  in blocks of alongTrackAvg_ping datagrams.
Results are put on a queue once per update interval.
"""

import collections
import logging
import queue
import time

import numpy as np
from KMALL.kmall import kmall

from WaterColumnPlotter.Web.gridding import grid_mwc, horizontal_band, vertical_band

logger = logging.getLogger(__name__)

# Settings that change the grid itself; changing them discards everything gridded so far.
GRID_SETTINGS = ('binSize_m', 'maxHeave_m', 'maxGridCells')


class Worker:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.km = kmall()
        self.stats = {'processed': 0, 'no_bottom': 0, 'outside_grid': 0, 'process_ms': 0.0}
        self.reset()

    def reset(self):
        s = self.settings
        n = s['maxGridCells']
        self.recent = collections.deque(maxlen=s['alongTrackAvg_ping'])  # (sums, counts) of recent datagrams
        self.block = None  # Accumulating along-track block: [v_sum, v_cnt, h_sum, h_cnt, times]
        self.new_columns = []  # Completed slice columns since the last update: (time, vertical, horizontal)
        self.v_band = vertical_band(s)
        self.h_band = horizontal_band(s)
        self.n = n

    def apply_settings(self, new):
        """Returns True if the grid changed (so displays must clear their history)."""
        grid_changed = any(new[k] != self.settings[k] for k in GRID_SETTINGS)
        self.settings = dict(new)
        if grid_changed or self.recent.maxlen != new['alongTrackAvg_ping']:
            self.reset()
        else:
            self.v_band = vertical_band(new)
            self.h_band = horizontal_band(new)
        return grid_changed

    def process(self, data):
        t0 = time.perf_counter()
        self.km.OpenBytestoRead(data)
        dg = self.km.read_EMdgmMWC()
        s = self.settings
        result = grid_mwc(dg, s['binSize_m'], s['maxHeave_m'], self.n)
        if result is None:
            self.stats['no_bottom'] += 1
            return
        sums, counts, outside = result
        self.stats['outside_grid'] += outside
        self.recent.append((sums, counts))

        c0, c1 = self.v_band
        r0, r1 = self.h_band
        v_sum, v_cnt = sums[:, c0:c1].sum(axis=1), counts[:, c0:c1].sum(axis=1)
        h_sum, h_cnt = sums[r0:r1, :].sum(axis=0), counts[r0:r1, :].sum(axis=0)
        if self.block is None:
            self.block = [v_sum, v_cnt, h_sum, h_cnt, [dg['header']['dgtime']]]
        else:
            b = self.block
            b[0] = b[0] + v_sum
            b[1] = b[1] + v_cnt
            b[2] = b[2] + h_sum
            b[3] = b[3] + h_cnt
            b[4].append(dg['header']['dgtime'])
        if len(self.block[4]) >= s['alongTrackAvg_ping']:
            b = self.block
            with np.errstate(invalid='ignore', divide='ignore'):
                self.new_columns.append((float(np.mean(b[4])),
                                         (b[0] / b[1]).astype(np.float32),
                                         (b[2] / b[3]).astype(np.float32)))
            self.block = None

        self.stats['processed'] += 1
        self.stats['process_ms'] = 0.9 * self.stats['process_ms'] + 0.1 * 1e3 * (time.perf_counter() - t0)

    def pie(self):
        """Average of the recent datagrams, cropped to cells with data: (data, row0, col0) or None."""
        if not self.recent:
            return None
        sums = sum(r[0] for r in self.recent)
        counts = sum(r[1] for r in self.recent)
        rows = np.flatnonzero(counts.any(axis=1))
        cols = np.flatnonzero(counts.any(axis=0))
        if not len(rows):
            return None
        r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
        with np.errstate(invalid='ignore', divide='ignore'):
            data = (sums[r0:r1, c0:c1] / counts[r0:r1, c0:c1]).astype(np.float32)
        return data, int(r0), int(c0)

    def take_update(self, receiver_stats):
        columns, self.new_columns = self.new_columns, []
        return {'type': 'update', 'pie': self.pie(), 'columns': columns,
                'stats': {**receiver_stats, **self.stats}}


def run(settings, in_queue, settings_queue, out_queue, update_interval):
    """Process entry point."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    worker = Worker(settings)
    receiver_stats = {}
    next_update = time.monotonic() + update_interval
    while True:
        try:
            while True:
                new = settings_queue.get_nowait()
                if worker.apply_settings(new):
                    out_queue.put({'type': 'reset'})
        except queue.Empty:
            pass

        try:
            item = in_queue.get(timeout=max(0.0, next_update - time.monotonic()))
            if isinstance(item, tuple):
                receiver_stats = item[1]
            else:
                worker.process(item)
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Failed to process datagram")

        if time.monotonic() >= next_update:
            out_queue.put(worker.take_update(receiver_stats))
            next_update = time.monotonic() + update_interval
