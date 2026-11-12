"""
Medians over the samples of one swath, and a per-beam noise floor fit, used for sidelobe suppression. Samples are flat arrays with a beam index and a
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


FLOOR_FIT_EPSILON = 1e-12  # added to linear power before inverting it for a weight (-120 dB): a safeguard only


def per_beam_floor_fit(values, beam_index, sample_number, degree=2):
    """
    For every sample, its beam's noise floor: a polynomial of the given degree in range sample number, fitted to the
    beam's values (dB) by weighted least squares with weights 1 / linear power (1 / (10^(dB/10) + FLOOR_FIT_EPSILON)),
    so samples near the floor dominate and strong echoes (seafloor, sidelobes, targets) pull the fit up very little:
    a sample 30 dB above the floor has 1/1000 of the weight. A beam with too few samples for the degree gets the
    minimum-norm solution.
    """
    if np.any(np.diff(beam_index) < 0):  # each beam's samples together, for np.add.reduceat
        order = np.argsort(beam_index, kind='stable')
        fit = np.empty_like(values)
        fit[order] = per_beam_floor_fit(values[order], beam_index[order], sample_number[order], degree)
        return fit
    v = values.astype(np.float64)
    s0, s1 = int(sample_number.min()), int(sample_number.max())
    x = (sample_number - s0) * (1.0 / max(s1 - s0, 1))  # range scaled to [0, 1], for well-conditioned sums
    w = 1.0 / (np.exp(v * (np.log(10.0) / 10.0)) + FLOOR_FIT_EPSILON)  # 1 / (10^(dB/10) + epsilon)
    starts = np.flatnonzero(np.r_[True, np.diff(beam_index) != 0])  # where each beam's samples begin
    group = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(beam_index)]))  # beam's position in starts
    n = degree + 1
    # Per beam: sums of w x^k (k = 0 .. 2 degree) and of w x^k y (k = 0 .. degree), building the powers in place.
    sw, swy = np.empty((len(starts), 2 * n - 1)), np.empty((len(starts), n))
    wx, wxy = w, w * v
    for k in range(2 * n - 1):
        sw[:, k] = np.add.reduceat(wx, starts)
        if k < n:
            swy[:, k] = np.add.reduceat(wxy, starts)
            wxy = wxy * x
        wx = wx * x
    normal = sw[:, np.arange(n)[:, None] + np.arange(n)[None, :]]  # (beams, n, n): sum of w x^(i + j)
    coef = (np.linalg.pinv(normal) @ swy[:, :, None])[:, :, 0]  # (beams, n), lowest power first
    fit = coef[group, n - 1]
    for j in range(n - 2, -1, -1):  # Horner's rule
        fit = fit * x + coef[group, j]
    return fit.astype(values.dtype)
