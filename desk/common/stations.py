"""The line's stations, in order. Mirrored from the console project."""

STATIONS = [
    "S00-MANDREL-LAYUP",
    "S01-CURE-OVEN",
    "S02-CNC-TRIM",
    "S03-NDT-SCAN",
    "S04-BOND-ASSEMBLY",
    "S05-AVIONICS-INSTALL",
    "S06-INTEGRATION",
    "S07-FINAL-QA",
]

# Index lookup, so "is the root station upstream of the trigger?" is a
# comparison rather than a search. Work flows 0 -> 7, so upstream means lower.
STATION_INDEX = {name: i for i, name in enumerate(STATIONS)}


def is_upstream_of(candidate: str, reference: str) -> bool:
    """True when `candidate` sits earlier on the line than `reference`.

    Used by the classifier's plausibility check: a defect found at NDT can be
    caused by layup, but a defect found at layup cannot be caused by final QA.
    """
    if candidate not in STATION_INDEX or reference not in STATION_INDEX:
        return False
    return STATION_INDEX[candidate] < STATION_INDEX[reference]
