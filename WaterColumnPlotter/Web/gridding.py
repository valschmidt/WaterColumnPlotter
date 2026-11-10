"""
Gridding of Kongsberg #MWC water column datagrams and Teledyne RESON 7k 7018 beamformed pings, and the across-track
band of the tracking column.

Cells are square, bin_size metres on a side:
- row r covers depth [r * bin_size, (r + 1) * bin_size), depth positive down, measured from the transducer's resting
  (heave-corrected) depth, or from the water surface when a depth offset (the transducer's depth) is given. Rows can
  be negative: heave can raise samples above the transducer's resting depth;
- column c covers across-track [c * bin_size, (c + 1) * bin_size), starboard positive, so port is on the left.

Each ping is gridded into a block just large enough for its samples, given with the index of its first row and
column, so no grid size has to be chosen and no samples are dropped.
"""

import collections
import itertools

import numpy as np

from WaterColumnPlotter.Web import kongsberg
from WaterColumnPlotter.Web.medians import across_beam_median, per_beam_floor_fit, per_beam_split_median

# Largest block one datagram may be gridded into. A cell size that is very small for the swath would otherwise use
# a great deal of memory.
MAX_CELLS = 2_000_000


class GridTooLarge(Exception):
    pass


class EmptyPing(Exception):
    """The ping has no samples to grid."""


class NoBackground(Exception):
    """With 'ping_mean', no sample has a background yet (each receive fan's first ping)."""


# Sidelobe suppression choices: for Kongsberg, the median options use the transmit sectors and are in kongsberg.py
# (7k pings, in grid_7018, are taken as one sector).
# 'ping_mean' subtracts, from each beam's sample at each range, that beam's mean at that range over the previous
# swaths (see PingBackground): whatever stays put from swath to swath.
SIDELOBE_SUPPRESSION = ('off',) + kongsberg.OPTIONS + ('ping_mean',)


class PingBackground:
    """
    Each beam's mean level (dB) at each range sample over the previous n swaths of the same receive fan (a dual-swath
    system's two swaths per ping cycle point in different directions, so each keeps its own background). Subtracting
    it removes what is the same from swath to swath in each beam: the seafloor echo received through the sidelobes
    beyond the minimum slant range, near-field ringing, steady interference, and also layers that don't change over n
    swaths.
    """

    def __init__(self, n):
        self.n = n
        self.fans = {}  # key -> {'history': deque of (beams x samples) float16 arrays, 'sum', 'count'}

    def subtract(self, key, beam_index, sample_number, amplitudes):
        """
        Returns (amplitudes less the background, mask of the samples that had a background), and adds this ping to
        the background. Samples with no history (the first ping, or ranges not reached before) have no background.
        """
        nbeams, nsamples = int(beam_index.max()) + 1, int(sample_number.max()) + 1
        ping = np.full((nbeams, nsamples), np.nan, dtype=np.float32)
        ping[beam_index, sample_number] = amplitudes
        fan = self.fans.get(key)
        if fan is None or fan['sum'].shape[0] != nbeams:  # new fan, or the beams changed
            fan = self.fans[key] = {'history': collections.deque(), 'sum': np.zeros((nbeams, nsamples)),
                                    'count': np.zeros((nbeams, nsamples), dtype=np.int32)}
        if fan['sum'].shape[1] < nsamples:  # longer ranges than before
            grow = ((0, 0), (0, nsamples - fan['sum'].shape[1]))
            fan['sum'], fan['count'] = np.pad(fan['sum'], grow), np.pad(fan['count'], grow)

        count = fan['count'][beam_index, sample_number]
        has_background = count > 0
        with np.errstate(invalid='ignore', divide='ignore'):
            background = fan['sum'][beam_index, sample_number] / count
        out = (amplitudes - background)[has_background].astype(np.float32)

        # This ping joins the background; pings beyond the last n leave it.
        ok = np.isfinite(ping)
        fan['sum'][:, :nsamples] += np.where(ok, ping, 0.0)
        fan['count'][:, :nsamples] += ok
        fan['history'].append(ping.astype(np.float16))
        while len(fan['history']) > self.n:
            old = fan['history'].popleft().astype(np.float32)
            ok = np.isfinite(old)
            fan['sum'][:, :old.shape[1]] -= np.where(ok, old, 0.0)
            fan['count'][:, :old.shape[1]] -= ok
        return out, has_background


