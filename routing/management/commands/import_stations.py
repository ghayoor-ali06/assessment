"""Load the committed, geocoded station CSV into the database.

Idempotent: re-running replaces the table contents in a single transaction, so
a partial or interrupted import never leaves a half-populated dataset behind.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.services.corridor import reset_station_index
from routing.services.geo import in_usa

BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Import data/stations.geocoded.csv into the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default=None,
            help="Geocoded station CSV (default: data/stations.geocoded.csv).",
        )

    def handle(self, *args, **options):
        from routing.models import FuelStation

        source = Path(options["source"] or Path(settings.DATA_DIR) / "stations.geocoded.csv")
        if not source.exists():
            raise CommandError(
                f"{source} not found. Run 'manage.py build_station_data' first."
            )

        stations: list[FuelStation] = []
        skipped = 0
        with source.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    latitude = float(row["lat"])
                    longitude = float(row["lon"])
                    price = Decimal(row["price"])
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    skipped += 1
                    continue
                # Guard against a malformed or hand-edited data file placing a
                # station somewhere that would silently skew a route.
                if price <= 0 or not in_usa(latitude, longitude):
                    skipped += 1
                    continue
                stations.append(
                    FuelStation(
                        opis_id=row.get("opis_id", "").strip(),
                        name=row.get("name", "").strip(),
                        address=row.get("address", "").strip(),
                        city=row.get("city", "").strip(),
                        state=row.get("state", "").strip().upper(),
                        latitude=latitude,
                        longitude=longitude,
                        price_per_gallon=price.quantize(Decimal("0.0001")),
                    )
                )

        if not stations:
            raise CommandError(f"No usable rows in {source}")

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=BATCH_SIZE)

        reset_station_index()
        message = f"Imported {len(stations)} stations"
        if skipped:
            message += f" ({skipped} rows skipped)"
        self.stdout.write(self.style.SUCCESS(message))
