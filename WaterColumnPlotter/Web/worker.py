"""
Worker: parses complete #MWC datagrams with pykmall, grids them, and produces display data:
- the pie: average of the last alongTrackAvg_swath swaths, cropped to its data;
- slice columns: vertical (per depth row) and horizontal (per across-track column) averages, averaged along track
  in blocks of alongTrackAvg_swath swaths.
Counts are of swaths, not ping cycles: a dual-swath system transmits two swaths per ping cycle, in different
directions, under one pingCnt. A swath is identified by (pingCnt, swathAlongPosition); its #MWC datagrams (one per
receive head on dual-head systems) are summed together.
Results are put on a queue once per update interval. A recorded file can be replayed in place of live data
(see replay.py).

Grids are blocks covering just the data (see gridding.py), so the pie and slice columns carry the index of their
first row / column.
"""

import collections
import functools
import logging
import os
import queue
import time

import numpy as np
from KMALL.kmall import kmall

from WaterColumnPlotter.Web.gridding import (GridTooLarge, PingBackground, add_blocks, average, grid_mwc,
                                             horizontal_band, vertical_band)
from WaterColumnPlotter.Web.layers import LayerTracker
from WaterColumnPlotter.Web.replay import Replay
from WaterColumnPlotter.Web.thermocline import ThermoclineTracker, depth_error, svp_thermocline

logger = logging.getLogger(__name__)

# Settings whose change discards everything processed so far: the cell size and depth reference (which place data
# in the grid), and the along-track averaging (which sets how many swaths each slice column spans).
# Thermocline tracking: a cast is compared with the picks that follow it if it arrived within this long of being
# taken, using the median of this many picks as the reference.
CAST_RECENT_S = 600.0
# Ping rate: measured over this many recent ping cycles; restarted after a gap longer than this.
PING_RATE_PINGS = 21
MAX_PING_GAP_S = 5.0
CALIBRATION_PICKS = 10

RESET_SETTINGS = ('binSize_m', 'alongTrackAvg_swath', 'referenceToSurface', 'transducerDepth_m', 'sidelobeSuppression',
                  'dualSwath')
# dualSwath: which swaths of a dual-swath system's ping cycle are used, by swathAlongPosition (Kongsberg numbers them
# along ship from 0); a single-swath system's one swath is always used.
DUAL_SWATH = {'both': None, 'first': 0, 'second': 1}


