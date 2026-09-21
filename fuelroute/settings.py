"""Django settings for the fuel-route project.

Tuned for a reviewer who wants to clone and run: SQLite by default, an
in-process cache, and no services to install. Anything environment-specific is
read from the environment with a working default.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env if present. Real environment variables always win, so a container
# or CI runner can override anything the file sets.
load_dotenv(BASE_DIR / ".env", override=False)

# Committed CSV data: station prices/coordinates and the places gazetteer.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-dev-key-not-for-production-use"
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "fuelroute.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]
WSGI_APPLICATION = "fuelroute.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
    }
}

# Fuel prices are a static snapshot, so routes can be cached aggressively. A
# cache hit skips the external routing call entirely, which is the single
# biggest win for response time.
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", 24 * 60 * 60))

# In-process cache: no service to run, and plenty for a single-process server.
# Note it is per-process, so under a multi-worker deployment each worker keeps
# its own copy and the hit rate drops. That is when REDIS_URL earns its keep.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute",
        "OPTIONS": {"MAX_ENTRIES": 2048},
    }
}

_redis_url = os.environ.get("REDIS_URL")
if _redis_url:
    # Django's Redis backend needs the `redis` package, which is not a base
    # requirement because nothing here uses it by default. Fail with a clear
    # message now rather than an opaque ImportError on the first request.
    try:
        import redis  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "REDIS_URL is set but the 'redis' package is not installed. "
            "Run `pip install redis`, or unset REDIS_URL to use the "
            "in-process cache."
        ) from exc
    CACHES["default"] = {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _redis_url,
    }

AUTH_PASSWORD_VALIDATORS: list[dict] = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "UNAUTHENTICATED_USER": None,
}

# ---------------------------------------------------------------------------
# Routing / fuel-planning behaviour
# ---------------------------------------------------------------------------

# Identifies this client to OSM-run services, which their usage policies require.
USER_AGENT = os.environ.get(
    "USER_AGENT", "fuel-route-optimizer/1.0 (backend assessment; contact via repo)"
)

ROUTING_PROVIDER = os.environ.get("ROUTING_PROVIDER", "osrm")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
VALHALLA_BASE_URL = os.environ.get("VALHALLA_BASE_URL", "https://valhalla1.openstreetmap.de")
ROUTING_TIMEOUT_SECONDS = float(os.environ.get("ROUTING_TIMEOUT_SECONDS", 15))

NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
)
# Street addresses fall through to Nominatim; "City, ST" is resolved offline.
ENABLE_REMOTE_GEOCODING = _env_bool("ENABLE_REMOTE_GEOCODING", True)

VEHICLE_RANGE_MILES = float(os.environ.get("VEHICLE_RANGE_MILES", 500))
VEHICLE_MPG = float(os.environ.get("VEHICLE_MPG", 10))
# How far off the route a station may sit and still count as "on the way".
CORRIDOR_MILES = float(os.environ.get("CORRIDOR_MILES", 5))
# Keep only the cheapest station per this many miles; 0 disables.
CLUSTER_BIN_MILES = float(os.environ.get("CLUSTER_BIN_MILES", 25))
