# Browser-based Water Column Plotter: design

Status: prototype in progress (November 2026). The PyQt5 GUI remains in the repo until the browser version replaces it.

## Goals

- Run with fewer OS dependencies: no Qt. Python plus a web browser.
- Real-time display of Kongsberg #MWC water column data: a pie (swath cross-section) and vertical and horizontal
  slices through the water column over time.
- View from the acquisition machine or from any machine on the network, by more than one person at once.

## Architecture

One command (`water_column_plotter_web`) starts one Python program, which opens the default browser at
`http://localhost:8000`.

```
Sonar / SIS / KmallPlayer --UDP--> receiver process: socket reads, partition reassembly
                                        |  multiprocessing.Queue (complete datagrams)
                                        v
recorded .kmall/.kmwcd file ------> worker process: pykmall parse -> grid -> pie and slice columns
  (replay, read by the worker)          |  multiprocessing.Queue (results)      ^ settings, source commands
                                        v                                       |
                                   main process: aiohttp web server -----------+
                                        |  HTTP (page) + WebSocket (binary frames, JSON messages)
                                        v
                                   browser(s): three canvas panels and controls
```

- **Receiver** does nothing but read the socket and reassemble partitioned datagrams, so it keeps up with the sonar.
  It uses a large socket receive buffer (default 8 MB, where the OS allows). Datagrams are parsed with pykmall
  (`kmall.OpenBytestoRead`, `read_EMdgmHeader`, `read_EMdgmMpartition`, `read_EMdgmMbody`).
- **Worker** parses each complete #MWC with `kmall.read_EMdgmMWC`, grids it, and produces display data.
- **Source**: the page's Source bar switches between real-time and replay. The source is shared, like the settings,
  and switching clears the display.
  - Real-time: Configure… sets the UDP listen address, port and multicast group (initially `--udp-ip`, `--udp-port`,
    `--multicast`). Changing them restarts the receiver; if it can't open the socket, the error is shown in the bar and replay still
    works.
  - Replay: choosing Replay opens a dialog for browsing the plotter computer's directories (served by `/browse`,
    starting in `--data-dir`, by default the current directory or the `--replay FILE` file's) for a .kmall / .kmwcd
    file; after that, a menu offers the other recordings in the same directory. The file is played through the
    worker in place of real-time data, which is ignored meanwhile.
    The worker indexes the file with `kmall.index_file` and reads its #MWC, #SVP and #IIP datagrams itself, paced by
    their timestamps at a chosen speed (0.5x to 10x, or as fast as they can be processed), so playback waits for
    processing instead of dropping data, and no UDP partitioning is involved. Seeking clears the display and
    restarts from the chosen time, after re-applying the latest #IIP and #SVP before it.
- **Main process** runs the asyncio web server, serves the page, and relays worker results to every connected browser.
- **Dependencies**: numpy, pykmall, aiohttp. The browser side is plain HTML/JavaScript/CSS with no build step and
  no external libraries.

## Processing

Gridding follows the Qt version's geometry, with two bugs fixed.

- Pings are identified by `pingCnt`. Dual-swath systems send one #MWC datagram per swath; swaths of the same ping
  are combined.
- Each water column sample is placed in a 2D grid (depth z by across-track y) with square cells of `binSize_m`:
  - range `r = (startRangeSampleNum + i) * soundVelocity / (2 * sampleFreq)`
  - `y = r * sin(beamPointAngReVertical)`. Positive angles are to port (beam 0, the port-most beam, has the
    largest positive angle), and port is drawn on the left.
  - `z = r * cos(tiltAngleReTx) * cos(beamPointAngReVertical) + heave`
  - Samples beyond each beam's bottom detect are dropped. Beams with no bottom detect use the swath's mean detect.
  - Heave: `heave_m` is added to every sample's depth, as in the Qt version. (Checked on a REV I file: adding it
    reduces ping-to-ping jitter of the nadir seafloor from 3.0 to 1.9 cm; subtracting it raises it to 5.1 cm.)
  - Depth reference: by default depth is measured from the transducer's resting (heave-corrected) depth. With
    "Reference to water surface", the transducer's depth below the waterline is added. That depth is read from the
    #IIP installation datagram with pykmall's `read_EMdgmIIP(translate=True)`: transmit transducer z
    (`tx_transducer_1_vertical_location`, or `transducer_1_vertical_location` for a combined head) minus waterline
    z (`waterline_vertical_location`). Until an #IIP arrives the depth is unknown and the page says so; a live feed
    may not send one after the plotter starts.
  - Cells are indexed from depth 0: row `floor(z / binSize_m)`, column `floor(y / binSize_m)`. Each ping is
    gridded into a block just large enough for its samples, with the index of its first row and column, so there is
    no fixed grid size, no samples are dropped at grid edges, and deeper water or a wider swath simply extends the
    plots. The Qt version's fixed 500 x 500 grid is gone. Its "max heave" setting only offset the array index so
    heave-raised samples (negative depth) could be stored; here it is a display setting, "heave allowance": when depth
    is measured from the transducer, the pie and vertical slice start that far above 0 m so those samples stay
    visible. Referenced to the water surface, they start at 0 m.
  - Safeguard: a ping whose block would exceed 2 million cells (a cell size far too small for the swath) is skipped,
    and the page's status line says so.