def band_weighted_depth(sums, counts, offset, band, bin_size):
    """
    Per grid column, over the rows of band [r0, r1): the sum of each cell's linear amplitude (from its mean dB) times
    its depth, and the sum of the amplitudes, as a 1D block ((col0,), sum, weight). Their ratio is the
    amplitude-weighted mean depth of the signal in the band.
    Only columns where the swath covers the whole band are included: those with data at or above the band's top row
    and at or below its bottom row. Elsewhere the fan's edge or the seafloor cuts the band, and only part of it would
    count, biasing the depth. Empty if the band isn't wholly inside the block.
    """
    (row0, col0), (r0, r1) = offset, band
    a, b = r0 - row0, r1 - row0
    empty = (col0,), np.zeros(0), np.zeros(0)
    if a < 0 or b > sums.shape[0] or b <= a:
        return empty
    covered = (counts[:a + 1] > 0).any(axis=0) & (counts[b - 1:] > 0).any(axis=0)
    cols = np.flatnonzero(covered)
    if not len(cols):
        return empty
    c0, c1 = cols[0], cols[-1] + 1
    n = np.where(covered[c0:c1], counts[a:b, c0:c1], 0)
    with np.errstate(invalid='ignore', divide='ignore'):
        amplitude = np.where(n > 0, 10.0 ** (sums[a:b, c0:c1] / np.maximum(n, 1) / 20.0), 0.0)
    depth = (row0 + np.arange(a, b) + 0.5)[:, None] * bin_size
    return (int(col0 + c0),), (amplitude * depth).sum(axis=0), amplitude.sum(axis=0)


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
        self.svp = None  # latest cast: {'depth', 'speed', 'top', 'bottom'} (depths below the surface)
        self.stats = {'processed': 0, 'no_bottom': 0, 'grid_too_large': 0, 'swath_skipped': 0, 'process_ms': 0.0,
                      'ping_rate_hz': None, 'swaths_per_ping': None}
        self.ping_times = collections.deque(maxlen=PING_RATE_PINGS)  # (pingCnt, time) of recent ping cycles
        self.reset()

    def note_ping(self, dg):
        """
        Track the sonar's ping rate from the data's timestamps (the recorded rate when replaying, whatever the speed).
        Each ping cycle (pingCnt) counts once, however many swaths and receive heads it has.
        """
        ping, t = dg['cmnPart']['pingCnt'], dg['header']['dgtime']
        last = self.ping_times[-1] if self.ping_times else None
        if last and last[0] == ping:
            return
        if last and not 0 < t - last[1] < MAX_PING_GAP_S:
            self.ping_times.clear()  # a gap, a seek or a new source
        self.ping_times.append((ping, t))
        span = self.ping_times[-1][1] - self.ping_times[0][1]
        self.stats['ping_rate_hz'] = (len(self.ping_times) - 1) / span if span > 0 else None
        self.stats['swaths_per_ping'] = dg['cmnPart']['swathsPerPing']

    def reset(self):
        s = self.settings
        # Gridded blocks of the most recent swaths, for the pie: [(pingCnt, swathAlongPosition), block]. Datagrams of
        # the same swath (one per receive head on dual-head systems) are summed into one entry.
        self.recent = collections.deque(maxlen=s['alongTrackAvg_swath'])
        # Slice column being accumulated: {'swaths': set of swath keys, 'v': block, 'h': block, 'times': []}
        self.column = None
        # Completed slice columns since the last update: (time, (v_off, v), (h_off, h), layers, thermocline, (hz_off, hz))
        self.new_columns = []
        self.tracker = LayerTracker(min_level_db=s['layerMinLevel_dB'])
        self.thermocline = ThermoclineTracker(threshold_db_per_m=s['thermoclineThreshold_dBpm'],
                                              along_track_s=s['thermoclineWindow_s'])
        self.v_band = vertical_band(s)
        self.h_band = horizontal_band(s)
        self.background = PingBackground(s['backgroundSwaths'])  # for 'ping_mean' sidelobe suppression

    def apply_settings(self, new):
        """Returns True if everything processed so far was discarded (so displays must clear their history)."""
        reset = any(new[k] != self.settings[k] for k in RESET_SETTINGS)
        self.settings = dict(new)
        if reset:
            self.reset()
        else:
            self.v_band = vertical_band(new)
            self.h_band = horizontal_band(new)
            self.tracker.min_level_db = new['layerMinLevel_dB']
            self.thermocline.threshold = new['thermoclineThreshold_dBpm']
            self.thermocline.along_track_s = new['thermoclineWindow_s']
            self.background.n = new['backgroundSwaths']
        return reset

    def finish_column(self):
        c = self.column
        (v_off,), v = average(c['v'])
        (h_off,), h = average(c['h'])
        (hz_off,), hz = average(c['hz'])  # amplitude-weighted mean depth in the horizontal slice's band
        t = float(np.mean(c['times']))
        b = self.settings['binSize_m']
        depths = (v_off + np.arange(len(v))) * b
        found = [z for z in c['seafloor'] if z is not None]
        seafloor = min(found) if found else None
        layers = self.tracker.add_profile(t, depths, v, seafloor)
        self.new_columns.append((t, (v_off, v), (h_off, h), layers,
                                 self.track_thermocline(t, v_off, depths, v, seafloor), (hz_off, hz)))
        self.column = None

    def set_svp(self, time, depth, speed):
        """A new cast: returns its thermocline (top, bottom) below the surface, or None if none was found."""
        found = svp_thermocline(depth, speed)
        self.svp = None if found is None else {
            'time': time, 'depth': depth, 'speed': speed, 'top': found[0], 'bottom': found[1],
            'reference': None,  # depth below the surface the picks are compared with, once known
            'calibration': []}  # picks since the cast arrived, while setting the reference
        return found

    def track_thermocline(self, t, v_off, depths, values, seafloor):
        """
        Pick the thermocline bottom in a vertical slice column and compare it with the last cast. Returns
        [pick, reference, outer-beam depth error] (depths in the column's reference; reference and error None while
        unknown), or None if there was no pick.

        If the cast arrived as it was taken (cast time within CAST_RECENT_S of the data), the reference is the median
        of the first CALIBRATION_PICKS picks after it, so the layer's movement since the cast is measured whatever its
        offset from the thermocline. Otherwise the cast's own thermocline bottom is the reference (Cordero's
        comparison), which assumes the layer marks the thermocline bottom.
        """
        s = self.settings
        z0 = s['transducerDepth_m'] or 0.0
        to_surface = 0.0 if s['referenceToSurface'] else z0  # add to column depths for depths below the surface
        cast = self.svp
        window = None
        if cast is not None:
            centre = cast['bottom'] - to_surface
            window = (centre - s['thermoclineSearch_m'], centre + s['thermoclineSearch_m'])
        pick = self.thermocline.add_profile(t, v_off, values, s['binSize_m'], window, seafloor)
        if pick is None:
            return None
        if cast is None:
            return [round(pick, 2), None, None]
        if cast['reference'] is None:
            if abs(t - cast['time']) > CAST_RECENT_S:
                cast['reference'] = cast['bottom']
            else:
                cast['calibration'].append(pick + to_surface)
                if len(cast['calibration']) < CALIBRATION_PICKS:
                    return [round(pick, 2), None, None]
                cast['reference'] = float(np.median(cast['calibration']))
        reference = cast['reference'] - to_surface
        err = None
        if seafloor is not None:
            err = depth_error(cast['depth'], cast['speed'], pick - reference, cast['top'], cast['bottom'],
                              z0, seafloor + to_surface, s['castAlertAngle_deg'])
        return [round(pick, 2), round(reference, 2), None if err is None else round(err, 3)]

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
        self.note_ping(dg)
        s = self.settings
        position = DUAL_SWATH[s['dualSwath']]
        cmn = dg['cmnPart']
        if position is not None and cmn['swathsPerPing'] > 1 and cmn['swathAlongPosition'] != position:
            self.stats['swath_skipped'] += 1
            return
        depth_offset = (s['transducerDepth_m'] or 0.0) if s['referenceToSurface'] else 0.0
        try:
            result = grid_mwc(dg, s['binSize_m'], depth_offset, s['sidelobeSuppression'], self.background)
        except GridTooLarge:
            self.stats['grid_too_large'] += 1
            return
        if result is None:
            self.stats['no_bottom'] += 1
            return
        sums, counts, row0, col0, bottom = result
        # The seafloor under the vertical slice: the shallowest bottom detection in its across-track band.
        b = s['binSize_m']
        in_band = (bottom[0] >= self.v_band[0] * b) & (bottom[0] < self.v_band[1] * b)
        seafloor = float(bottom[1][in_band].min()) if in_band.any() else None
        block = ((row0, col0), sums, counts)
        swath = (dg['cmnPart']['pingCnt'], dg['cmnPart']['swathAlongPosition'])

        if self.recent and self.recent[-1][0] == swath:
            self.recent[-1][1] = add_blocks(self.recent[-1][1], block)
        else:
            self.recent.append([swath, block])

        # Each slice column averages alongTrackAvg_swath swaths (an odd number splits a dual-swath ping cycle).
        if self.column is not None and swath not in self.column['swaths'] \
                and len(self.column['swaths']) >= s['alongTrackAvg_swath']:
            self.finish_column()
        v = band_sum(sums, counts, (row0, col0), self.v_band, axis=1)  # per row, over the across-track band
        h = band_sum(sums, counts, (row0, col0), self.h_band, axis=0)  # per column, over the depth band
        hz = band_weighted_depth(sums, counts, (row0, col0), self.h_band, b)
        if self.column is None:
            self.column = {'swaths': {swath}, 'v': v, 'h': h, 'hz': hz, 'times': [dg['header']['dgtime']],
                           'seafloor': [seafloor]}
        else:
            c = self.column
            c['swaths'].add(swath)
            c['v'] = add_blocks(c['v'], v)
            c['h'] = add_blocks(c['h'], h)
            c['hz'] = add_blocks(c['hz'], hz)
            c['times'].append(dg['header']['dgtime'])
            c['seafloor'].append(seafloor)

        self.stats['processed'] += 1
        self.stats['process_ms'] = 0.9 * self.stats['process_ms'] + 0.1 * 1e3 * (time.perf_counter() - t0)

    def pie(self):
        """Average of the recent swaths, cropped to cells with data: (data, row0, col0) or None."""
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


