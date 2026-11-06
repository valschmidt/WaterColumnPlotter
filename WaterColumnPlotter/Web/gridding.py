"""
Gridding of Kongsberg #MWC water column datagrams, and the slice geometry used for display.

Cells are square, bin_size metres on a side:
- row r covers depth [r * bin_size, (r + 1) * bin_size), depth positive down, measured from the transducer's resting
  (heave-corrected) depth, or from the water surface when a depth offset (the transducer's depth) is given. Rows can
  be negative: heave can raise samples above the transducer's resting depth;
- column c covers across-track [c * bin_size, (c + 1) * bin_size), starboard positive, so port is on the left.

Each ping is gridded into a block just large enough for its samples, given with the index of its first row and
column, so no grid size has to be chosen and no samples are dropped.
"""

import itertools

import numpy as np

# Largest block one datagram may be gridded into. A cell size that is very small for the swath would otherwise use
# a great deal of memory.
MAX_CELLS = 2_000_000


class GridTooLarge(Exception):
    pass


def grid_mwc(dg, bin_size, depth_offset=0.0):
    """
    Grid one #MWC datagram (as returned by pykmall's kmall.read_EMdgmMWC()). depth_offset (m) is added to every
    depth: the transducer's depth below the waterline, to reference depths to the water surface.
    :return: (sums, counts, row0, col0): 2D arrays of summed amplitude (dB) and sample count per cell, and the
    row and column index of their first cell. Returns None if the ping has no bottom detections.
    :raises GridTooLarge: if the block would exceed MAX_CELLS cells.
    """
    beam = dg['beamData']
    det = np.asarray(beam['detectedRangeInSamples'], dtype=np.int64)
    if not det.any():
        return None
    # Beams without a bottom detection use the swath's mean detection range.
    det[det == 0] = int(det[det > 0].mean())

    start = np.asarray(beam['startRangeSampleNum'], dtype=np.int64)
    num_samples = np.asarray(beam['numSampleData'], dtype=np.int64)
    # Use samples up to and including the bottom detection, and only samples that exist.
    n_use = np.clip(np.minimum(num_samples, det - start + 1), 0, None)
    total = int(n_use.sum())
    if total == 0:
        return None

    amplitudes = np.fromiter(itertools.chain.from_iterable(s[:k] for s, k in zip(beam['sampleAmplitude05dB_p'], n_use)),
                             dtype=np.float32, count=total)
    amplitudes = amplitudes * 0.5 - dg['rxInfo']['TVGoffset_dB']

    # Beam index and range sample number of every sample used.
    beam_index = np.repeat(np.arange(len(n_use)), n_use)
    first = np.repeat(np.cumsum(n_use) - n_use, n_use)
    sample_number = np.arange(total) - first + start[beam_index]

    rx = dg['rxInfo']
    sample_range = sample_number.astype(np.float32) * np.float32(rx['soundVelocity_mPerSec'] / (2.0 * rx['sampleFreq_Hz']))

    # Per-beam geometry, then expanded to samples.
    beam_angle = np.radians(np.asarray(beam['beamPointAngReVertical_deg'], dtype=np.float32))
    # TODO: add pitch (from #SKM) to the sector tilt to get tilt relative to vertical.
    tilt = np.radians(np.asarray(dg['sectorData']['tiltAngleReTx_deg'], dtype=np.float32)
                      [np.asarray(beam['beamTxSectorNum'])])
    # Kongsberg beam angles are positive to port; across-track here is positive to starboard.
    across = sample_range * (-np.sin(beam_angle))[beam_index]
    depth = sample_range * (np.cos(tilt) * np.cos(beam_angle))[beam_index] \
        + np.float32(dg['txInfo']['heave_m'] + depth_offset)

    col = np.floor(across / bin_size).astype(np.int64)
    row = np.floor(depth / bin_size).astype(np.int64)
    row0, col0 = int(row.min()), int(col.min())
    nrows, ncols = int(row.max()) - row0 + 1, int(col.max()) - col0 + 1
    if nrows * ncols > MAX_CELLS:
        raise GridTooLarge(f"{nrows} x {ncols} cells")

    cell = (row - row0) * ncols + (col - col0)
    sums = np.bincount(cell, weights=amplitudes, minlength=nrows * ncols).reshape(nrows, ncols)
    counts = np.bincount(cell, minlength=nrows * ncols).reshape(nrows, ncols)
    return sums, counts, row0, col0


def vertical_band(settings):
    """Columns [c0, c1) averaged for the vertical slice: across-track within +/- acrossTrackAvg_m / 2."""
    b = settings['binSize_m']
    half = settings['acrossTrackAvg_m'] / 2
    c0 = int(np.floor(-half / b))
    return c0, max(c0 + 1, int(np.ceil(half / b)))


def horizontal_band(settings):
    """Rows [r0, r1) averaged for the horizontal slice: sliceDepth_m +/- sliceThickness_m / 2."""
    b = settings['binSize_m']
    r0 = int(np.floor((settings['sliceDepth_m'] - settings['sliceThickness_m'] / 2) / b))
    return r0, max(r0 + 1, int(np.ceil((settings['sliceDepth_m'] + settings['sliceThickness_m'] / 2) / b)))


def add_blocks(a, b):
    """
    Sum two blocks that may cover different index ranges. A block is (offsets, sums, counts), where offsets gives
    the index of the first element along each axis. Returns a block covering both.
    """
    (off_a, sa, ca), (off_b, sb, cb) = a, b
    lo = [min(x, y) for x, y in zip(off_a, off_b)]
    hi = [max(x + n, y + m) for x, y, n, m in zip(off_a, off_b, sa.shape, sb.shape)]
    sums = np.zeros([h - l for l, h in zip(lo, hi)])
    counts = np.zeros(sums.shape, dtype=np.int64)
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