- Status line: the sonar's ping rate and swaths per ping, measured from the datagram timestamps over the last 20 ping
  cycles (so a replay shows the recorded rate at any speed), then datagrams received (or processed, when replaying)
  per second, and processing time per datagram. A ping cycle sends one datagram per swath per receive head, so dual-swath
  data from one head gives two datagrams per ping cycle.
- Amplitude is `sampleAmplitude05dB_p * 0.5 - TVGoffset_dB`. Each cell holds the sum and count of the samples in
  it, so averages are sum / count.
- **Fixed:** the Qt version accumulated with `grid[idx] += amp`, which NumPy applies once per cell even when
  indices repeat. The cell therefore held a single sample, not the sum and count. We use `np.bincount`.
- **Fixed:** beams with fewer samples than the bottom-detect range were padded with NaN, and the NaNs were added
  into the grid, erasing those cells. We only use samples that exist.
- **Pie**: average of the last `alongTrackAvg_swath` swaths.
- **Vertical slice column**: per depth row, average over the across-track band `|y| <= acrossTrackAvg_m / 2`.
- **Horizontal slice column**: per across-track column, average over the depth band
  `sliceDepth_m ± sliceThickness_m / 2`.
- Slice columns are averaged along track in blocks of `alongTrackAvg_swath` swaths. Counts throughout (along-track
  average, swath history, background swaths) are of swaths, not ping cycles: a dual-swath system transmits two swaths
  per ping cycle, pointing in different directions, under one `pingCnt`. A swath is `(pingCnt, swathAlongPosition)`;
  an odd count is allowed and splits a ping cycle. (The Qt version counted datagrams, which is the same for
  single-head systems but counts each receive head separately on dual-head ones.)

## Settings

| Kind | Settings | Scope |
| --- | --- | --- |
| Network | UDP address, port, multicast group, HTTP port | Command-line arguments |
| Processing | cell size, reference to water surface, heave allowance, slice depth, slice thickness, across-track width, along-track average, swath history | Server: shared by all viewers |
| Display | zoom, pan, auto-zoom, color scale, pause | Browser: per viewer |

Changing a processing setting applies to everyone, and every connected page shows the new value. Changing the cell size,
depth reference, transducer depth (from #IIP) or the along-track average clears the history, since earlier data was gridded or averaged differently. Changing the slice settings applies to new data only.

The settings form groups settings by the plot they affect (pie grid, horizontal slice, vertical slice, all plots),
with explanations in tooltips. Names in the form versus the settings keys: cell size = `binSize_m`,
swath history = `historySwaths`.

Possible later change: keep each datagram's grid (cropped to its data, roughly 0.3 MB) so slices can be recomputed
for the whole history, or cut per viewer.

## Display (requirements from review of the Qt version)

- Fixed layout: pie upper left, horizontal slice upper right, vertical slice across the full bottom. Panels resize
  with the window and never overlap.
- Axes in metres (depth, across-track) and pings (newest = 0 at the right; each column spans the along-track
  average). The Qt version's axes were in bin
  indices, which made a 10 m slice read as about 50.
- Color scale: an "Auto levels" checkbox (on by default) keeps each panel at the 10th to 90th percentile of its data,
  smoothed so colors don't jump between updates. Setting a level by hand (histogram or min/max inputs) turns it off.
- Each panel has a histogram of its values beside a color bar, with draggable minimum and maximum lines (drag the band
  between them to move both). The histogram's value range is fixed when data first arrives, so it doesn't shift with
  each update; it widens only if a level is set outside it. Double-clicking the histogram resets its range and levels.
- Each plot has an "Auto-zoom" button in its header (on by default) that keeps it fitted to the data as it arrives;
  zooming (wheel) or panning (drag) that plot turns it off, and the button or a double-click turns it back on. Zoom
  and pan are free in both directions.
- Pie and vertical slice: when fitted, the top is 0 m (water surface) when referenced to the surface, otherwise the
  heave allowance above 0 m.
- Horizontal and vertical slices: with auto-zoom on, the x-axis spans the full history and the newest ping stays at
  the right edge, so new data scrolls in from the right at a constant scale.
- The horizontal slice's depth band and the vertical slice's across-track band are drawn on the pie.
- Controls are labelled "Slice depth" and "Slice thickness" (the Qt version's "Depth" and "Depth avg").
- Cursor readout: position and value under the mouse.

## Data to the browser

- On connect, the server sends the settings, the current pie and the slice history.
- Then once per update interval (default 1 s), it sends the new pie and only the slice columns added since the last
  update. Each browser keeps its own slice history (default 2500 pings).
- Binary WebSocket frames: a small JSON header (type, shape, geometry) followed by float32 data, NaN for empty cells.
  Each slice column covers only its data and carries the grid index of its first value.
  The pie is cropped to its data extent. At 18 datagrams/s with along-track averaging of 5, that is about 30 kB/s per
  viewer plus the pie (about 0.2 MB per update).

## Open questions

- Depth reference to the water surface ignores tide and squat; it is not referenced to a datum.
- Default cell size: 0.2 m is a fixed default. It could be chosen from the data (for example from the range
  resolution and depth).

- Positions and attitude: the Qt version ignores #SKM/#SPO. Pitch could correct the along-track tilt, and positions
  could label the slices.
- Multi-head / dual-swath handling: currently each datagram is a column.
- Whether to keep a recording option (write received datagrams to a file).