def limit_to_bottom(det, start, num_samples):
    """
    The samples to use from each beam: those up to and including its bottom detection, and only samples that exist.
    det: each beam's bottom detection (range sample number, 0 where it has none); start, num_samples: each beam's
    first range sample number and number of samples. Beams without a bottom detection use the swath's mean detection
    range. When no beam has one (the seafloor beyond the recorded range, deep water or a short water column range, or
    detection failed), every recorded sample is used, with no sidelobe region beyond a minimum slant range, rather
    than dropping the ping.
    :return: (det, detected, n_use, min_slant_range): det filled in, the beams with a detection, samples used per beam,
    and the range sample number from which the sidelobe region starts.
    """
    detected = det > 0
    if detected.any():
        det = det.copy()
        det[~detected] = int(det[detected].mean())
        min_slant_range = int(det.min())
    else:
        det = start + num_samples - 1
        min_slant_range = int(det.max()) + 1
    n_use = np.clip(np.minimum(num_samples, det - start + 1), 0, None)
    return det, detected, n_use, min_slant_range


def sample_indices(n_use, start):
    """
    Beam index and range sample number of every sample used, beam by beam (n_use samples from each beam's start).
    :raises EmptyPing: if no sample is used.
    """
    total = int(n_use.sum())
    if total == 0:
        raise EmptyPing
    beam_index = np.repeat(np.arange(len(n_use)), n_use)
    first = np.repeat(np.cumsum(n_use) - n_use, n_use)
    sample_number = np.arange(total) - first + start[beam_index]
    return beam_index, sample_number


def subtract_ping_mean(background, key, amplitudes, beam_index, sample_number):
    """
    'ping_mean' sidelobe suppression: the amplitudes less each beam's background (a PingBackground; key: the receive
    fan), with the beam index and range sample number of the samples kept. Samples with no history yet are left out,
    rather than shown on a different (absolute dB) scale.
    :raises NoBackground: if no sample has a background yet.
    """
    amplitudes, has_background = background.subtract(key, beam_index, sample_number, amplitudes)
    if not has_background.any():
        raise NoBackground
    return amplitudes, beam_index[has_background], sample_number[has_background]


def bin_samples(amplitudes, beam_index, sample_number, metres_per_sample, beam_angle, tilt, vertical_offset, det,
                detected, bin_size):
    """
    Grid one swath's samples (amplitudes in dB, each with its beam index and range sample number).
    metres_per_sample: one-way range per range sample (sound speed / (2 * sample rate)). beam_angle: each beam's angle
    from vertical (radians, positive to starboard); tilt: each beam's transmit tilt along track (radians).
    vertical_offset (m) is added to every depth. det, detected: each beam's bottom detection (range sample number)
    and whether it has one.
    :return: (sums, counts, row0, col0, bottom), as grid_mwc returns.
    :raises GridTooLarge: if the block would exceed MAX_CELLS cells.
    """
    m = np.float32(metres_per_sample)
    sample_range = sample_number.astype(np.float32) * m
    across = sample_range * np.sin(beam_angle)[beam_index]
    vertical_offset = np.float32(vertical_offset)
    depth = sample_range * (np.cos(tilt) * np.cos(beam_angle))[beam_index] + vertical_offset
    det_range = det[detected] * m
    bottom = (det_range * np.sin(beam_angle[detected]),
              det_range * np.cos(tilt[detected]) * np.cos(beam_angle[detected]) + vertical_offset)

    col = np.floor(across / bin_size).astype(np.int64)
    row = np.floor(depth / bin_size).astype(np.int64)
    row0, col0 = int(row.min()), int(col.min())
    nrows, ncols = int(row.max()) - row0 + 1, int(col.max()) - col0 + 1
    if nrows * ncols > MAX_CELLS:
        raise GridTooLarge(f"{nrows} x {ncols} cells")

    cell = (row - row0) * ncols + (col - col0)
    sums = np.bincount(cell, weights=amplitudes, minlength=nrows * ncols).reshape(nrows, ncols)
    counts = np.bincount(cell, minlength=nrows * ncols).reshape(nrows, ncols)
    return sums, counts, row0, col0, bottom


