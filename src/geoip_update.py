"""Install the official, freely licensed DB-IP Country Lite MMDB atomically."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import maxminddb

from src.settings import Settings


def release_months(today: date):
    current = today.replace(day=1)
    for _ in range(3):
        yield current.strftime("%Y-%m")
        current = (current - timedelta(days=1)).replace(day=1)


def validate_database(path: Path):
    with maxminddb.open_database(path) as reader:
        metadata = reader.metadata()
        if "country" not in metadata.database_type.lower() or metadata.node_count <= 0:
            raise ValueError("Not a populated Country MMDB")
        # Exercise a real lookup as well as the metadata reader.
        reader.get("8.8.8.8")
        return metadata.database_type


def update_database(output: Path, *, today: date | None = None, client=None):
    today = today or date.today()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".geoip-download-", dir=output.parent) as temporary:
        archive = Path(temporary) / "country.mmdb.gz"
        database = Path(temporary) / "country.mmdb"
        for month in release_months(today):
            url = f"https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                downloaded = 0
                with archive.open("wb") as file:
                    for chunk in response.iter_bytes(65536):
                        downloaded += len(chunk)
                        if downloaded > 100 * 1024 * 1024:
                            raise ValueError("GeoIP archive exceeds the size limit")
                        file.write(chunk)
            expanded = 0
            digest = hashlib.sha256()
            # Reading to EOF verifies gzip integrity before replacing any valid DB.
            with gzip.open(archive, "rb") as source, database.open("wb") as destination:
                while chunk := source.read(65536):
                    expanded += len(chunk)
                    if expanded > 300 * 1024 * 1024:
                        raise ValueError("GeoIP database exceeds the size limit")
                    digest.update(chunk)
                    destination.write(chunk)
            database_type = validate_database(database)
            os.replace(database, output)
            record = {
                "release": month,
                "source": url,
                "sha256": digest.hexdigest(),
                "database_type": database_type,
                "bytes": expanded,
                "license": "CC BY 4.0",
                "attribution": "IP Geolocation by DB-IP (https://db-ip.com)",
            }
            metadata = Path(temporary) / "metadata.json"
            metadata.write_text(json.dumps(record, indent=2) + "\n")
            os.replace(metadata, output.with_suffix(".json"))
            return record
    raise RuntimeError("No DB-IP Country Lite release found for the last three months")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(Settings().geoip_database))
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            record = update_database(args.output, client=client)
        print(
            f"DB-IP Country Lite {record['release']} installed: {args.output} ({record['bytes']} bytes)"
        )
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(
            f"GeoIP update failed ({type(exc).__name__}); existing database preserved"
        ) from None


if __name__ == "__main__":
    main()
