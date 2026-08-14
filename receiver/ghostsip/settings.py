"""GhostSIP Django settings.

Deployment model (docs/deployment.md): gunicorn on 127.0.0.1:8100 inside the
container; Caddy exposes ONLY /webhook (+ /healthz) publicly; the admin is
reached over an SSH tunnel to loopback. Bootstrap secrets come from .env via
docker-compose; the Django SECRET_KEY is generated once by entrypoint.sh into
the persistent /etc/ghostsip volume.
"""

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _secret_key() -> str:
    path = os.environ.get("GHOSTSIP_SECRET_KEY_FILE", "/etc/ghostsip/secret_key")
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    # Local development / test runs only — entrypoint.sh always writes the file.
    return os.environ.get("GHOSTSIP_DJANGO_SECRET", "insecure-key-for-tests-only")


SECRET_KEY = _secret_key()
DEBUG = False

# Admin arrives via the SSH tunnel (loopback); the webhook arrives via Caddy
# with the public domain in the Host header.
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
if os.environ.get("GHOSTSIP_DOMAIN"):
    ALLOWED_HOSTS.append(os.environ["GHOSTSIP_DOMAIN"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "panel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

# django-axes: proven login lockout. Every admin request arrives from
# loopback (SSH tunnel), so lock by username rather than IP.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 10
AXES_COOLOFF_TIME = timedelta(minutes=10)
AXES_LOCKOUT_PARAMETERS = ["username"]

ROOT_URLCONF = "ghostsip.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ghostsip.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("GHOSTSIP_DB", "/etc/ghostsip/db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("GHOSTSIP_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Admin is browsed at http://127.0.0.1:8100 through the SSH tunnel.
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1:8100", "http://localhost:8100"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
