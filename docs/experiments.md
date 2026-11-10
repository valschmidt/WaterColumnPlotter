# Experiments (branch `experiments`)

Ideas being tried on the browser plotter. Some may not be kept.

## Prototyped

| Feature | Where | Notes |
| --- | --- | --- |
| Faint grid | Settings > Display > Grid (per viewer) | Lines at the axis ticks. |
| Color maps | Settings > Display > Colors: viridis, jet, grayscale (per viewer) | Applies to all plots and histogram color bars. |
| Sound speed profile | Strip right of the vertical slice | Latest #SVP, on the vertical slice's depth scale (shifted by the transducer depth when depths are measured from the transducer), with grid lines when the grid is on. The orange dashed line marks the strongest sound speed gradient in view (the velocline); the shaded band is the cast's thermocline, its bottom dashed. |
| Swath buffer, draggable slices, pie time | Drag the bands on the plots (edges resize, centre lines move); drag the white pie time line on either slice; pie header: Live (per viewer) | Pages keep every swath (up to the swath history) and build the pie and slices from them, so moving a slice recomputes its whole history at once and the pie can show any held swath. See browser-design.md. Tracking still runs on the plotter, under the vessel. |
| Dual-swath selection | Settings > Pie grid > Swaths: Both / First only / Second only (shared) | Swaths not chosen (by #MWC `swathAlongPosition`, 0 or 1) are skipped before gridding, so the pie and both slices use only the chosen one; along-track averages then count only chosen swaths. Single-swath systems ignore it. The status line shows e.g. "× 2 swaths (first shown)". Changing it clears the display. |
| Sidelobe suppression | Settings > Pie grid (shared) | See below. Applies before gridding, so the pie and both slices are corrected. |
| Layer tracking | Toolbar "Tracking: Layers" (per viewer); Settings > Tracking > Layer threshold (shared) | `Web/layers.py`, see below. Any number of layers. |
| Data source: real-time or replay | Source bar under the toolbar: Configure… sets the UDP address; Replay opens the OS Open dialog (the page's own browser for remote viewers); while a file is being indexed, the source badge and the plots say "Opening <file>…" with the seconds so far; `--replay FILE`, `--data-dir DIR` (shared) | `Web/replay.py` plays a recorded file (play/pause, speed, seek). See browser-design.md. |
| Horizontal slice depth | Horizontal slice header: Amplitude / Depth / Depth, shaded (per viewer) | See below. |
| Thermocline tracking and cast alert | Toolbar "Tracking: Thermocline" (per viewer); Settings > Thermocline (shared) | `Web/thermocline.py`, after Cordero Ros (2018), see below. One feature: the thermocline bottom. |

Both trackers run on the server; the Tracking selector chooses which is drawn. They answer different questions:
layer tracking follows every scattering layer (it found 3-8 persistent ones in the test file), while thermocline
tracking follows the one edge expected to mark the bottom of the thermocline, tied to the last cast for the alert.

### Sidelobe suppression

Options, applied to each swath's samples before gridding (values become dB relative to the subtracted level). The
median options use Kongsberg's transmit sectors (a 7k ping is taken as one sector) and are in `Web/kongsberg.py`, since only multisector sonars need
them; the medians themselves are in `Web/medians.py`.

- **Subtract median per sector**: at each range sample, subtract the median over each transmit sector's beams at
  that range. Removes the seafloor echo received through sidelobes beyond the minimum slant range, the level change
  with range, and level differences between sectors.
- **Subtract range and beam medians**: first subtract each beam's median, taken separately inside the minimum slant
  range (water only) and beyond it (water plus the seafloor echo received through the sidelobes, far louder), then
  the range median per sector. Also evens out the beam pattern within sectors, which the median per sector leaves as
  bright stripes at the centre sector's edges (below). The beam step is split at the minimum slant range because
  beyond it a beam's level has nothing to do with its level in the water.
- **Subtract range median and beam noise floor fit** (`median_range_poly_beam`, 2026-11-10): subtract the range
  median per sector, then each beam's noise floor: a quadratic in range fitted, beam by beam, to what is left (down to
  the bottom detection, including the sidelobe region) by weighted least squares with weights 1 / linear power,
  1 / (10^(dB/10) + 1e-12), so the fit follows the quietest samples: one 30 dB above the floor weighs 1/1000 as
  much. Values become dB above each beam's floor rather than relative to a median. (`medians.per_beam_floor_fit`.)
  - Synthetic check (64 beams, known quadratic floors, +-3 dB noise, 10% of samples 20-50 dB echoes): the fit lands
    0.66 dB below the true floor (5-95%: 0.54 to 0.78 dB below); an unweighted fit is pulled 3.5 dB above it.
  - Speed: about 30 ms for 870,000 samples (a T51R ping to the bottom), using `np.add.reduceat` over each beam's
    contiguous samples (`np.bincount` took 54 ms, the first version 85 ms). Per ping through the worker: 43 ms on
    the EM 2040 file, 53 ms on T51R (11.2 pings/s), about the same as range and beam medians.
  - First look (one ping each): on T51R, the water inside the minimum slant range is darker and more even than with
    range and beam medians, and the bright wedge under nadir that those leave is gone; beyond the minimum slant
    range it stays several dB above the water inside. On the EM 2040 file, beam-to-beam differences between fits
    show as radial stripes.
- **Subtract each beam's mean of recent pings**: from each beam's sample at each range, subtract that beam's mean at
  that range over the previous N swaths in the same position (Settings > Background swaths, default 10), kept per
  receive fan. Removes whatever is the same from ping to ping in a beam, so only changes remain (moving targets,
  plumes, bubbles). The first ping after a reset has no background and is not shown.

Tried and dropped: subtracting the mean (suppressed about as well as the median but is pulled by strong targets);
subtracting the median over all beams, not per sector (left the outer sectors 7.8 dB above the centre); range and
beam medians over all beams, with the beam step inside the minimum slant range only (superseded by the version above);
the median per sector only beyond the first bottom return, adding back each sector's level there.

Measured on a REV I EM2040 .kmwcd (dual swath, 24 m depth), mean of 10 pings, levels relative to the centre beams at
11-15 m depth, gridding time per datagram:

| Option | Outer sector (14-20 m across, 11-15 m deep) | Sidelobe region (16-21 m deep, 25-40 m across) | Gridding |
| --- | --- | --- | --- |
| Off | +14.9 dB | +41.4 dB | 15 ms |
| Subtract median (dropped) | +7.8 dB | +0.3 dB | 20 ms |
| Subtract median per sector | +0.3 dB | -1.1 dB | 24 ms |
| Range and beam medians over all beams (dropped) | -1.2 dB | -2.9 dB | 26 ms |
| Subtract range and beam medians (per sector; measured before the beam step was split) | -0.8 dB | -2.2 dB | 29 ms |
| Subtract each beam's mean of recent pings (10) | -0.2 dB | -0.2 dB | 21 ms |

Measured over 20 datagrams after 40 (so the last option has 10 pings of history): the layer at 11-14 m stands 7.2 dB
above the water at 3-6 m with suppression off, -1.6 dB with the median per sector, -0.8 dB with range and beam medians over all beams (-1.1 dB per sector)
and +0.1 dB with each beam's mean of recent pings. That last removes the sidelobe echo and the sector differences most
completely, but scattering layers too, since they barely change over 10 ping cycles (1.1 s); the vertical slice is left as
noise and the trackers have nothing to follow. The minimum slant range arc remains, as heave moves it between pings.

Centre sector edge stripes: in this file the centre sector spans +-47.6 degrees and the outer sectors start at
+-45.7 degrees (they overlap), at different frequencies (centre 320/350 kHz, outer 260/290 kHz for the two swaths).
Inside the minimum slant range the centre sector's level rises about 5 dB from nadir to its edges (receive beams
widening with steering explain about 1.7 dB of that at 47 degrees), while the outer sectors are flat to within 1 dB.
A range median per sector removes one level per range, so the centre sector's edge beams stay about 4 dB above its
median and show as bright stripes along the horizontal slice. Measured as the pie level at the edge (+-1 m about
depth x tan 46.5 degrees) relative to the water either side, at 8 / 10 / 14 m depth:

