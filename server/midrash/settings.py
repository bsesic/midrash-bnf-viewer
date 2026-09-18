"""Django settings for the viewer's search API.

There is no database: every record the site serves lives in Elasticsearch, so
Django is here only as the layer that turns a reader's query into a search and
keeps the cluster off the public internet.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-a-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "api",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",
]

ROOT_URLCONF = "midrash.urls"
WSGI_APPLICATION = "midrash.wsgi.application"
ASGI_APPLICATION = "midrash.asgi.application"
DATABASES = {}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [REPO_DIR],
    "APP_DIRS": False,
    "OPTIONS": {"context_processors": []},
}]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Elasticsearch ---------------------------------------------------------
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_INDEX = os.environ.get("ES_INDEX", "manuscripts")
ES_USER = os.environ.get("ES_USER") or None
ES_PASSWORD = os.environ.get("ES_PASSWORD") or None
ES_API_KEY = os.environ.get("ES_API_KEY") or None
ES_CA_CERT = os.environ.get("ES_CA_CERT") or None
ES_VERIFY = os.environ.get("ES_VERIFY", "1") != "0"
ES_TIMEOUT = float(os.environ.get("ES_TIMEOUT", "20"))

# Serve index.html from Django too, so `runserver` alone is a working site.
SERVE_VIEWER = os.environ.get("SERVE_VIEWER", "1") == "1"
VIEWER_FILE = Path(os.environ.get("VIEWER_FILE", REPO_DIR / "index.html"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
