# Experiments (branch `experiments`)

Ideas being tried on the browser plotter. Some may not be kept.

## Prototyped

| Feature | Where | Notes |
| --- | --- | --- |
| Faint grid | Toolbar "Grid" (per viewer) | Lines at the axis ticks. |
| Colour maps | Toolbar "Colours": viridis, jet, grayscale (per viewer) | Applies to all plots and histogram colour bars. |
| Sound speed profile | Strip right of the vertical slice | Latest #SVP, on the vertical slice's depth scale (shifted by the transducer depth when depths are measured from the transducer). The orange dashed line marks the strongest sound speed gradient in view (the velocline). |
| Sidelobe suppression | Settings > Pie grid (shared) | See below. Applies before gridding, so the pie and both slices are corrected. |
| Layer tracking | Toolbar "Layers" (per viewer); Settings > Vertical slice > Layer threshold (shared) | `Web/layers.py`, see below. |

### Sidelobe suppression

Options, applied to each ping's samples before gridding (values become dB relative to the subtracted level):

- **Subtract mean / median**: at each range sample, subtract the mean or median over all beams at that range. Removes
  the seafloor echo received through sidelobes beyond the minimum slant range, and the overall level change with
  range.
- **Subtract range and beam medians**: first subtract each beam's median over ranges inside the minimum slant range
  (where every beam sees only water), then the range median. Also evens out differences between transmit sectors and
  across the beam pattern. Doing the beam step second over-corrects beyond the minimum slant range (measured
  -7.8 dB there versus -2.7 dB in this order).

Measured on a REV I EM2040 .kmwcd (dual swath, 24 m depth), mean of 10 pings, levels relative to the centre beams at
11-15 m depth:

| Option | Outer sector (14-20 m across) | Sidelobe region (16-21 m deep, 25-40 m across) | Processing |
| --- | --- | --- | --- |
| Off | +13.4 dB | +39.4 dB | 13 ms/datagram |
| Subtract mean | +8.7 dB | +0.2 dB | 14 ms |
| Subtract median | +8.6 dB | +0.9 dB | 18 ms |
| Subtract range and beam medians | -0.9 dB | -2.7 dB | 23 ms |

The median uses a sort per range row (6 ms) instead of `np.nanmedian` (21 ms).

### Layer tracking

A Python reimplementation of the approach in Roland Arsenault's
[watercolumn_layer_tracker](https://github.com/rolker/watercolumn_layer_tracker) (C++/ROS; no licence file, so the
code was not copied). Each vertical slice column is taken relative to a quadratic fit of its background against
depth; candidate slices are intervals averaging at least the threshold above background (1-25 m thick), the best
non-overlapping ones are kept, and each joins the layer whose recent extent it overlaps or is nearest to. Changes from
the original: the seafloor echo (first value 20 dB above the profile median) and the first metre below the transducer
are excluded, and layers are not split at a maximum duration. Layers seen in fewer than 10 columns are not drawn.

On the test file with sidelobe suppression off and a 1 dB threshold, 300 columns (1500 pings) gave three persistent
layers: 10.1-13.6 m (279 columns), 19.5-20.7 m (66) and 1.7-3.8 m just below the transducer (296; possibly near-field
or bubbles). The 10-14 m layer sits just below the SVP's velocline (about 8.3 m below the transducer), the relationship
Jose Cordero Ros's MS thesis (UNH, 2018, "Improved Sound Speed Control Through Remotely Detecting Strong Changes in the
Thermocline") uses to track the thermocline between casts.

## Requested, not yet prototyped

- **Long-term integration window** left of the vertical slice: much coarser along-track averaging (e.g. one column per
  minute) covering hours, to show long-term trends.
- **Longer history with a time slider**: keep more than the display history (on disk or in memory) and let viewers
  scroll back.

## Further ideas

Processing
1. **Layer-velocline comparison** (from Cordero): compare the tracked layer depth with the last cast's velocline and
   flag when they diverge by more than a threshold ("consider a new cast"), or estimate the resulting depth bias with
   a simple ray trace.
2. **Temporal background removal**: subtract a running median per (beam, range) over the last N pings, removing
   stationary artifacts (near-field ringing, interference) that sidelobe suppression doesn't.
3. **Average in the linear domain**: average intensities rather than dB values, which is the physically meaningful
   mean for echo integration (dB averages are biased low).
4. **Undo the TVG**: apply X log R + 2 alpha R + OFS + C from the datagram to get values closer to volume backscatter
   strength (Sv), so levels mean the same across settings and sonars.
5. **Height-above-seafloor slices**: a horizontal slice that follows the seafloor at a set height, for seeps and
   near-bottom plumes; overlay the bottom detections on the pie and vertical slice.
6. **Pitch correction** from #SKM for along-track tilt (a TODO in the gridding).
7. **Target / plume detection**: threshold and connected components on the pie, with a contact list.
8. **pykmall returning NumPy arrays** for sample amplitudes: converting tuples is about a third of processing time.

Display
9. **Minimum slant range** marker on the pie, with an option to blank beyond it.
10. **Georeferenced horizontal slice**: use #SPO positions to draw it as a plan view on a map.
11. **Per-viewer slice settings**: keep each ping's (cropped) grid so slices can be cut per viewer and recomputed for
    the whole history.
12. **Snapshots and export**: PNG of the plots, CSV of tracked layers.
13. **Playback from file in the server**, with speed control and pause, replacing the replay script and pairing with
    the time slider.