def grid_mwc(dg, bin_size, depth_offset=0.0, sidelobe_suppression='off', background=None):
    """
    Grid one #MWC datagram (as returned by pykmall's kmall.read_EMdgmMWC()). depth_offset (m) is added to every
    depth: the transducer's depth below the waterline, to reference depths to the water surface.
    sidelobe_suppression: 'off'; one of kongsberg.OPTIONS, medians subtracted using the transmit sectors (values are
    then dB relative to them); or 'ping_mean', which subtracts each beam's mean over the previous swaths, from background (a PingBackground), which this ping is then added to.
    :return: (sums, counts, row0, col0, bottom): 2D arrays of summed amplitude (dB) and sample count per cell, the
    row and column index of their first cell, and the bottom detections as (across-track, depth) arrays (m) for the
    beams that have one (empty if none has).
    :raises EmptyPing: if the ping has no samples.
    :raises NoBackground: with 'ping_mean', if no sample has a background yet.
    :raises GridTooLarge: if the block would exceed MAX_CELLS cells.
    """
    beam = dg['beamData']
    start = np.asarray(beam['startRangeSampleNum'], dtype=np.int64)
    num_samples = np.asarray(beam['numSampleData'], dtype=np.int64)
    det, detected, n_use, min_slant_range = limit_to_bottom(
        np.asarray(beam['detectedRangeInSamples'], dtype=np.int64), start, num_samples)
    beam_index, sample_number = sample_indices(n_use, start)

    amplitudes = np.fromiter(itertools.chain.from_iterable(s[:k] for s, k in zip(beam['sampleAmplitude05dB_p'], n_use)),
                             dtype=np.float32, count=len(beam_index))
    amplitudes = amplitudes * 0.5 - dg['rxInfo']['TVGoffset_dB']

    if sidelobe_suppression in kongsberg.OPTIONS:
        amplitudes = kongsberg.suppress_sidelobes(dg, sidelobe_suppression, amplitudes, beam_index, sample_number,
                                                  min_slant_range)
    elif sidelobe_suppression == 'ping_mean':
        key = (dg['cmnPart']['rxTransducerInd'], dg['cmnPart']['rxFanIndex'])
        amplitudes, beam_index, sample_number = subtract_ping_mean(background, key, amplitudes, beam_index,
                                                                   sample_number)

    rx = dg['rxInfo']
    # Kongsberg beam angles are positive to port; across-track here is positive to starboard.
    beam_angle = -np.radians(np.asarray(beam['beamPointAngReVertical_deg'], dtype=np.float32))
    # TODO: add pitch (from #SKM) to the sector tilt to get tilt relative to vertical.
    tilt = np.radians(np.asarray(dg['sectorData']['tiltAngleReTx_deg'], dtype=np.float32)
                      [np.asarray(beam['beamTxSectorNum'])])
    return bin_samples(amplitudes, beam_index, sample_number, rx['soundVelocity_mPerSec'] / (2.0 * rx['sampleFreq_Hz']),
                       beam_angle, tilt, dg['txInfo']['heave_m'] + depth_offset, det, detected, bin_size)


