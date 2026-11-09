# Changelog

What has changed in Water Column Plotter, newest first, written for the people who use it. Every change is also in
the git history, with more technical detail.

"Unreleased" lists changes not yet on GitHub's `master` branch. The browser version (the `wcp` command) is still a
prototype, so changes are listed by date rather than version number.

## Unreleased

Nothing yet.

## 2026-11-09

### Added
- **Choose the first swath, the second, or both** (Settings > Pie grid > Swaths) on dual-swath systems. The other
  swath is left out of the pie, both slices and the averages. First and second are in time within each ping cycle.
  The status line shows which is in use, e.g. "9.2 pings/s × 2 swaths (first shown)". Single-swath systems ignore
  the setting.
- **"Opening *file*…" while a recording is being prepared.** Before replaying, the plotter reads through the whole
  file to index it, which can take several seconds for a large file or one on a slow or network drive. The source
  badge and all three plots now say so, with the seconds so far, instead of appearing to do nothing.
- **The computer's own Open dialog** (Finder on macOS, Explorer on Windows, the desktop's dialog on Linux) for
  choosing a recording to replay. People viewing from another computer get the page's own file browser instead,
  since the dialog would appear on the plotter's screen.
- **README** with what the plotter does, installation steps for Linux, macOS and Windows, how to run it, a
  screenshot, acknowledgements, copyright and license.

### Changed
- The **Background swaths** setting is shown only when the sidelobe suppression that uses it ("Subtract each beam's
  mean of recent pings") is selected.
- **The command is now `wcp`** (was `water_column_plotter_web`).
- **Installing no longer pulls in PyQt5 and pyqtgraph**, which only the original Qt version needs. Install them with
  `pip install -e ".[gui]"` if you want that version too. This makes installing simpler, especially on ARM
  computers.
- `requirements.txt` lists only what the browser version needs, and no longer asks for packages too new for
  Ubuntu 22.04's Python.

## 2026-11-06

### Added
- **Replay of recorded files** (`.kmall` / `.kmwcd`) from a Source bar under the toolbar, with play/pause, speed
  (0.5x to 10x, or as fast as possible) and a position slider. Live data is ignored while replaying.
- **Real-time source settings in the page**: the UDP address, port and multicast group can be changed without
  restarting. Problems opening the socket are shown in the page instead of stopping the plotter.
- **Thermocline tracking and cast alert**, after Jose Cordero Ros (2018): tracks the bottom of the scattering layer
  that marks the thermocline, and colors it green or red by how far off outer-beam depths would be with the last
  sound speed cast, so you know when a new cast is needed. Tracking is chosen in the toolbar (Off, Layers,
  Thermocline).
- **Horizontal slice depth mode**: besides amplitude, the horizontal slice can show the amplitude-weighted depth of
  the signal within its depth band, colored (deep blue to shallow yellow) or as a shaded surface. Only places where
  the swath covers the whole band are shown.
- **Sidelobe suppression for Kongsberg multi-sector sonars**: "Subtract median per sector" and "Subtract range and
  beam medians", which also evens out level differences between transmit sectors and the stripes they caused.
- **"Subtract each beam's mean of recent pings"** sidelobe suppression, which removes whatever stays the same from
  swath to swath.
- **The sonar's ping rate** in the status line, with the number of swaths per ping.
- **CCOM/JHC logo, an About box** with contributors, and the **BSD 3-Clause license**.

### Changed
- **Each plot has its own Auto-zoom button.** Zooming or panning a plot turns off only that plot's auto-zoom.
- **Zooming is no longer locked to the surface**: the pie and vertical slice can be zoomed to any depth.
- **Along-track averaging, history and backgrounds count swaths, not ping cycles.** A dual-swath system's two swaths
  per ping cycle each count. The defaults doubled (10 and 5000 swaths) to cover the same time as before.

### Removed
- The "Subtract median" sidelobe suppression and the older range and beam medians, replaced by the per-sector
  methods above.

## 2026-11-05

### Added
- **The browser version of Water Column Plotter.** One command starts the plotter and opens it in a web browser: the
  pie, vertical slice and horizontal slice in a fixed layout, with axes in meters. Several people can view the same
  plotter from different computers. Settings that change the processing are shared; display settings are each
  viewer's own.
- **A histogram beside each plot**, with draggable limits for its color scale, and **Auto levels**, which keeps each
  scale at the 10th to 90th percentile of its data.
- **Depths from the water surface** (optional), using the transducer depth from the sonar's installation datagram.
- **Layer tracking** in the vertical slice, after Roland Arsenault's watercolumn_layer_tracker.
- **The sound speed profile** beside the vertical slice, on the same depth scale.
- **Grid lines** and a choice of **color maps** (viridis, jet, grayscale).

### Fixed
- Every sample now counts in its grid cell; the Qt version kept only one sample per cell.
- Beams shorter than the bottom detection no longer add gaps to the image.
- Plots grow with the data, so nothing is cut off at the edges of a fixed-size grid.
- KmallPlayer (the tool that replays files over UDP for testing) now splits large datagrams so they can be put back
  together, and works on macOS.

### Changed
- Kongsberg datagrams are read with the [pykmall](https://pypi.org/project/pykmall/) package instead of a copy
  embedded in this repository.
- Fewer packages are needed to install, and installing works again: the old setup installed no code.

## 2021–2022: the original Qt version

Lynette Davis wrote the original Water Column Plotter, a desktop application built with PyQt5 and pyqtgraph and
tested at sea on the R/V Gulf Surveyor. It received Kongsberg water column datagrams over UDP, reassembled them,
and plotted the pie, vertical slice and horizontal slice in real time, with settings for the network connection,
cell size, heave and averaging, plus pause and a hover readout of values. It is still in this repository: install it
with `pip install -e ".[gui]"` and run `water_column_plotter`.
