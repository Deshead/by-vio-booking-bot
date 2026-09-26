"""Consistent online SQLite snapshots, including isolated portfolio demos."""

import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup(data_dir: Path, output_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    output_dir = output_dir.resolve()
    sources = list(data_dir.glob("*.sqlite3")) + list((data_dir / "demo").glob("*.sqlite3"))
    if not sources:
        raise ValueError(f"No SQLite databases found in {data_dir}")
    destination = output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for source in sources:
        target = destination / source.relative_to(data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(target)) as snapshot:
                origin.backup(snapshot)
                if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    args = parser.parse_args()
    try:
        result = backup(args.data_dir, args.output_dir)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print(f"Backup created: {result}")