def grid_7018(wc, settings, geometry, detections, heave, bin_size, depth_offset=0.0, sidelobe_suppression='off',
              background=None):
    """
    Grid one Teledyne RESON 7k ping: its 7018 beamformed water column, with the same ping's 7000 sonar settings
    (sample rate, sound speed, transmit tilt), the 7004 beam geometry and the ping's 7027 bottom detections (None if
    there are none), each a record as pys7k's decode_record returns it. heave (m, positive up, from 1012) is
    subtracted from every depth, and depth_offset (m) added, as in grid_mwc.
    Magnitudes are shown as 20 log10(magnitude) dB (uncalibrated). The median sidelobe suppression options take the
    whole swath as one transmit sector. Returns, and raises, as grid_mwc.

    Geometry, checked on T51R data (docs/browser-design.md): 7004 beam angles are positive to starboard (as the 7k
    DFD defines every steering angle) and already relative to vertical (the receiver steers for roll: 7027
    AppliedRoll is 0), so roll isn't applied here; 7018 samples start at transmit, sample 0.
    """
    magnitude = wc['RD']['Amplitude']  # (samples, beams)
    nsamples, nbeams = magnitude.shape
    rth = settings['RTH']
    det = np.zeros(nbeams, dtype=np.int64)
    if detections is not None:
        found = detections['RD']['Detections']
        beam = found['BeamDescriptor'].astype(np.int64)
        point = found['DetectionPoint'] * (rth['SampleRate'] / detections['RTH']['SamplingRate'])
        ok = ((found['Flags'] & 3) != 0) & (point > 0) & (beam < nbeams)  # magnitude or phase detection
        det[beam[ok]] = np.round(point[ok]).astype(np.int64)
    start = np.zeros(nbeams, dtype=np.int64)
    det, detected, n_use, min_slant_range = limit_to_bottom(det, start, np.full(nbeams, nsamples, dtype=np.int64))
    beam_index, sample_number = sample_indices(n_use, start)

    used = np.arange(nsamples)[None, :] < n_use[:, None]  # (beams, samples), in the order of beam_index
    amplitudes = np.float32(20) * np.log10(np.maximum(magnitude.T[used], 1).astype(np.float32))

    if sidelobe_suppression == 'median_beam':
        amplitudes = amplitudes - per_beam_split_median(amplitudes, beam_index, sample_number, min_slant_range)
    if sidelobe_suppression in kongsberg.OPTIONS:
        amplitudes = amplitudes - across_beam_median(amplitudes, beam_index, sample_number)
    if sidelobe_suppression == 'median_range_poly_beam':
        amplitudes = amplitudes - per_beam_floor_fit(amplitudes, beam_index, sample_number)
    if sidelobe_suppression == 'ping_mean':
        amplitudes, beam_index, sample_number = subtract_ping_mean(
            background, ('7k', wc['RTH']['MultiPingSequence']), amplitudes, beam_index, sample_number)

    beam_angle = np.asarray(geometry['RD']['Beams']['BeamHorizontalDirection'], dtype=np.float32)[:nbeams]
    tilt = np.full(nbeams, rth['ProjectorBeamSteeringAngleVertical'], dtype=np.float32)
    return bin_samples(amplitudes, beam_index, sample_number, rth['SoundVelocity'] / (2.0 * rth['SampleRate']),
                       beam_angle, tilt, depth_offset - heave, det, detected, bin_size)


def vertical_band(settings):
    """Columns [c0, c1) averaged for the tracking column under the vessel: across-track within +/- acrossTrackAvg_m / 2."""
    b = settings['binSize_m']
    half = settings['acrossTrackAvg_m'] / 2
    c0 = int(np.floor(-half / b))
    return c0, max(c0 + 1, int(np.ceil(half / b)))


def add_blocks(a, b):
    """
    Sum two blocks that may cover different index ranges. A block is (offsets, sums, counts), where offsets gives
    the index of the first element along each axis. Returns a block covering both.
    """
    (off_a, sa, ca), (off_b, sb, cb) = a, b
    if not sb.size:
        return a  # an empty block adds nothing, and its offset shouldn't widen the result
    if not sa.size:
        return b
    lo = [min(x, y) for x, y in zip(off_a, off_b)]
    hi = [max(x + n, y + m) for x, y, n, m in zip(off_a, off_b, sa.shape, sb.shape)]
    sums = np.zeros([h - l for l, h in zip(lo, hi)])
    counts = np.zeros(sums.shape, dtype=np.result_type(ca, cb))  # counts, or weights (float)
    for off, s, c in ((off_a, sa, ca), (off_b, sb, cb)):
        idx = tuple(slice(o - l, o - l + n) for o, l, n in zip(off, lo, s.shape))
        sums[idx] += s
        counts[idx] += c
    return tuple(lo), sums, counts


def average(block):
    """(offsets, sums, counts) -> (offsets, float32 averages with NaN where there is no data)."""
    off, sums, counts = block
    with np.errstate(invalid='ignore', divide='ignore'):
        return off, (sums / counts).astype(np.float32)
