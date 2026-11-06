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
                                   worker process: pykmall parse -> grid -> pie and slice columns
                                        |  multiprocessing.Queue (results)      ^ settings
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
- **Main process** runs the asyncio web server, serves the page, and relays worker results to every connected browser.
- **Dependencies**: numpy, pykmall, aiohttp. The browser side is plain HTML/JavaScript/CSS with no build step and
  no external libraries.

## Processing

Gridding follows the Qt version's geometry, with two bugs fixed.

- Each #MWC datagram is one column in the slices. (For dual-swath systems that is two columns per ping.)
- Each water column sample is placed in a 2D grid (depth z by across-track y) with square cells of `binSize_m`:
  - range `r = (startRangeSampleNum + i) * soundVelocity / (2 * sampleFreq)`
  - `y = r * sin(beamPointAngReVertical)`. Positive angles are to port (beam 0, the port-most beam, has the
    largest positive angle), and port is drawn on the left.
  - `z = r * cos(tiltAngleReTx) * cos(beamPointAngReVertical) + heave`
  - Samples beyond each beam's bottom detect are dropped. Beams with no bottom detect use the swath's mean detect.
  - The grid's top edge is `-maxHeave_m`, so z = 0 is row `maxHeave_m / binSize_m`.
- Amplitude is `sampleAmplitude05dB_p * 0.5 - TVGoffset_dB`. Each cell holds the sum and count of the samples in
  it, so averages are sum / count.
- **Fixed:** the Qt version accumulated with `grid[idx] += amp`, which NumPy applies once per cell even when
  indices repeat. The cell therefore held a single sample, not the sum and count. We use `np.bincount`.
- **Fixed:** beams with fewer samples than the bottom-detect range were padded with NaN, and the NaNs were added
  into the grid, erasing those cells. We only use samples that exist.
- **Pie**: average of the last `alongTrackAvg_ping` gridded datagrams.
- **Vertical slice column**: per depth row, average over the across-track band `|y| <= acrossTrackAvg_m / 2`.
- **Horizontal slice column**: per across-track column, average over the depth band
  `sliceDepth_m ± sliceThickness_m / 2`.
- Slice columns are averaged along track in blocks of `alongTrackAvg_ping` datagrams, as in the Qt version.

## Settings

| Kind | Settings | Scope |
| --- | --- | --- |
| Network | UDP address, port, multicast group, HTTP port | Command-line arguments |
| Processing | bin size, max heave, slice depth, slice thickness, across-track width, along-track average | Server: shared by all viewers |
| Display | zoom, pan, auto-zoom, colour scale, pause | Browser: per viewer |

Changing a processing setting applies to everyone, and every connected page shows the new value. Changing bin size or
max heave clears the history, since earlier data was gridded differently. Changing the slice settings applies to new
data only.

Possible later change: keep each datagram's grid (cropped to its data, roughly 0.3 MB) so slices can be recomputed
for the whole history, or cut per viewer.

## Display (requirements from review of the Qt version)

- Fixed layout: pie upper left, horizontal slice upper right, vertical slice across the full bottom. Panels resize
  with the window and never overlap.
- Axes in metres (depth, across-track) and pings (newest = 0 at the right). The Qt version's axes were in bin
  indices, which made a 10 m slice read as about 50.
- Colour scale defaults to the 10th to 90th percentile of valid values. An "Auto levels" button recomputes it, and
  each panel has min/max inputs.
- Plots auto-zoom to the data extent by default. An "Auto-zoom" checkbox keeps them fitted as data arrives; manual
  zoom (wheel) or pan (drag) turns it off.
- Pie and vertical slice: 0 m depth stays at the top at every zoom level.
- Horizontal and vertical slices: with auto-zoom on, the newest ping stays at the right edge.
- The horizontal slice's depth band and the vertical slice's across-track band are drawn on the pie.
- Controls are labelled "Slice depth" and "Slice thickness" (the Qt version's "Depth" and "Depth avg").
- Cursor readout: position and value under the mouse.

## Data to the browser

- On connect, the server sends the settings, the current pie and the slice history.
- Then once per update interval (default 1 s), it sends the new pie and only the slice columns added since the last
  update. Each browser keeps its own slice history (default 1000 columns).
- Binary WebSocket frames: a small JSON header (type, shape, geometry) followed by float32 data, NaN for empty cells.
  The pie is cropped to its data extent. At 18 datagrams/s with along-track averaging of 5, that is about 30 kB/s per
  viewer plus the pie (about 0.2 MB per update).

## Open questions

- Positions and attitude: the Qt version ignores #SKM/#SPO. Pitch could correct the along-track tilt, and positions
  could label the slices.
- Multi-head / dual-swath handling: currently each datagram is a column.
- Whether to keep a recording option (write received datagrams to a file).