| Option | Edge stripe |
| --- | --- |
| Off | +0.2 / -0.3 / -0.2 dB |
| Subtract median per sector | +0.9 / +1.4 / +1.2 dB |
| Range and beam medians over all beams (dropped) | -0.5 / -0.6 / -0.2 dB |
| Subtract range and beam medians (per sector, beam step before the split) | -0.3 / +0.1 / -0.0 dB |

Centre sector beyond the minimum slant range (here 23.8 m, seafloor 23.7 m, so a thin wedge above the seafloor at
15-43 degrees), level re the centre water at 11-15 m and the spread (standard deviation) of its mean across 2 degree
angle bins, over 60 datagrams:

| Option | Centre sector beyond the ring | Just inside the ring | Outer sectors beyond the ring | Spread across angle |
| --- | --- | --- | --- | --- |
| Off | +41.4 dB | +3.4 dB | +41.5 dB | 1.8 dB |
| Subtract median per sector | -0.8 dB | +0.5 dB | -0.5 dB | 0.6 dB |
| Range and beam medians over all beams (dropped) | +1.6 dB | -1.2 dB | -3.0 dB | 0.6 dB |
| Range and beam medians per sector, beam step inside the ring only | -1.5 dB | -0.6 dB | -1.3 dB | 0.9 dB |
| Subtract range and beam medians (per sector, beam step split at the ring) | -1.2 dB | -0.6 dB | -1.3 dB | 0.4 dB |

