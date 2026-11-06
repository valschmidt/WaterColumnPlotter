"""
Gridding of Kongsberg #MWC water column datagrams, and the slice geometry used for display.

Grid layout (n x n cells of bin_size metres):
- Row r covers depth [(r - heave_rows) * bin_size, (r - heave_rows + 1) * bin_size), where
  heave_rows = round(max_heave / bin_size); depth is positive down from the transducer.
- Column c covers across-track [(c - n // 2) * bin_size, (c - n // 2 + 1) * bin_size), starboard positive,
  so port is on the left (low column index).
"""

import itertools

import numpy as np


def heave_rows(bin_size, max_heave):
    """Number of grid rows above 0 m depth, reserved for upward heave."""
    return int(round(max_heave / bin_size))


def grid_mwc(dg, bin_size, max_heave, n):
    """
    Grid one #MWC datagram (as returned by pykmall's kmall.read_EMdgmMWC()).
    :return: (sums, counts, num_outside): n x n arrays of summed amplitude (dB) and sample count per cell, and the
    number of samples that fell outside the grid. Returns None if the ping has no bottom detections.
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
    depth = sample_range * (np.cos(tilt) * np.cos(beam_angle))[beam_index] + np.float32(dg['txInfo']['heave_m'])

    col = np.floor(across / bin_size).astype(np.int64) + n // 2
    row = np.floor(depth / bin_size).astype(np.int64) + heave_rows(bin_size, max_heave)
    inside = (col >= 0) & (col < n) & (row >= 0) & (row < n)

    cell = row[inside] * n + col[inside]
    sums = np.bincount(cell, weights=amplitudes[inside], minlength=n * n).reshape(n, n)
    counts = np.bincount(cell, minlength=n * n).reshape(n, n)
    return sums, counts, int(total - inside.sum())


def vertical_band(settings):
    """Grid columns [c0, c1) averaged for the vertical slice: across-track within +/- acrossTrackAvg_m / 2."""
    n, b = settings['maxGridCells'], settings['binSize_m']
    half = max(1, int(round(settings['acrossTrackAvg_m'] / 2 / b)))
    return max(0, n // 2 - half), min(n, n // 2 + half)


def horizontal_band(settings):
    """Grid rows [r0, r1) averaged for the horizontal slice: sliceDepth_m +/- sliceThickness_m / 2."""
    n, b = settings['maxGridCells'], settings['binSize_m']
    h = heave_rows(b, settings['maxHeave_m'])
    top = settings['sliceDepth_m'] - settings['sliceThickness_m'] / 2
    bottom = settings['sliceDepth_m'] + settings['sliceThickness_m'] / 2
    r0 = h + int(np.floor(top / b))
    r1 = max(r0 + 1, h + int(np.ceil(bottom / b)))
    return max(0, r0), min(n, r1)
