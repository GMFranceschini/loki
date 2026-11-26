"""Author lists: keep both ends, drop the middle."""

from __future__ import annotations

# How many names to store per paper. A consortium paper can carry hundreds,
# and the database is meant to stay small enough to live in a home directory.
KEEP = 25


def trim_authors(names: list[str], keep: int = KEEP) -> tuple[list[str], int]:
    """Return the names worth keeping and how many there were in total.

    In biology the last author is the group leader, so it carries as much
    information as the first. Plain truncation loses it on exactly the large
    collaborations where it matters most, so the last name is always kept and
    the middle of the list goes instead.
    """
    kept = [name for name in names if name]
    total = len(kept)
    if total <= keep:
        return kept, total
    return kept[: keep - 1] + [kept[-1]], total
