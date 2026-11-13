"""
Experimental wreck detection: the CSV export of the detections a page found in its wreck box (see detectWreck in
static/app.js), after the water column least-depth workflow of Wyllie, Weber & Armstrong (US Hydro 2015).

The depths are the plotter's: straight rays at the sound speed at the transducer, heave-corrected, below the
transducer or the water surface. They are not corrected for refraction, water level or datum. Where the sonar's
#MRZ soundings were received (Kongsberg), each detection is also given a position and a depth below the waterline,
corrected from the bottom detections of its beam (georef.py), with an uncertainty. The file's header says what was
and wasn't applied: it is a field estimate, not a charted depth.
"""

import csv
import io
import time

import numpy as np

from WaterColumnPlotter.Web import georef

COLUMNS = ('swath', 'ping', 'swath_along_position', 'time_utc', 'across_m', 'wc_depth_m', 'level_dB', 'shallowest',
           'beam_angle_deg', 'range_m',
           'lat', 'lon', 'depth_wl_m', 'u_vert_m', 'u_horiz_m',
           'u_model_v_m', 'u_beams_v_m', 'u_bottom_v_m', 'u_cell_m', 'u_model_h_m', 'u_bottom_h_m', 'u_footprint_h_m')


def utc(t, decimals=3):
    """Seconds since 1970 as an ISO 8601 UTC time."""
    whole = int(t)
    frac = f"{t - whole:.{decimals}f}"[1:] if decimals else ''
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(whole)) + frac + 'Z'


