"""
Medians over the samples of one swath, used for sidelobe suppression. Samples are flat arrays with a beam index and a
range sample number each (as gridding.grid_mwc builds them), so these work for any sonar's beam-formed samples.
"""

import numpy as np


def sorted_median(values, row, col, counts):
    """Median of values per row, where (row, col) places each value in a table and counts is values per row."""
    table = np.full((len(counts), int(col.max()) + 1), np.inf, dtype=np.float32)
    table[row, col] = values
    table.sort(axis=1)  # missing entries (inf) sort to the end of each row
    lo, hi, r = np.maximum((counts - 1) // 2, 0), np.maximum(counts // 2, 0), np.arange(len(counts))
    return (table[r, lo] + table[r, hi]) / 2


def across_beam_median(values, beam_index, sample_number, group=None):
    """
    For every sample, the median (dB) of all beams' samples at the same range sample number, or, if group (an integer
    per sample, such as the transmit sector) is given, of the beams in the same group. Subtracting it suppresses
    features that are the same across beams at a given range, such as the seafloor echo received through the
    sidelobes beyond the minimum slant range.
    """
    s = sample_number - sample_number.min()
    n = int(s.max()) + 1
    row = s if group is None else (group - group.min()) * n + s  # one table row per (group,) range sample
    counts = np.bincount(row, minlength=int(row.max()) + 1)
    return sorted_median(values, row, beam_index, counts)[row]


def per_beam_split_median(values, beam_index, sample_number, split_sample):
    """
    For every sample, its beam's median over the samples on the same side of split_sample: before it, or from it on.
    With split_sample the minimum slant range, the two sides are water only and water plus whatever the sidelobes
    receive from the seafloor.
    """
    after = (sample_number >= split_sample).astype(np.int64)
    row = beam_index * 2 + after  # one table row per (beam, side)
    counts = np.bincount(row, minlength=2 * (int(beam_index.max()) + 1))
    return sorted_median(values, row, sample_number - sample_number.min(), counts)[row]
