"""
Experimental: coordinates for wreck detections (see wreck.py), from Kongsberg #MRZ soundings.

#MWC water column data has no position, heading or ray tracing: the plotter places each sample on a straight ray
from the transducer at the sound speed there (gridding.grid_mwc), its "plotter frame" (across-track, depth). #MRZ
gives, for the bottom detection of each beam, its position corrected by the sonar (ray traced with the cast, motion
and offsets applied) relative to the vessel reference point (x forward, y starboard, z down), with the ping's
position, heading and water level, and the bottom detection's range in water column samples (WCrange_samples) on
water column beam WCBeamNumb.

So for each beam the plotter knows where it puts the bottom detection, and #MRZ where it is. A detection higher up the
beam is corrected by one of two models that bracket the truth:
- constant: the bottom detection's whole correction applies to it, as if all the error (refraction, offsets) arose
  above it: Method One of Wyllie, Weber & Armstrong (US Hydro 2015), for each beam;
- scaled: the correction grows in proportion to range, as if the error arose evenly along the ray: the true position
  is on the straight line (chord) from the transducer to the corrected bottom detection, at the same fraction of its
  length as the detection's straight-ray range is of the bottom detection's.
The position given is their mean, with half their difference as an uncertainty, combined with the others (see
locate). Not applied: water level (tide), datum or separation model, post-processed navigation. A field estimate.
"""

import numpy as np

# A #MRZ sounding is paired with a water column beam when its range in water column samples is within this fraction
# of that beam's own bottom detection range (soundings are numbered against water column beams, but there are more
# soundings than water column beams, and the numbering's base isn't documented: beam WCBeamNumb or WCBeamNumb - 1).
PAIR_TOLERANCE = 0.02
REJECTED = 2  # #MRZ detectionType: rejected


def beam_geometry(dg, depth_offset):
    """
    The plotter frame of a #MWC datagram (pykmall's read_EMdgmMWC), as gridding.grid_mwc places samples: each beam's
    angle from vertical and transmit tilt (rad), the transmit beam's along-track half-width (rad), metres per sample,
    the vertical offset (heave plus depth_offset, m) and the bottom detection ranges (samples, 0: none).
    """
    beam, rx = dg['beamData'], dg['rxInfo']
    sector = np.asarray(beam['beamTxSectorNum'])
    return {
        'angle': np.radians(np.asarray(beam['beamPointAngReVertical_deg'], dtype=float)),
        'tilt': np.radians(np.asarray(dg['sectorData']['tiltAngleReTx_deg'], dtype=float))[sector],
        'half_width': np.radians(np.asarray(dg['sectorData']['txBeamWidthAlong_deg'], dtype=float))[sector] / 2,
        'per_sample': rx['soundVelocity_mPerSec'] / (2.0 * rx['sampleFreq_Hz']),
        'v_off': dg['txInfo']['heave_m'] + depth_offset,
        'det': np.asarray(beam['detectedRangeInSamples'], dtype=np.int64),
    }


def plotter_position(geom, beam, samples):
    """(across-track, depth) of range sample(s) on beam(s), as gridding.grid_mwc places them."""
    r = np.asarray(samples, dtype=float) * geom['per_sample']
    angle = geom['angle'][beam]
    return -r * np.sin(angle), r * np.cos(geom['tilt'][beam]) * np.cos(angle) + geom['v_off']


def mrz_summary(dg):
    """What a correction record needs from a #MRZ datagram (pykmall's read_EMdgmMRZ)."""
    ping, s = dg['pingInfo'], dg['sounding']
    keep = ('WCBeamNumb', 'WCrange_samples', 'x_reRefPoint_m', 'y_reRefPoint_m', 'z_reRefPoint_m',
            'detectionUncertaintyVer_m', 'detectionUncertaintyHor_m', 'detectionType')
    return {'lat': ping['latitude_deg'], 'lon': ping['longitude_deg'], 'heading': ping['headingVessel_deg'],
            'z_wl': ping['z_waterLevelReRefPoint_m'], 'tx_depth': ping['txTransducerDepth_m'],
            **{k: np.asarray(s[k]) for k in keep}}