def export_csv(request, swaths, settings, source, geo=None):
    """
    The detections as CSV text with a header of '#' comment lines describing them.
    request: {'prism': {'seq', 'depth', 'across'}, 'threshold_dB', 'detections': [{'seq', 'across', 'depth', 'level'}],
    'bottomLeast': {'seq', 'across', 'depth'} or None} from the page; swaths: {seq: meta} of the swaths held (from
    worker.encode_swath); settings: the plotter's; source: what the data came from (a file name, or real-time);
    geo: {seq: georef.correction_record} for the swaths that have one.
    Returns (file name, text).
    """
    geo = geo or {}
    detections = request['detections']
    prism = request['prism']
    cell = settings['binSize_m']
    if settings['referenceToSurface']:
        reference = f"below the water surface (transducer {settings['transducerDepth_m'] or 0:.2f} m below the waterline)"
    else:
        reference = "below the transmit transducer's resting (heave-corrected) depth"
    least = min(detections, key=lambda d: (d['depth'], -d['level'])) if detections else None
    located = {}
    for i, d in enumerate(detections):
        record = geo.get(d['seq'])
        if record is not None:
            found = georef.locate(record, d['across'], d['depth'], cell)
            if found is not None:
                located[i] = found
    least_wl = min(located.values(), key=lambda p: p['depth_wl']) if located else None

    out = io.StringIO()
    note = lambda text: out.write(f"# {text}\n")
    note("Water Column Plotter: wreck detections (EXPERIMENTAL field estimate, not a charted depth)")
    note(f"source: {source}")
    note(f"exported: {utc(time.time(), 0)}")
    note(f"wc_depth_m: depth of the detected cell's centre, {reference}; straight rays at the sound speed at the "
         "transducer, heave-corrected. NOT corrected for refraction, water level (tide) or datum; positions are "
         "sonar-relative (across_m: starboard positive).")
    note(f"cell size: {cell:g} m (depths are cell centres, +/- {cell / 2:g} m)")
    note(f"detection: in each column of the box (one swath, one across-track cell), the shallowest cell with a mean "
         f"level at or above {request['threshold_dB']:g} dB (sidelobe suppression: {settings['sidelobeSuppression']})")
    note(f"box: swaths {prism['seq'][0]} to {prism['seq'][1]}, depth {prism['depth'][0]:.2f} to "
         f"{prism['depth'][1]:.2f} m, across-track {prism['across'][0]:.2f} to {prism['across'][1]:.2f} m")
    if least is not None:
        note(f"WC shallowest: {least['depth']:.2f} m (swath {least['seq']}, across {least['across']:.2f} m)")
    bottom = request.get('bottomLeast')
    if bottom:
        line = f"sonar bottom detections in the box: shallowest {bottom['depth']:.2f} m (swath {bottom['seq']}, " \
               f"across {bottom['across']:.2f} m), same frame"
        if least is not None:
            line += f"; {bottom['depth'] - least['depth']:.2f} m deeper than the WC shallowest"
        note(line)
    else:
        note("sonar bottom detections in the box: none")
    if located:
        note(f"lat, lon, depth_wl_m: corrected from the sonar's own bottom detections (#MRZ: ray traced, motion and "
             f"offsets applied by the sonar, its real-time navigation and water level) of the detection's beam; "
             f"{len(located)} of {len(detections)} detections located. depth_wl_m is below the waterline at the time: "
             "NOT corrected for tide, datum or separation model, and no post-processed navigation.")
        note("method: the mean of two corrections that bracket the truth: 'constant' (the bottom detection's whole "
             "correction, Method One of Wyllie, Weber & Armstrong 2015, per beam) and 'scaled' (on the chord from the "
             "transducer to the corrected bottom detection, at the detection's fraction of its range).")
        note("u_vert_m, u_horiz_m: combined standard uncertainty (1 sigma) of: half the two corrections' difference "
             "(u_model), half the neighbouring beams' difference (u_beams), the bottom detections' own (u_bottom, "
             "from #MRZ), the cell (cell/sqrt(12)) and, horizontally, the beam footprint (u_footprint).")
        note(f"WC shallowest corrected: {least_wl['depth_wl']:.2f} m below the waterline +/- {least_wl['u_vert']:.2f} "
             f"m (1 sigma), at {least_wl['lat']:.7f}, {least_wl['lon']:.7f} +/- {least_wl['u_horiz']:.2f} m")
    else:
        note("lat, lon, depth_wl_m: not available (no #MRZ soundings from the sonar for these swaths: Kongsberg "
             "only, from the .kmall recorded with the water column, or received over UDP); use beam_angle_deg, "
             "range_m and time_utc to place the detections in processing software.")
    note("beam_angle_deg, range_m: the detection's straight-ray angle from vertical (starboard positive) and range "
         "from the transducer, at the sound speed at the transducer")

    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(COLUMNS)
    for i in sorted(range(len(detections)), key=lambda i: (detections[i]['seq'], detections[i]['across'])):
        d = detections[i]
        meta = swaths.get(d['seq'], {})
        record = geo.get(d['seq'])
        # The straight-ray angle and range, from the transducer's place in the plotter frame (heave and depth offset).
        if record is not None:
            v = float(record['v_off'])
        else:
            v = (settings['transducerDepth_m'] or 0.0) if settings['referenceToSurface'] else 0.0
        angle, rng = np.degrees(np.arctan2(d['across'], d['depth'] - v)), np.hypot(d['across'], d['depth'] - v)
        p = located.get(i)
        row = [d['seq'], meta.get('ping', ''), meta.get('pos', ''), utc(meta['t']) if 't' in meta else '',
               f"{d['across']:.3f}", f"{d['depth']:.3f}", f"{d['level']:.2f}", int(d is least),
               f"{angle:.3f}", f"{rng:.3f}"]
        if p is None:
            row += [''] * 12
        else:
            row += [f"{p['lat']:.8f}", f"{p['lon']:.8f}", f"{p['depth_wl']:.3f}", f"{p['u_vert']:.3f}",
                    f"{p['u_horiz']:.3f}"] + [f"{p[k]:.3f}" for k in ('u_model_v', 'u_beams_v', 'u_bottom_v', 'u_cell',
                                                                   'u_model_h', 'u_bottom_h', 'u_footprint_h')]
        writer.writerow(row)
    t = swaths.get(least['seq'], {}).get('t') if least else None
    name = 'wreck_' + (time.strftime('%Y%m%d_%H%M%S', time.gmtime(t)) if t else 'detections') + '.csv'
    return name, out.getvalue()
