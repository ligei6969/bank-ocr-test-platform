"""Repository-wide pytest configuration."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def pytest_configure(config) -> None:
    """Give every run an isolated temp root under ``reports/``.

    A fixed ``--basetemp`` makes pytest delete the previous run before starting.
    On Windows that fails when an interrupted/elevated process left a directory
    with incompatible ACLs. A per-run root avoids both that failure and pytest's
    shared ``%TEMP%/pytest-of-<user>`` directory, while keeping artifacts inside
    the repository's designated reports area.
    """
    if config.option.basetemp is None:
        run_id = uuid4().hex[:12]
        config.option.basetemp = str(Path("reports") / f"pytest-tmp-{run_id}")

