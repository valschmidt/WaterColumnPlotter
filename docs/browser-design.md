# Browser-based Water Column Plotter: design

Status: prototype in progress (November 2026). The PyQt5 GUI remains in the repo until the browser version replaces it.

## Goals

- Run with fewer OS dependencies: no Qt. Python plus a web browser.
- Real-time display of Kongsberg #MWC water column data: a pie (swath cross-section) and vertical and horizontal
  slices through the water column over time.
- View from the acquisition machine or from any machine on the network, by more than one person at once.

## Architecture

One command (`wcp`) starts one Python program, which opens the default browser at
`http://localhost:8000`.

```
Sonar / SIS / KmallPlayer --UDP--> receiver process: socket reads, partition reassembly
                                        |  multiprocessing.Queue (complete datagrams)
                                        v
recorded .kmall/.kmwcd/.s7k file -> worker process: pykmall / pys7k parse -> grid -> pie and slice columns
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
  - Replay: choosing Replay (or Browse…) opens the operating system's own Open dialog on the plotter's computer
    (`Web/filedialog.py`, via `POST /choose-file`: AppleScript on macOS, PowerShell's OpenFileDialog on Windows,
    zenity or kdialog on Linux, else Tk), starting in `--data-dir`, by default the current directory or the
    `--replay FILE` file's. A page can't get the path of a file chosen in the browser's own file picker, and the
    replay reads the file from the plotter's disk, so the server shows the dialog. A browser on another computer
    (which couldn't see that dialog), or a computer with no dialog available, gets the page's own browser of the
    plotter computer's directories instead (served by `/browse`). Either way, a .kmall / .kmwcd file; after that, a menu offers the other recordings in the same directory. The file is played through the
    worker in place of real-time data, which is ignored meanwhile. With **Play folder** (on by default; part of the
    shared source state, `{'cmd': 'folder', 'on'}`), when a file ends the worker opens the next recording in its
    folder with the same extension, by name (`Source.play_next`), skipping any it can't open, and plays on at the same
    speed without clearing the display; the position slider then covers that file.
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
    A swath in which no beam has a bottom detect (the seafloor beyond the water column range, or detection failed)
    uses every recorded sample, rather than being dropped.
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
- **Teledyne RESON 7k (replay of .s7k files; `gridding.grid_7018`, `replay_s7k.py`)**: the same gridding, from each
  ping's 7018 beamformed water column (samples x beams magnitudes, every beam from sample 0 at transmit) with that
  ping's 7000 (sample rate, sound speed, projector vertical steering as the tilt), the 7004 beam geometry and the
  7027 bottom detections (beams with a magnitude or phase detection; `DetectionPoint` scaled from the 7027 sample
  rate to the 7000 one). Records arrive in the order 7000, 7004, 7027, 7018 within a ping, so the worker keeps the
  latest of each and grids when the 7018 arrives; a seek starts at a ping's 7000. Checked on SeaBat T51R data
  (775 kHz, 512 beams, 2979 samples to 64 m, 11.2 pings/s; 152 files, 2026-11-09):
  - Amplitude: `20 log10(magnitude)` dB, uncalibrated (magnitude 0 is taken as 1). The color scale sets itself, so
    the offset from Kongsberg's levels doesn't matter.
  - `y = r * sin(BeamHorizontalDirection)`: 7k steering angles are positive to starboard (as the DFD defines every
    steering angle; 7027 `RxAngle`, in the array frame, is the 7004 angle plus roll, which fits only that sign).
  - Roll is not applied: the T51R steers its receive beams for roll (7012 Flags bit 1; 7027 `AppliedRoll` is 0, which
    the DFD says means roll stabilization is on). Over 1970 pings, the across-track slope of the seafloor fitted to
    the 7027 detections is uncorrelated with roll (r = +0.04) as the 7004 angles stand, and follows it at about 1
    degree per degree when roll is added or subtracted. A sonar without roll stabilization would need roll here.
  - Heave: 1012 heave (positive up) is subtracted, from the latest 1012 before the ping (1012 arrives at about 100 Hz).
    Subtracting reduces ping-to-ping change of the nadir seafloor from 2.0 to 1.6 cm; adding raises it to 2.6 cm.
  - The median sidelobe options take the whole swath as one transmit sector; `ping_mean` works as for Kongsberg.
  - Bottom detections in the grid sit on the strongest echo: in raw 7018 data, the strongest sample in 92 to 94% of
    beams is within 10 samples of the 7027 detection (median offset -0.2 samples).
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
- **Swath buffer**: each completed swath (its datagrams summed: one per receive head) goes to the pages as its mean
  level per cell, cropped to its data and quantized to one byte per cell (`base + q * step` dB; the step is the
  swath's range over 254 levels, 0.5 dB for Kongsberg's 127 dB sample range, finer for a smaller range; 255 = no
  data). Each page keeps the last `historySwaths` swaths and builds all three plots from them, so slice positions
  and the pie's time are the viewer's own and change without asking the plotter. The plotter keeps the same history
  to send pages that connect later.
- **Pie**: average of `alongTrackAvg_swath` swaths ending at the chosen swath, or the newest (Live).
- **Vertical slice column**: per depth row, average over the viewer's across-track band (cells, weighted by
  count).
- **Horizontal slice column**: per across-track column, average over the viewer's depth band; or the
  amplitude-weighted depth in the band, where the swath covers all of it.
- **Tracking column** (layers, thermocline): on the plotter, per depth row, average over the band
  `|y| <= acrossTrackAvg_m / 2` under the vessel. It doesn't follow the viewer's vertical slice.
- Slice and tracking columns average along track in groups of `alongTrackAvg_swath` swaths, by swath number
  (`seq`, from 0 after each reset): group k is swaths k * A to (k + 1) * A - 1, so groups don't shift as data
  arrives, and tracking results line up with the slice columns. Counts throughout (along-track
  average, swath history, background swaths) are of swaths, not ping cycles: a dual-swath system transmits two swaths
  per ping cycle, pointing in different directions, under one `pingCnt`. A swath is `(pingCnt, swathAlongPosition)`;
  an odd count is allowed and splits a ping cycle. (The Qt version counted datagrams, which is the same for
  single-head systems but counts each receive head separately on dual-head ones.)

## Settings

| Kind | Settings | Scope |
| --- | --- | --- |
| Network | UDP address, port, multicast group, HTTP port | Command-line arguments |
| Processing | cell size, reference to water surface, heave allowance, swaths, sidelobe suppression, tracking width, along-track average, swath history, history memory | Server: shared by all viewers |
| Display | slice positions and widths, the pie's time (Live or a chosen swath), zoom, pan, auto-zoom, color scale, and Settings > Display (grid, colors, combine) | Browser: per viewer; slice positions are kept in the browser's storage |

Changing a processing setting applies to everyone, and every connected page shows the new value. Changing the cell size,
depth reference, transducer depth (from #IIP), along-track average, swaths or sidelobe suppression clears the
history, since earlier data was gridded or averaged differently. Moving a slice recomputes it over the whole history.

The settings form groups settings by the plot they affect (pie grid, depth, tracking, thermocline, all plots),
with explanations in tooltips. Names in the form versus the settings keys: cell size = `binSize_m`,
swath history = `historySwaths`.

Memory: a swath takes about one byte per cell it covers, about 60 kB at 0.2 m cells in 25 m of water, so the default
5000-swath history is about 300 MB in each page and in the plotter. A swath's grid grows as (depth / cell size)²,
so the history is also limited by memory:

- **History memory** (`historyMemory_MB`, default 500 MB, shared): the plotter and each page keep the newest swaths
  that fit in both the swath history and this budget; the status line adds "memory limit" when the budget is what
  limits it.
- **One swath** may cover at most 2 million cells (`gridding.MAX_CELLS`); larger ones are skipped, with a warning.
- **Receiver to worker**: at most 100 complete datagrams wait (`DG_QUEUE_MAX`); when processing can't keep up, new
  ones are dropped and counted (`behind`), and the status line warns.
- **Plotter to pages**: each page has its own send queue and task, so a slow page doesn't hold up the others. The
  history is sent to a page that connects in frames of at most 16 MB, built as they are sent. A page that stays
  behind (over 64 MB waiting for over 10 s, or over 512 MB at any time) is disconnected, with the reason shown; it
  reconnects and catches up from the history.
 At 5000 swaths, moving a slice band takes about
80 ms (amplitude) to 140 ms (depth mode) to recompute, and each update about 40 ms.

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
  and pan are free in both directions. Holding H while turning the wheel zooms horizontally only, and V vertically
  only (exaggerating depth on the pie, for instance); the pie's equal aspect returns with auto-zoom.
- Pie and vertical slice: when fitted, the top is 0 m (water surface) when referenced to the surface, otherwise the
  heave allowance above 0 m.
- Horizontal and vertical slices: with auto-zoom on, the x-axis spans the full history and the newest ping stays at
  the right edge, so new data scrolls in from the right at a constant scale.
- Draggable indicators. The horizontal slice's depth band (magenta) is drawn on the pie and the vertical slice;
  the vertical slice's across-track band (cyan) on the pie and the horizontal slice. Dragging a band's dashed edge
  widens or narrows it about its centre, its centre line moves it (a band under 16 px: inside moves it, just outside
  an edge resizes it), snapped to cell edges. The pie's time (white line, over the swaths it
  averages) is drawn on both slices; dragging it steps the pie through the history, and dragging it to the newest
  end, or the pie's Live button, follows new data again. The readout names the indicator under the mouse.
- More data than pixels: when a plot's image has more columns (or rows) than device pixels at the current zoom, N
  per pixel (rounded), they are combined before coloring, by the mean or the maximum of the values present, or by
  decimation (the value nearest each group's middle; Settings > Display > Combine, per viewer; depths by the mean when Max is
  chosen), in groups counted from grid indices so they don't
  shift as the slices scroll. The plot shows "1:N" in yellow at its bottom right, with the tooltip "1 px = N columns
  at this zoom level". Combining is redone from the full image when zooming changes N, not from the history, so
  zooming in shows full detail. Not done yet: keeping pre-combined copies at several resolutions (wait for
  feedback on whether zooming is fast enough without them).
- Cursor readout: position and value under the mouse.

## Data to the browser

- On connect, the server sends the settings, then the swaths it holds (in frames of 200), the tracking results,
  the cast and the source.
- Then once per update interval (default 1 s), the swaths completed since the last update and their tracking
  results. A swath is complete when the next one starts, or after 0.5 s without data.
- Swath frames are binary: uint32 header length, JSON header `{type: 'swaths', swaths: [{seq, t, ping, pos, row0,
  col0, rows, cols, base, step}]}`, then each swath's rows x cols bytes. Tracking results are JSON:
  `{type: 'tracks', tracks: [{seq0, seq1, t, layers, thermo}]}`.
- At 18 swaths/s and about 60 kB each, about 1.1 MB/s per viewer, and about 300 MB for a page connecting to a full
  5000-swath history.

## Open questions

- Depth reference to the water surface ignores tide and squat; it is not referenced to a datum.
- Default cell size: 0.2 m is a fixed default. It could be chosen from the data (for example from the range
  resolution and depth).

- Positions and attitude: the Qt version ignores #SKM/#SPO. Pitch could correct the along-track tilt, and positions
  could label the slices.
- Multi-head / dual-swath handling: currently each datagram is a column.
- Whether to keep a recording option (write received datagrams to a file).