def handle_datagram(worker, item, out_queue):
    """Process one complete datagram (bytes), live or replayed."""
    kind = item[4:8]
    if kind == b'#SVP':
        worker.km.OpenBytestoRead(item)
        svp = worker.km.read_EMdgmSVP()
        depth = [round(float(d), 2) for d in svp['sensorData']['depth_m']]
        speed = [round(float(c), 2) for c in svp['sensorData']['soundVelocity_mPerSec']]
        found = worker.set_svp(svp['time_sec'], depth, speed)
        out_queue.put({'type': 'svp', 'time': svp['time_sec'], 'depth_m': depth, 'speed_mps': speed,
                       'thermocline_m': None if found is None else [round(z, 2) for z in found]})
    elif kind == b'#IIP':
        depth = worker.process_installation(item)
        if depth is not None:
            out_queue.put({'type': 'installation', 'transducerDepth_m': round(depth, 3)})
    elif kind == b'#MWC':
        worker.process(item)


class Source:
    """Where the worker's data comes from: real-time (datagrams from the receiver) or replay of a recorded file."""

    def __init__(self, worker, out_queue):
        self.worker = worker
        self.out_queue = out_queue
        self.mode = 'realtime'
        self.replay = None  # the file being replayed, if one is open

    def status(self):
        status = {'type': 'source', 'mode': self.mode}
        if self.replay is not None:
            status['replay'] = self.replay.status()
        return status

    def start_over(self, new_source):
        """Clear what was processed; new_source: the cast belongs to the old source, so drop it too."""
        self.worker.reset()
        if new_source:
            self.worker.svp = None
        self.out_queue.put({'type': 'reset', 'svp': new_source})

    def close_file(self):
        if self.replay is not None:
            self.replay.close()
            self.replay = None

    def command(self, command):
        """
        Apply a command from the server: 'realtime' or 'replay' (switch mode), {'cmd': 'open', 'path'}, 'play',
        'pause', {'cmd': 'speed', 'speed'} or {'cmd': 'seek', 'time'}.
        """
        cmd = command['cmd']
        replay = self.replay
        if cmd in ('realtime', 'replay') and cmd != self.mode:
            self.close_file()
            self.mode = cmd
            self.start_over(True)
            logger.info("Source: %s", 'real-time' if cmd == 'realtime' else 'replay')
        elif cmd == 'open':
            # Indexing a large file, or one on a slow or network drive, can take a while: say so meanwhile.
            self.out_queue.put({'type': 'opening', 'name': os.path.basename(command['path'])})
            try:
                opened = Replay(command['path'])
            except Exception as e:
                logger.exception("Can't replay %s", command['path'])
                self.out_queue.put({'type': 'source', 'error':
                                    f"Can't replay {os.path.basename(command['path'])}: {e}"})
                return
            self.close_file()
            self.replay, self.mode = opened, 'replay'
            self.start_over(True)
            logger.info("Replaying %s", command['path'])
        elif replay is not None and cmd == 'seek':
            self.start_over(False)
            for item in replay.seek(float(command['time'])):
                handle_datagram(self.worker, item, self.out_queue)
        elif replay is not None and cmd in ('play', 'pause'):
            if cmd == 'play' and replay.finished:  # play again from the start
                self.command({'cmd': 'seek', 'time': replay.start})
            replay.set_playing(cmd == 'play')
        elif replay is not None and cmd == 'speed':
            replay.set_speed(max(0.0, float(command['speed'])))
        self.out_queue.put(self.status())