The median uses a sort per range row (6 ms) instead of `np.nanmedian` (21 ms). Suppression also weakens horizontal
layers, which are similar across beams at a given range: the thermocline edge below fell from about 0.35 to 0.2
dB/m with range and beam medians (the earlier all-beam version), so the thermocline pick threshold was set to work with both.

### Horizontal slice depth

The horizontal slice's header selects Amplitude (the mean level in the band) or Depth: for each across-track cell of
each slice column, the amplitude-weighted mean depth of the signal in the band, sum(a z) / sum(a) over the band's
grid rows and the column's swaths, where a is a cell's linear amplitude (10^(dB/20) of its mean level) and z its
centre depth (`band_weighted_depth` in `worker.py`). It is sent with every column, so switching needs nothing from
the server.

"Depth, shaded" adds hillshading in the page (light from the upper left at 45 degrees, 35 % ambient), taking cells as
square: along track, the real spacing depends on the vessel's speed. Depths use the color map reversed, so shallow is
the high end (yellow in viridis) and deep the low end. With 500 columns of 500 cells, building and shading the image
takes about 35 ms per update in the page; the worker's cost is about 0.05 ms per datagram.

Only cells where the swath covers the whole band are used: those with data at or above the band's top row and at or
below its bottom row in that swath. Elsewhere the fan's edge (or the seafloor) leaves only part of the band with
data, which biased the depth toward the covered part by up to half the band's thickness. On the test file a 9-11 m band
is limited to about +-19.5 m across, 9 m x tan 64.8 degrees (the outermost beam), while the amplitude mode covers
+-49 m. Where the band reaches the seafloor, samples run down to the bottom detection, so those cells count as covered
and their depths follow the seafloor echo.

On the test file (suppression off, band 9-11 m) the weighted depth stays within about 0.1 m of the band's centre
(10th-90th percentile 9.92-10.08 m): a 3 dB contrast is only a factor 1.4 in amplitude, so the weighting moves the mean
little unless a target or layer stands well above the rest of the band. In a 10-15 m band over the layer at 11-14 m:
12.1-13.1 m.

