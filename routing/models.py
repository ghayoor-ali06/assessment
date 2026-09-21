from django.db import models


class FuelStation(models.Model):
    """A truck stop with its quoted retail price, already geocoded.

    Coordinates are resolved offline by ``build_station_data`` (the source CSV
    has none), so nothing here requires a network call at request time.
    """

    opis_id = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    price_per_gallon = models.DecimalField(max_digits=7, decimal_places=4)

    class Meta:
        # The raw file lists the same stop several times under different rack
        # IDs; the importer averages those, leaving one row per stop.
        constraints = [
            models.UniqueConstraint(fields=["opis_id", "name"], name="unique_station"),
        ]
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"