def correction_record(geoms, mrzs, transducer=None):
    """
    A swath's corrections: geoms and mrzs, {receive head index: beam_geometry} and {receive head: mrz_summary} of its
    datagrams; transducer: the transmit transducer's (x, y, z) from the vessel reference point (#IIP), or None to use
    (0, 0, its depth less the water level). Each water column beam with a bottom detection is paired with a #MRZ
    sounding (see PAIR_TOLERANCE). Returns None if no beam could be paired, else a dict of the ping's position, heading
    (deg), water level and transducer position, the plotter frame's vertical offset, and float32 arrays per paired
    beam, sorted by angle in the plotter frame (phi, rad, starboard positive): the bottom detection's plotter range R
    and position (a, d), its #MRZ position (x, y, z) and uncertainties (uv, uh), and the transmit half-width (hw, rad).
    """
    beams = []
    first = None
    for head, geom in geoms.items():
        mrz = mrzs.get(head)
        if mrz is None:
            continue
        first = first or (geom, mrz)
        det = geom['det']
        best = {}  # beam -> (range difference, sounding)
        usable = mrz['detectionType'] != REJECTED
        for i in np.flatnonzero(usable):
            for j in (mrz['WCBeamNumb'][i], mrz['WCBeamNumb'][i] - 1):
                if 0 <= j < len(det) and det[j] > 0:
                    diff = abs(int(mrz['WCrange_samples'][i]) - int(det[j]))
                    if diff <= PAIR_TOLERANCE * det[j] and (j not in best or diff < best[j][0]):
                        best[j] = (diff, i)
        if not best:
            continue
        j = np.array(sorted(best))
        i = np.array([best[k][1] for k in j])
        a, d = plotter_position(geom, j, mrz['WCrange_samples'][i])
        beams.append({'a': a, 'd': d, 'v_off': np.full(len(j), geom['v_off']), 'hw': geom['half_width'][j],
                      'x': mrz['x_reRefPoint_m'][i], 'y': mrz['y_reRefPoint_m'][i], 'z': mrz['z_reRefPoint_m'][i],
                      'uv': mrz['detectionUncertaintyVer_m'][i], 'uh': mrz['detectionUncertaintyHor_m'][i]})
    if not beams:
        return None
    geom, mrz = first
    cat = {k: np.concatenate([b[k] for b in beams]).astype(float) for k in beams[0]}
    phi = np.arctan2(cat['a'], cat['d'] - cat['v_off'])
    order = np.argsort(phi)
    if transducer is None:
        transducer = (0.0, 0.0, mrz['tx_depth'] + mrz['z_wl'])
    record = {'lat': float(mrz['lat']), 'lon': float(mrz['lon']), 'heading': float(mrz['heading']),
              'z_wl': float(mrz['z_wl']), 'T': tuple(float(v) for v in transducer), 'v_off': float(geom['v_off']),
              'phi': phi[order], 'R': np.hypot(cat['a'], cat['d'] - cat['v_off'])[order]}
    for k in ('a', 'd', 'x', 'y', 'z', 'uv', 'uh', 'hw'):
        record[k] = cat[k][order]
    for k, v in record.items():
        if isinstance(v, np.ndarray):
            record[k] = v.astype(np.float32)
    return record


def record_bytes(record):
    return sum(v.nbytes for v in record.values() if isinstance(v, np.ndarray))


def metres_per_degree(lat_deg):
    """WGS84 metres per degree of latitude and of longitude at a latitude."""
    p = np.radians(lat_deg)
    return (111132.92 - 559.82 * np.cos(2 * p) + 1.175 * np.cos(4 * p),
            111412.84 * np.cos(p) - 93.5 * np.cos(3 * p))


