"""Text and JSON output of the results."""

import dataclasses
import json
import os
from collections import Counter
from pathlib import Path

from .runner import CaseResult


def grid(results: list[CaseResult]) -> str:
    """ACK counts by frequency and datarate for the cases expecting an ACK."""
    cells: dict[tuple[int, int], str] = {}
    for result in results:
        if result.expect_ack and not result.recheck and result.dr is not None:
            cells[result.frequency_hz, result.dr] = f"{result.acked}/{len(result.attempts)}"
    if not cells:
        return ""
    drs = range(max(7, max(dr for _, dr in cells)) + 1)
    width = max(5, max(len(cell) for cell in cells.values())) + 2
    lines = ["MHz".ljust(9) + "".join(f"DR{dr}".ljust(width) for dr in drs)]
    for frequency in sorted({frequency for frequency, _ in cells}):
        row = "".join(cells.get((frequency, dr), ".").ljust(width) for dr in drs)
        lines.append(str(frequency / 1e6).ljust(9) + row)
    return "\n".join(line.rstrip() for line in lines)


def summary(results: list[CaseResult]) -> str:
    sections = [grid(results)]
    negatives = [result for result in results if not result.expect_ack]
    if negatives:
        sections.append(
            "Negative cases:\n"
            + "\n".join(f"  {r.name}: {r.verdict}, {r.detail}" for r in negatives)
        )
    totals = Counter(str(result.verdict) for result in results)
    sections.append("Totals: " + ", ".join(f"{count} {name}" for name, count in totals.items()))
    return "\n\n".join(section for section in sections if section)


def write_json(path: str | Path, results: list[CaseResult]):
    """Replace the file atomically, so it stays readable during a run."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    os.replace(temp, path)