Also tried and dropped: the depth of the strongest cell in the band (in the column's along-track average), which
spanned the band (9.1-10.9 m in the 9-11 m band) but stepped by the cell size and was mostly noise.

In the depth modes the color scale's range is the band itself (the only depths possible), and levels are kept
inside it. Moving or resizing the band resets the scale to the new band, and recomputes the whole history.

### Layer tracking

A Python reimplementation of the approach in Roland Arsenault's
[watercolumn_layer_tracker](https://github.com/rolker/watercolumn_layer_tracker) (C++/ROS; no licence file, so the
code was not copied). Each vertical slice column is taken relative to a quadratic fit of its background against
depth; candidate slices are intervals averaging at least the threshold above background (1-25 m thick), the best
non-overlapping ones are kept, and each joins the layer whose recent extent it overlaps or is nearest to. Changes from
the original: the seafloor echo (first value 20 dB above the profile median) and the first metre below the transducer
are excluded, and layers are not split at a maximum duration. Layers seen in fewer than 10 columns are not drawn.

On the test file with sidelobe suppression off and a 1 dB threshold, 300 columns (1500 ping cycles, 3000 swaths) gave three persistent
layers: 10.1-13.6 m (279 columns), 19.5-20.7 m (66) and 1.7-3.8 m just below the transducer (296; possibly near-field
or bubbles). The 10-14 m layer sits just below the SVP's velocline (about 8.3 m below the transducer), the relationship
Jose Cordero Ros's MS thesis (UNH, 2018, "Improved Sound Speed Control Through Remotely Detecting Strong Changes in the
Thermocline") uses to track the thermocline between casts.

### Thermocline tracking and cast alert

Jose Cordero Ros's MS thesis (UNH, 2018) tracked the bottom of the thermocline in EK60 echograms of the Celtic Sea,
where a scattering layer mimics it, and flagged when its movement since the last cast would push the outer-beam depth
error past a limit. The prototype follows his method on the vertical slice:

1. Thermocline in each cast: from the shallowest to the deepest depth whose sound speed gradient is at least half the
   maximum (his definition). Shaded in the SVP strip, its bottom dashed.
2. Filters on the vertical slice, with his best sizes: a low-pass (he used 310 m along track; here a trailing window
   in seconds, default 60 s, x 1 m vertically), a vertical gradient over 2 m, and a 6 m high-pass of the gradient.
3. Pick: the deepest drop in level with depth sharper than the threshold (the bottom of the scattering layer), within
   a window around the cast's thermocline bottom (he searched 15-60 m, where the thermocline was expected), then a
   Hampel filter over 25 picks at 2 standard deviations.
4. Error: the cast profile's thermocline is shifted by the pick's movement (he found the thermocline's thickness
   changed less than 5 % per hour) and both profiles are ray traced at 65 degrees to the seafloor in the column. Picks
   are green within the alert error (0.5 m, his limit: about half the IHO Order 1 allowance) and red beyond it.

Changes from the thesis, from testing on the .kmwcd:

- **Seafloor**: the seafloor echo is excluded using the sonar's bottom detections (the shallowest one in the vertical
  slice's band); with sidelobe suppression it stands only about 12 dB above the profile, too little to find from
  levels. The seafloor depth also sets the ray trace's end. (The layer tracker uses it too.)

- **Tracking**: once five picks are established, each pick is the candidate nearest their median (within 3 m). The
  deepest candidate is picked again after 25 columns without a pick, or when an edge more than 3 m deeper appears in
  at least half of the last 25 columns. With "deepest" alone, the base of the near-surface layer (5 m below the
  transducer, inside the search window) took over whenever the thermocline's edge weakened; with tracking alone, it
  could lock onto that layer for good.
- **Reference**: in this file the picked layer bottom (14.4 m below the transducer, 16.4 m below the surface) is
  about 4 m below the cast's thermocline bottom (12.5 m below the surface by the half-maximum rule; the profile
  becomes isovelocity at about 13.5 m). Comparing absolute depths, as the thesis did, reports that offset as movement.
  So when a cast arrives within 10 minutes of being taken (the real-time case), movement is measured from the median
  of the first 10 picks after it; an older cast (this file's was taken 8.5 hours before the data) falls back to the
  absolute comparison.

Results on the test file (24 m water, cast 1533 m/s at the surface to 1496 m/s below 13.5 m, 1138 columns of 5 ping cycles (10 swaths; measured before counts became swaths),
threshold 0.2 dB/m, search +-6 m):

| Run | Picks | Pick depth below transducer, median (p10-p90) | Error at 65 degrees (p10 / p50 / p90) |
| --- | --- | --- | --- |
| Suppression off, reference the cast's thermocline bottom (as recorded) | 97 % of columns | 14.4 m (12.8-14.6) | +0.22 / +0.30 / +0.36 m |
| Suppression off, reference the first picks after the cast (cast time set to the data start) | 97 % | 14.4 m (12.8-14.6) | -0.04 / +0.04 / +0.10 m |
| Range and beam medians (earlier all-beam version), reference the cast's thermocline bottom | 100 % | 14.2 m (9.6-14.6) | +0.03 / +0.29 / +0.35 m |

As a check of the ray tracing, a canonical profile like the thesis's (1511 to 1493.5 m/s between 30 and 42 m) gives
0.04, 0.12, 0.24 and 0.40 m errors at 65 degrees in 80 m of water for 1, 3, 6 and 10 m shifts, the same scale as his
finding that the 0.5 m limit was exceeded beyond about 6 m (in deeper water).

Open questions: he found the method reliable by day (79 % within 3 m) but poor at night and twilight, when migrating
plankton form other layers; a single-frequency multibeam has no channel agreement to fall back on. Heave is in the
picked depths. Depths are from the sonar's own sound speed, not ray traced.

## Requested, not yet prototyped

- **Long-term integration window** left of the vertical slice: much coarser along-track averaging (e.g. one column per
  minute) covering hours, to show long-term trends.
- **Longer history with a time slider**: keep more than the display history (on disk or in memory) and let viewers
  scroll back.

## Further ideas

Processing
1. **Synthetic profile** (Cordero's suggested extension of the cast alert above): publish the shifted cast as a
   sound speed profile the acquisition or processing software can use until the next cast.
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
