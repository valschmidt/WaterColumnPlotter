"""
Experimental: tracking the bottom of the thermocline in the vertical slice, after Jose Cordero Ros, "Improved Sound
Speed Control Through Remotely Detecting Strong Changes in the Thermocline" (MS thesis, UNH, 2018).

Cordero found that a scattering layer mimics the bottom of the thermocline, and tracked it in EK60 echograms with:
1. A low-pass box filter (best: 310 m along track x 1 m vertically) to remove speckle.
2. A vertical gradient filter (2 m) to find edges.
3. A high-pass filter on the gradient (6 m), leaving only gradients that change over less than that.
4. The deepest negative peak beyond a threshold (intensity falling fastest with depth: the bottom of the scattering
   layer) in each ping, cleaned with a Hampel filter (25 pings, 2 standard deviations).
The thermocline in a sound speed profile runs from the shallowest to the deepest depth whose gradient is at least half
the maximum. Each pick is compared with the last cast's thermocline bottom: the cast profile is shifted vertically by
the difference (Cordero found the thermocline's thickness nearly constant over hours) and both profiles are ray traced
at the outer beam angle (65 degrees) to the seafloor. A depth error beyond a limit (0.5 m in the thesis, about half the
IHO Order 1 allowance) means a new cast is warranted.

Differences here: the input is the plotter's vertical slice (averaged multibeam water column in dB, in time order)
rather than three EK60 channels, so the along-track filter is a trailing window in seconds, and the seafloor echo and
the first metre below the transducer are excluded from the search. Picks are also tracked: once established, each
pick is the candidate nearest the recent picks (within track_m), so a shallower layer can't take over when the
thermocline's edge briefly weakens. The deepest candidate is picked again after hampel_n columns without a pick, or
when an edge more than track_m deeper appears in at least half of the last hampel_n columns.
"""

import collections

import numpy as np

from WaterColumnPlotter.Web.layers import search_range