def locate(record, across, depth, cell):
    """
    Corrected position of a detection at (across, depth) in the plotter frame (m), found in cells of size cell (m).
    Returns None if it lies outside the paired beams' angles, else a dict:
    lat, lon (deg), depth_wl (m below the waterline at the time, from the sonar's water level), u_vert and u_horiz
    (combined standard uncertainties, m) with their components: u_model_v / u_model_h (half the difference of the
    two models), u_beams_v (half the difference of the neighbouring beams' corrections), u_bottom_v / u_bottom_h (the
    bottom detections' own, from #MRZ), u_cell (cell / sqrt(12)), u_footprint_h (the beam's footprint at the
    detection's range, along-track and between beams, as uniform); and the detection's straight-ray beam angle
    (deg, starboard positive) and range (m).
    """
    v = record['v_off']
    phi = np.arctan2(across, depth - v)
    r = float(np.hypot(across, depth - v))
    p = record['phi']
    if len(p) < 2 or not p[0] <= phi <= p[-1]:
        return None
    k = int(np.clip(np.searchsorted(p, phi), 1, len(p) - 1))
    w = float((phi - p[k - 1]) / (p[k] - p[k - 1])) if p[k] > p[k - 1] else 0.0
    at = lambda name: float(record[name][k - 1] * (1 - w) + record[name][k] * w)
    S = np.array([at('x'), at('y'), at('z')])
    T = np.array(record['T'])
    a_b, d_b, R_b = at('a'), at('d'), at('R')

    # Scaled: on the chord from the transducer to the corrected bottom detection.
    scaled = T + (r / R_b) * (S - T)
    # Constant: back from the corrected bottom detection by the straight-ray offset between them: vertically by the
    # depth difference, horizontally (towards the transducer) by the across-track difference.
    toward = S[:2] - T[:2]
    unit = toward / np.linalg.norm(toward) if np.linalg.norm(toward) > 0 else np.zeros(2)
    constant = np.array([*(S[:2] - unit * abs(a_b - across)), S[2] - (d_b - depth)])
    P = (scaled + constant) / 2
    u_model_v = abs(scaled[2] - constant[2]) / 2
    u_model_h = float(np.linalg.norm(scaled[:2] - constant[:2])) / 2

    # The two neighbouring beams' constant corrections (corrected depth less plotter depth) should agree.
    c0 = record['z'][k - 1] - record['d'][k - 1]
    c1 = record['z'][k] - record['d'][k]
    u_beams_v = abs(float(c1 - c0)) / 2
    u_bottom_v, u_bottom_h = at('uv'), at('uh')
    u_cell = cell / np.sqrt(12)
    # The footprint: the target could be anywhere across the transmit beam along track, and between beams across.
    along = r * np.tan(at('hw'))
    between = r * float(p[k] - p[k - 1]) / 2
    u_footprint_h = float(np.hypot(along, between)) / np.sqrt(3)
    u_vert = float(np.sqrt(u_model_v ** 2 + u_beams_v ** 2 + u_bottom_v ** 2 + u_cell ** 2))
    u_horiz = float(np.sqrt(u_model_h ** 2 + u_bottom_h ** 2 + u_cell ** 2 + u_footprint_h ** 2))

    # Vessel frame (x forward, y starboard) to north and east by the heading, from the ping's position.
    h = np.radians(record['heading'])
    north = P[0] * np.cos(h) - P[1] * np.sin(h)
    east = P[0] * np.sin(h) + P[1] * np.cos(h)
    m_lat, m_lon = metres_per_degree(record['lat'])
    return {'lat': record['lat'] + north / m_lat, 'lon': record['lon'] + east / m_lon,
            'depth_wl': float(P[2] - record['z_wl']), 'u_vert': u_vert, 'u_horiz': u_horiz,
            'u_model_v': float(u_model_v), 'u_beams_v': u_beams_v, 'u_bottom_v': u_bottom_v, 'u_cell': float(u_cell),
            'u_model_h': u_model_h, 'u_bottom_h': u_bottom_h, 'u_footprint_h': u_footprint_h,
            'beam_angle_deg': float(np.degrees(phi)), 'range_m': r}
