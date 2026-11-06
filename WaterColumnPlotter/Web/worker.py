"""
Worker: parses complete #MWC datagrams with pykmall, grids them, and produces display data:
- the pie: average of the last alongTrackAvg_ping pings, cropped to its data;
- slice columns: vertical (per depth row) and horizontal (per across-track column) averages, averaged along track
  in blocks of alongTrackAvg_ping pings. (A ping is identified by pingCnt; dual-swath systems send two #MWC
  datagrams per ping.)
Results are put on a queue once per update interval.

Grids are blocks covering just the data (see gridding.py), so the pie and slice columns carry the index of their
first row / column.
"""

import collections
import functools
import logging
import queue
import time

import numpy as np
from KMALL.kmall import kmall

from WaterColumnPlotter.Web.gridding import (GridTooLarge, add_blocks, average, grid_mwc, horizontal_band,
                                             vertical_band)

logger = logging.getLogger(__name__)

# Settings whose change discards everything processed so far: the cell size and depth reference (which place data
# in the grid), and the along-track averaging (which sets how many pings each slice column spans).
RESET_SETTINGS = ('binSize_m', 'alongTrackAvg_ping', 'referenceToSurface', 'transducerDepth_m')


def band_sum(sums, counts, offset, band, axis):
    """
    Sum a 2D block over the part of band [i0, i1) that it covers, along axis (1: columns, 0: rows).
    Returns a 1D block ((offset,), sums, counts) along the other axis; empty if the band misses the block.
    """
    i0, i1 = band
    a, b = max(i0, offset[axis]) - offset[axis], min(i1, offset[axis] + sums.shape[axis]) - offset[axis]
    other = 1 - axis
    if b <= a:
        return (offset[other],), np.zeros(0), np.zeros(0, dtype=np.int64)
    idx = (slice(None), slice(a, b)) if axis == 1 else (slice(a, b), slice(None))
    return (offset[other],), sums[idx].sum(axis=axis), counts[idx].sum(axis=axis)


class Worker:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.km = kmall()
        self.stats = {'processed': 0, 'no_bottom': 0, 'grid_too_large': 0, 'process_ms': 0.0}
        self.reset()

    def reset(self):
        s = self.settings
        # Gridded blocks of the most recent pings, for the pie: [pingCnt, block]. Swaths of the same ping
        # (dual-swath systems send one #MWC per swath) are summed into one entry.
        self.recent = collections.deque(maxlen=s['alongTrackAvg_ping'])
        # Slice column being accumulated: {'pings': set of pingCnt, 'v': block, 'h': block, 'times': []}
        self.column = None
        self.new_columns = []  # Completed slice columns since the last update: (time, (v_off, v), (h_off, h))
        self.v_band = vertical_band(s)
        self.h_band = horizontal_band(s)

    def apply_settings(self, new):
        """Returns True if everything processed so far was discarded (so displays must clear their history)."""
        reset = any(new[k] != self.settings[k] for k in RESET_SETTINGS)
        self.settings = dict(new)
        if reset:
            self.reset()
        else:
            self.v_band = vertical_band(new)
            self.h_band = horizontal_band(new)
        return reset

    def finish_column(self):
        c = self.column
        (v_off,), v = average(c['v'])
        (h_off,), h = average(c['h'])
        self.new_columns.append((float(np.mean(c['times'])), (v_off, v), (h_off, h)))
        self.column = None

    def process_installation(self, data):
        """
        #IIP: returns the transmit transducer's depth below the waterline (m), or None if it isn't in the datagram.
        Installation z values are positive down from the vessel reference point.
        """
        self.km.OpenBytestoRead(data)
        install = self.km.read_EMdgmIIP(translate=True)['install_txt']
        # Separate transmit array (TRAI_TX1), or a combined transmit/receive head (TRAI_HD1).
        tx_z = install.get('tx_transducer_1_vertical_location', install.get('transducer_1_vertical_location'))
        waterline_z = install.get('waterline_vertical_location')
        if tx_z is None or waterline_z is None:
            return None
        return float(tx_z) - float(waterline_z)

    def process(self, data):
        t0 = time.perf_counter()
        self.km.OpenBytestoRead(data)
        dg = self.km.read_EMdgmMWC()
        s = self.settings
        depth_offset = (s['transducerDepth_m'] or 0.0) if s['referenceToSurface'] else 0.0
        try:
            result = grid_mwc(dg, s['binSize_m'], depth_offset)
        except GridTooLarge:
            self.stats['grid_too_large'] += 1
            return
        if result is None:
            self.stats['no_bottom'] += 1
            return
        sums, counts, row0, col0 = result
        block = ((row0, col0), sums, counts)
        ping = dg['cmnPart']['pingCnt']

        if self.recent and self.recent[-1][0] == ping:
            self.recent[-1][1] = add_blocks(self.recent[-1][1], block)
        else:
            self.recent.append([ping, block])

        # Each slice column averages alongTrackAvg_ping whole pings.
        if self.column is not None and ping not in self.column['pings'] \
                and len(self.column['pings']) >= s['alongTrackAvg_ping']:
            self.finish_column()
        v = band_sum(sums, counts, (row0, col0), self.v_band, axis=1)  # per row, over the across-track band
        h = band_sum(sums, counts, (row0, col0), self.h_band, axis=0)  # per column, over the depth band
        if self.column is None:
            self.column = {'pings': {ping}, 'v': v, 'h': h, 'times': [dg['header']['dgtime']]}
        else:
            c = self.column
            c['pings'].add(ping)
            c['v'] = add_blocks(c['v'], v)
            c['h'] = add_blocks(c['h'], h)
            c['times'].append(dg['header']['dgtime'])

        self.stats['processed'] += 1
        self.stats['process_ms'] = 0.9 * self.stats['process_ms'] + 0.1 * 1e3 * (time.perf_counter() - t0)

    def pie(self):
        """Average of the recent pings, cropped to cells with data: (data, row0, col0) or None."""
        if not self.recent:
            return None
        (row0, col0), sums, counts = functools.reduce(add_blocks, (r[1] for r in self.recent))
        rows = np.flatnonzero(counts.any(axis=1))
        cols = np.flatnonzero(counts.any(axis=0))
        if not len(rows):
            return None
        r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
        _, data = average(((0, 0), sums[r0:r1, c0:c1], counts[r0:r1, c0:c1]))
        return data, int(row0 + r0), int(col0 + c0)

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
            elif item[4:8] == b'#IIP':
                depth = worker.process_installation(item)
                if depth is not None:
                    out_queue.put({'type': 'installation', 'transducerDepth_m': round(depth, 3)})
            else:
                worker.process(item)
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Failed to process datagram")

        if time.monotonic() >= next_update:
            out_queue.put(worker.take_update(receiver_stats))
            next_update = time.monotonic() + update_interval
