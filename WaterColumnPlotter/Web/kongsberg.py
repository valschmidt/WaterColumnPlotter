"""
Sidelobe suppression specific to Kongsberg multibeams (EM 2040, EM 712, ...), which transmit each swath in several
sectors, each steered and at its own frequency, so levels differ from sector to sector and across each sector's beams.
These use the sector each beam was transmitted in (#MWC beamTxSectorNum); sonars without transmit sectors use the
other options in gridding.py.

In a REV I EM 2040 file (docs/experiments.md) the centre sector's level rises about 5 dB from nadir to its edges
while the outer sectors are flat, and beyond the minimum slant range (the nadir range ring) the sidelobe echo of the
seafloor is far above the water inside it.
"""

import numpy as np

from WaterColumnPlotter.Web.medians import across_beam_median, per_beam_floor_fit, per_beam_split_median

# Sidelobe suppression choices handled here:
# 'median_sector': at each range sample, subtract the median over each transmit sector's beams.
# 'median_beam': first subtract each beam's median, taken separately inside the minimum slant range and beyond
#   it (so the much louder sidelobe region beyond it is levelled against itself, not against the water), then the
#   range median per sector.
# 'median_range_poly_beam': subtract the range median per sector, then each beam's noise floor: a quadratic in range
#   fitted to what is left, weighted by 1 / linear power (medians.per_beam_floor_fit).
OPTIONS = ('median_sector', 'median_beam', 'median_range_poly_beam')


def suppress_sidelobes(dg, how, amplitudes, beam_index, sample_number, min_slant_range_sample):
    """
    Returns the amplitudes (dB, one per sample, with each sample's beam index and range sample number) less the
    levels the option removes, for one #MWC datagram (as read by pykmall). min_slant_range_sample: the shortest bottom
    detection's range sample number.
    """
    if how == 'median_beam':
        amplitudes = amplitudes - per_beam_split_median(amplitudes, beam_index, sample_number, min_slant_range_sample)
    sector = np.asarray(dg['beamData']['beamTxSectorNum'], dtype=np.int64)[beam_index]
    amplitudes = amplitudes - across_beam_median(amplitudes, beam_index, sample_number, sector)
    if how == 'median_range_poly_beam':
        amplitudes = amplitudes - per_beam_floor_fit(amplitudes, beam_index, sample_number)
    return amplitudes
