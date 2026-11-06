"""
Experimental tracking of scattering layers in the vertical slice.

A reimplementation of the approach in Roland Arsenault's watercolumn_layer_tracker
(https://github.com/rolker/watercolumn_layer_tracker, C++/ROS):
1. Each profile (one vertical slice column: dB per depth row) is taken relative to a quadratic fit of its
   background level against depth.
2. Candidate slices are depth intervals whose average level above background is at least min_level_db and whose
   thickness is between min_size_m and max_size_m. For each starting depth the best-scoring interval is kept
   (score = average dB above background x thickness), then the best non-overlapping slices are chosen.
3. Each slice joins the layer whose recent extent it overlaps, or is nearest to, if the combined extent stays
   within max_size_m; otherwise it starts a new layer. Layers not extended for max_gap_s end.

Differences from the original: it works on the plotter's averaged nadir-band columns rather than a single nadir
beam, it excludes the seafloor echo and the first metre below the transducer from the background fit and from the
search (the seafloor is the shallowest bottom detection in the band, or else the first value more than seafloor_db
above the profile's median), and layers are not
split at a maximum duration.
"""

import numpy as np


def search_range(depths, values, exclude_top_m, exclude_bottom_m, seafloor_m=None, seafloor_db=20.0):
    """
    Indices [lo, hi) of a profile to search for layers: from exclude_top_m below its first value to exclude_bottom_m
    above the seafloor. The seafloor is seafloor_m if given (the shallowest bottom detection in the profile's band),
    otherwise the first value more than seafloor_db above the profile's median (or its last value).
    """
    ok = np.isfinite(values)
    if ok.sum() < 10:
        return 0, 0
    b = depths[1] - depths[0]
    first, last = np.flatnonzero(ok)[[0, -1]]
    if seafloor_m is not None:
        seafloor = min(int(np.searchsorted(depths, seafloor_m)), last)
    else:
        loud = np.flatnonzero(ok & (np.where(ok, values, -np.inf) > np.nanmedian(values) + seafloor_db))
        seafloor = loud[0] if len(loud) else last
    return first + int(np.ceil(exclude_top_m / b)), seafloor - int(np.ceil(exclude_bottom_m / b))


class LayerTracker:
    def __init__(self, min_level_db=1.0, min_size_m=1.0, max_size_m=25.0, max_gap_s=10.0,
                 exclude_top_m=1.0, exclude_bottom_m=1.0, seafloor_db=20.0):
        self.min_level_db = min_level_db
        self.min_size_m = min_size_m
        self.max_size_m = max_size_m
        self.max_gap_s = max_gap_s
        self.exclude_top_m = exclude_top_m
        self.exclude_bottom_m = exclude_bottom_m
        self.seafloor_db = seafloor_db
        self.layers = []  # {'id', 'slices': [(time, top, bottom, level)]}
        self.next_id = 0

    def slices(self, depths, values, seafloor_m=None):
        """Candidate layer slices in one profile: list of (top, bottom, average dB above background)."""
        # Search between just below the transducer and just above the seafloor echo.
        lo, hi = search_range(depths, values, self.exclude_top_m, self.exclude_bottom_m, seafloor_m, self.seafloor_db)
        if hi - lo < 10:
            return []
        b = depths[1] - depths[0]
        d, v = depths[lo:hi], values[lo:hi]
        fit = np.isfinite(v)
        coeffs = np.polyfit(d[fit], v[fit], 2)
        rel = np.where(fit, v - np.polyval(coeffs, d), -np.inf)

        n = len(rel)
        min_bins = max(1, int(round(self.min_size_m / b)))
        max_bins = max(min_bins, int(round(self.max_size_m / b)))
        csum = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(rel), rel, 0.0))])
        candidates = []
        for i in np.flatnonzero(rel >= self.min_level_db):
            lengths = np.arange(min_bins, min(max_bins, n - i) + 1)
            if not len(lengths):
                continue
            avg = (csum[i + lengths] - csum[i]) / lengths
            score = np.where(avg >= self.min_level_db, avg * lengths * b, -np.inf)
            k = int(np.argmax(score))
            if np.isfinite(score[k]):
                candidates.append((score[k], d[i], d[i] + lengths[k] * b, avg[k]))
        chosen = []
        for score, top, bottom, level in sorted(candidates, reverse=True):
            if all(bottom <= t or top >= bt for t, bt, _ in chosen):
                chosen.append((top, bottom, level))
        return chosen

    def add_profile(self, time, depths, values, seafloor_m=None):
        """Track the layers in one profile. Returns [(layer id, top, bottom), ...] for this profile."""
        self.layers = [L for L in self.layers if time - L['slices'][-1][0] <= self.max_gap_s]
        found = []
        for top, bottom, level in self.slices(depths, values, seafloor_m):
            best, best_score = None, 0.0
            for L in self.layers:
                recent = [s for s in L['slices'] if time - s[0] <= self.max_gap_s]
                w = np.array([s[2] - s[1] for s in recent])
                m_top = np.dot(w, [s[1] for s in recent]) / w.sum()
                m_bottom = np.dot(w, [s[2] for s in recent]) / w.sum()
                if max(bottom, m_bottom) - min(top, m_top) > self.max_size_m * 1.1:
                    continue
                gap = max(0.0, max(top, m_top) - min(bottom, m_bottom))
                score = 0.5 - gap / (m_bottom - m_top)
                if score > best_score:
                    best, best_score = L, score
            if best is None:
                best = {'id': self.next_id, 'slices': []}
                self.next_id += 1
                self.layers.append(best)
            best['slices'].append((time, top, bottom, level))
            found.append((best['id'], round(float(top), 2), round(float(bottom), 2)))
        return found