def run(settings, in_queue, settings_queue, control_queue, out_queue, update_interval):
    """Process entry point. Processes real-time datagrams from in_queue, or a file being replayed instead."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    worker = Worker(settings)
    source = Source(worker, out_queue)
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
            while True:
                source.command(control_queue.get_nowait())
        except queue.Empty:
            pass

        # When replaying, wait for the next datagram's time (if before the next update) instead of live data.
        wait_until = next_update
        replay = source.replay
        if replay is not None and replay.playing:
            if replay.finished:
                replay.set_playing(False)
            else:
                now = time.monotonic()
                due = replay.due(now)
                if due <= now:
                    try:
                        handle_datagram(worker, replay.next(), out_queue)
                    except Exception:
                        logger.exception("Failed to process datagram")
                    wait_until = now
                else:
                    wait_until = min(wait_until, due)

        try:
            item = in_queue.get(timeout=max(0.0, wait_until - time.monotonic()))
            if isinstance(item, tuple):
                receiver_stats = item[1]
            elif source.mode == 'realtime':
                handle_datagram(worker, item, out_queue)
            else:  # replaying: live data is ignored
                worker.stats['live_ignored'] = worker.stats.get('live_ignored', 0) + 1
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Failed to process datagram")

        if time.monotonic() >= next_update:
            out_queue.put(worker.take_update(receiver_stats))
            if source.replay is not None:
                out_queue.put(source.status())
            next_update = time.monotonic() + update_interval