def box_mean(x, n):
    """Centred running mean of x over n samples, ignoring NaNs (NaN where the window holds no data)."""
    n = max(1, int(n))
    ok = np.isfinite(x)
    s = np.concatenate([[0.0], np.cumsum(np.where(ok, x, 0.0))])
    c = np.concatenate([[0], np.cumsum(ok)])
    i = np.arange(len(x))
    lo = np.clip(i - n // 2, 0, len(x))
    hi = np.clip(i - n // 2 + n, 0, len(x))
    count = c[hi] - c[lo]
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(count > 0, (s[hi] - s[lo]) / count, np.nan)


def svp_thermocline(depth, speed):
    """
    Thermocline of a sound speed profile: (top, bottom) depths, the shallowest and deepest depths whose sound speed
    gradient magnitude is at least half the maximum, or None if the profile is too short.
    """
    depth, speed = np.asarray(depth, float), np.asarray(speed, float)
    keep = np.flatnonzero(np.diff(depth, prepend=-np.inf) > 0)  # increasing depths only
    depth, speed = depth[keep], speed[keep]
    if len(depth) < 3:
        return None
    z = np.arange(depth[0], depth[-1], 0.5)  # regular 0.5 m grid, smoothed over 2 m
    if len(z) < 5:
        return None
    g = np.abs(np.gradient(box_mean(np.interp(z, depth, speed), 5), z))
    strong = np.flatnonzero(g >= 0.5 * g.max())
    return float(z[strong[0]]), float(z[strong[-1]])


def shifted_profile(depth, speed, top, bottom, shift):
    """
    The profile with its thermocline moved down by shift (m), keeping its structure: depths above the thermocline
    are stretched or squeezed between the surface and the new top, and the rest moved by shift.
    """
    depth = np.asarray(depth, float)
    new_top = max(0.0, top + shift)
    out = np.where(depth <= top, depth * (new_top / top) if top > 0 else depth, depth + shift)
    return out, np.asarray(speed, float)


def ray_trace(depth, speed, z0, angle_deg, z_end=None, travel_time=None, dz=0.1):
    """
    Trace a ray launched from depth z0 at angle_deg from vertical through a layered profile (constant speed in
    dz layers, held constant beyond the profile's ends), down to depth z_end or for a one-way travel_time.
    Returns (depth, horizontal distance, travel time) at the end, or None if the ray turns before reaching it.
    """
    z = np.arange(z0, (z_end if z_end is not None else z0 + 12000.0), dz) + dz / 2
    c = np.interp(z, depth, speed)
    p = np.sin(np.radians(angle_deg)) / np.interp(z0, depth, speed)
    sin = p * c
    if travel_time is None:
        if np.any(sin >= 1):
            return None
        cos = np.sqrt(1 - sin ** 2)
        return float(z_end), float(np.sum(dz * sin / cos)), float(np.sum(dz / (c * cos)))
    turn = np.flatnonzero(sin >= 1)
    n = turn[0] if len(turn) else len(z)
    cos = np.sqrt(1 - sin[:n] ** 2)
    t = np.cumsum(dz / (c[:n] * cos))
    k = int(np.searchsorted(t, travel_time))
    if k >= n:
        return None
    t_prev = t[k - 1] if k else 0.0
    frac = (travel_time - t_prev) / (t[k] - t_prev)  # part of layer k crossed
    x = np.sum(dz * sin[:k] / cos[:k]) + frac * dz * sin[k] / cos[k]
    return float(z0 + (k + frac) * dz), float(x), float(travel_time)


def depth_error(depth, speed, shift, top, bottom, z0, seafloor, angle_deg):
    """
    Seafloor depth error (m, positive too deep) at angle_deg when the cast profile is used but its thermocline has
    moved down by shift: travel time is traced through the shifted (true) profile and depth recovered with the cast.
    """
    if shift == 0 or seafloor <= z0:
        return 0.0
    true_depth, true_speed = shifted_profile(depth, speed, top, bottom, shift)
    true = ray_trace(true_depth, true_speed, z0, angle_deg, z_end=seafloor)
    if true is None:
        return None
    cast = ray_trace(depth, speed, z0, angle_deg, travel_time=true[2])
    if cast is None:
        return None
    return cast[0] - seafloor


MIN_TRACK_PICKS = 5  # picks needed before tracking takes over from picking the deepest candidate


class ThermoclineTracker:
    def __init__(self, threshold_db_per_m=0.5, along_track_s=60.0, smooth_m=1.0, gradient_m=2.0,
                 highpass_m=6.0, hampel_n=25, hampel_sigma=2.0, track_m=3.0):
        self.threshold = threshold_db_per_m
        self.along_track_s = along_track_s
        self.smooth_m = smooth_m
        self.gradient_m = gradient_m
        self.highpass_m = highpass_m
        self.hampel_n = hampel_n
        self.hampel_sigma = hampel_sigma
        self.track_m = track_m  # once tracking, picks must be within this of the recent median
        self.misses = 0  # consecutive columns without a pick while tracking
        self.deeper = collections.deque(maxlen=hampel_n)  # while tracking: whether each column had a deeper edge
        self.recent = collections.deque()  # (time, first row, values) of the columns in the along-track window
        self.picks = collections.deque(maxlen=hampel_n)  # raw picks (m), for the Hampel filter

    def filtered(self, time, row0, values, bin_size):
        """Low-pass (trailing along-track window x smooth_m), vertical gradient (dB/m), then high-pass."""
        self.recent.append((time, row0, values))
        while time - self.recent[0][0] > self.along_track_s:
            self.recent.popleft()
        r0 = min(r for _, r, _ in self.recent)
        r1 = max(r + len(v) for _, r, v in self.recent)
        sums, counts = np.zeros(r1 - r0), np.zeros(r1 - r0)
        for _, r, v in self.recent:
            ok = np.isfinite(v)
            sums[r - r0:r - r0 + len(v)] += np.where(ok, v, 0.0)
            counts[r - r0:r - r0 + len(v)] += ok
        with np.errstate(invalid='ignore', divide='ignore'):
            mean = np.where(counts > 0, sums / counts, np.nan)
        # Only the rows of this column (the window can extend deeper or shallower).
        mean = box_mean(mean, round(self.smooth_m / bin_size))[row0 - r0:row0 - r0 + len(values)]
        # Central difference over gradient_m: positive where the level rises with depth.
        h = max(1, round(self.gradient_m / bin_size / 2))
        grad = np.full(len(mean), np.nan)
        grad[h:-h] = (mean[2 * h:] - mean[:-2 * h]) / (2 * h * bin_size)
        return grad - box_mean(grad, round(self.highpass_m / bin_size))

    def add_profile(self, time, row0, values, bin_size, window=None, seafloor_m=None):
        """
        Pick the thermocline bottom in one column, searching only between window = (top, bottom) depths if given.
        seafloor_m: the shallowest bottom detection in the column's band, if known.
        Returns the depth (m, same reference as the column) after the Hampel filter, or None if nothing was found.
        """
        depths = (row0 + np.arange(len(values))) * bin_size
        lo, hi = search_range(depths, values, 1.0, 1.0, seafloor_m)
        # Blank the near field and the seafloor echo so the filters don't respond to them.
        values = np.array(values, float)
        values[:max(lo, 0)] = np.nan
        values[max(hi, 0):] = np.nan
        hp = self.filtered(time, row0, values, bin_size)
        if window is not None:
            lo = max(lo, int(np.searchsorted(depths, window[0])))
            hi = min(hi, int(np.searchsorted(depths, window[1])))
        candidates = []
        if hi - lo >= 10:
            seg = hp[lo:hi]
            # Local minima of the high-passed gradient beyond the threshold.
            inner = np.r_[False, (seg[1:-1] <= seg[:-2]) & (seg[1:-1] <= seg[2:]), False]
            candidates = depths[lo + np.flatnonzero(inner & (seg <= -self.threshold))]
        pick = None
        if len(self.picks) >= MIN_TRACK_PICKS:
            median = np.median(self.picks)
            # A deeper edge seen in at least half the recent columns takes over (Cordero picks the deepest).
            self.deeper.append(bool(len(candidates)) and candidates[-1] > median + self.track_m)
            if sum(self.deeper) >= self.deeper.maxlen / 2:
                self.picks.clear()
                self.deeper.clear()
        if len(self.picks) >= MIN_TRACK_PICKS:
            # Tracking: the candidate nearest the recent picks, if close enough.
            median = np.median(self.picks)
            if len(candidates):
                nearest = candidates[np.argmin(np.abs(candidates - median))]
                if abs(nearest - median) <= self.track_m:
                    pick = float(nearest)
            if pick is None:
                self.misses += 1
                if self.misses >= self.hampel_n:  # lost: start again from the deepest candidate
                    self.picks.clear()
                return None
        elif len(candidates):
            pick = float(candidates[-1])  # acquiring: the deepest candidate (Cordero)
        if pick is None:
            return None
        self.misses = 0
        self.picks.append(pick)
        p = np.array(self.picks)
        median = np.median(p)
        sigma = 1.4826 * np.median(np.abs(p - median))
        return pick if abs(pick - median) <= self.hampel_sigma * sigma else float(median)
