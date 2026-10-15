"""
Django settings for Vallotton.

All secrets and deployment knobs come from environment variables. When the
backend runs outside docker (development), the repository root ``.env`` is
loaded first (values already present in the environment win, so docker
compose ``env_file`` behaviour is identical).
"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent   # backend/
REPO_ROOT = BASE_DIR.parent                          # repository root


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- core
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
# The public domain lives ONLY in .env (never in the repo).
DOMAIN = env("DOMAIN", "")

ALLOWED_HOSTS = [h for h in (DOMAIN, "localhost", "127.0.0.1", "backend") if h]
if DEBUG:
    ALLOWED_HOSTS = ["*"]

CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"] if DOMAIN else []
# extra trusted origins for local smoke tests of production builds,
# e.g. CSRF_EXTRA_ORIGINS=http://127.0.0.1:8300
if env("CSRF_EXTRA_ORIGINS"):
    CSRF_TRUSTED_ORIGINS += [
        o.strip() for o in env("CSRF_EXTRA_ORIGINS").split(",") if o.strip()
    ]
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8300",
        "http://127.0.0.1:8300",
    ]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    # outermost on the way out, so it can take back the Vary that the session
    # middleware adds after a view has already returned — see its docstring
    "core.middleware.DoesNotVaryByWhoAsked",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vallotton.urls"

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

WSGI_APPLICATION = "vallotton.wsgi.application"

# ---------------------------------------------------------------- database
if env("DJANGO_DB") == "sqlite":
    # SQLITE_NAME lets a second instance run against its own file. The e2e
    # suites use it: they create and delete folders wholesale, and doing that
    # to the database someone is looking at is not a test, it is vandalism.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": REPO_ROOT / "tmp" / env("SQLITE_NAME", "dev.sqlite3"),
            # SQLite takes one writer at a time and, by default, gives up
            # almost at once. Generation runs in threads and the browser writes
            # while it does, so "database is locked" was reaching the user as a
            # 500 and a setting that silently would not stick. Waiting is the
            # right answer: these writes are milliseconds apart, not seconds.
            "OPTIONS": {"timeout": 20},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "vallotton"),
            "USER": env("POSTGRES_USER", "vallotton"),
            "PASSWORD": env("POSTGRES_PASSWORD"),
            "HOST": env("POSTGRES_HOST", "127.0.0.1"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }

AUTH_PASSWORD_VALIDATORS = []  # accounts are provisioned from .env, not self-service

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------- static & media
STATIC_URL = "/django-static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_ROOT = Path(env("MEDIA_ROOT") or (REPO_ROOT / "tmp" / "media"))
MEDIA_URL = "/api/media/"
# When 1, the media view answers with X-Accel-Redirect and nginx streams the
# file from the shared volume (production). When 0, Django streams it (dev).
MEDIA_ACCEL = env_bool("MEDIA_ACCEL", False)

# ---------------------------------------------------------------- the frontend, served by Django itself
# Only for the local, single-process form (quickstart): the built frontend
# (frontend/dist) is served from here -- whitenoise for the files, a catch-all
# view for index.html (core.views.frontend). In production this stays off:
# nginx serves dist and Django never sees those requests. Off by default, so
# a server that sets nothing serves nothing it should not.
SERVE_FRONTEND = env_bool("SERVE_FRONTEND", False)
FRONTEND_DIST = Path(env("FRONTEND_DIST") or (REPO_ROOT / "frontend" / "dist"))
if SERVE_FRONTEND:
    WHITENOISE_ROOT = FRONTEND_DIST
    WHITENOISE_INDEX_FILE = False

DATA_UPLOAD_MAX_MEMORY_SIZE = 40 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ---------------------------------------------------------------- security
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# COOKIE_SECURE=0 lets a production build be exercised over plain http
# (local docker smoke tests). Defaults to secure whenever DEBUG is off.
_cookie_secure = env_bool("COOKIE_SECURE", not DEBUG)
if not DEBUG:
    SESSION_COOKIE_SECURE = _cookie_secure
    CSRF_COOKIE_SECURE = _cookie_secure
    # TLS terminates at the host nginx; it forwards X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# The token the frontend sends comes from /api/csrf's JSON body, never from the
# cookie, so nothing in the browser has to be able to read it.
CSRF_COOKIE_HTTPONLY = True

# ---------------------------------------------------------------- app config
# Trailing X-Forwarded-For entries added by our own proxies, to skip when
# deciding the caller's address. 1 = host nginx + compose nginx (the standard
# deployment), 0 = compose nginx reached directly. Getting this wrong lets a
# caller spoof the address the login throttle counts — see core.http.client_ip.
TRUSTED_PROXY_HOPS = int(env("TRUSTED_PROXY_HOPS", "1") or 1)

APP_USERNAME = env("APP_USERNAME", "user")
APP_PASSWORD = env("APP_PASSWORD")
ADMIN_USERNAME = env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")

# ---- models -------------------------------------------------------------
# Which vendor answers. One name, and everything model-shaped follows it:
# the clients, the context budget, the reasoning dials, what the quota panel
# reports. See core/services/providers/ — adding a vendor is a module there
# plus a line in its REGISTRY, not a search for "grok" across the tree.
LLM_PROVIDER = env("LLM_PROVIDER", "deepseek")   # deepseek | grok

GROK_API_KEY = env("GROK_API_KEY")
GROK_MODEL = env("GROK_MODEL", "grok-4.6")
GROK_REASONING_EFFORT = env("GROK_REASONING_EFFORT", "high")
# The drawing model gets its own dial. Its job is translation, not judgement:
# it takes a picture description that is already decided and turns it into
# tags, under a prompt that spells out the rules at length. Sharing the lead
# model's "high" cost minutes per image — one captured turn spent ~3 minutes
# on it to emit 423 tokens, all of it in reasoning the user never sees.
GROK_DRAW_REASONING_EFFORT = env("GROK_DRAW_REASONING_EFFORT", "medium")
GROK_TITLE_MODEL = env("GROK_TITLE_MODEL", "grok-4.20-non-reasoning")
# Context budget for the chat model. grok-4.6 reports context_length=500000
# and long_context_threshold=200000 (prices double past that point), so we
# warn once when the conversation crosses the price step and again when it
# approaches the hard ceiling.
GROK_CONTEXT_LIMIT = int(env("GROK_CONTEXT_LIMIT", "500000") or 500000)
GROK_LONG_CONTEXT_THRESHOLD = int(
    env("GROK_LONG_CONTEXT_THRESHOLD", "200000") or 200000
)
GROK_MAX_TOKENS = int(env("GROK_MAX_TOKENS", "20000") or 20000)

# ---- DeepSeek ------------------------------------------------------------
# deepseek-flash: 1M context, 384K max output, and vision. No json_schema —
# the provider spells the shape out in the prompt instead — and no price in
# the usage block, so the quota panel reads the account balance instead of
# summing responses.
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_REASONING_EFFORT = env("DEEPSEEK_REASONING_EFFORT", "max")
DEEPSEEK_DRAW_REASONING_EFFORT = env("DEEPSEEK_DRAW_REASONING_EFFORT", "max")
# Reasoning tokens count against this and its docs warn that a truncated
# answer comes back as broken JSON, so it is generous on purpose.
#
# 128000 because that is what the API itself defaults to at reasoning_effort
# "max" — 64K is the default for ordinary thinking, and setting max effort while
# capping at 64K halves the budget the model is sizing its thinking against.
# It stays just under 131,072, the smallest max-output among the OpenRouter
# hosts this app is willing to route to (see the road comment in
# providers/deepseek.py); raising it past that is what would make the second
# road stop being equivalent to the first.
DEEPSEEK_MAX_TOKENS = int(env("DEEPSEEK_MAX_TOKENS", "128000") or 128000)
# Extra attempts when an answer comes back unusable — empty, truncated, or not
# valid JSON. Two, because a run of pictures is worth more than a few seconds:
# inside a reflection or continuous run a single hiccup used to take the whole
# task down with it, and the user is waiting for ten pictures rather than one.
DEEPSEEK_RETRIES = int(env("DEEPSEEK_RETRIES", "2") or 2)
DEEPSEEK_CONTEXT_LIMIT = int(env("DEEPSEEK_CONTEXT_LIMIT", "1000000") or 1000000)
# No documented price step, so the first notice is set where a conversation
# is worth thinking about rather than where the bill changes.
DEEPSEEK_LONG_CONTEXT_THRESHOLD = int(
    env("DEEPSEEK_LONG_CONTEXT_THRESHOLD", "400000") or 400000
)

# ---- the way round, for when DeepSeek's own API is not there --------------
# Same model, someone else's front door. Set OPENROUTER_API_KEY and the app
# steps over an outage instead of hanging every turn at 「思考中」; leave it
# empty and there is simply one road, which is the behaviour there has always
# been. Nothing above the provider knows either way.
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_BASE = env("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
# deepseek-v4.1-flash is the same flash model with text+image in and a 1M
# window, so a road change cannot change what the app is able to ask for.
OPENROUTER_MODEL = env("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
# How long a road that just failed is stepped over. Only a backstop: what
# normally reopens a road is the sweeper's probe, every LLM_ROAD_PROBE_SECONDS.
# Long enough that a blocked road is not retried by accident, short enough that
# a process which never sweeps still recovers on its own.
LLM_ROAD_BLOCKED_SECONDS = float(env("LLM_ROAD_BLOCKED_SECONDS", "1800") or 1800)
# A probe is one token, and only goes out while a road is actually being
# stepped over — so the cost of asking often is very close to nothing, and what
# it buys is the time the app spends on the more expensive road after the
# preferred one has come back. Ten minutes of that is ten minutes too many.
LLM_ROAD_PROBE_SECONDS = float(env("LLM_ROAD_PROBE_SECONDS", "60") or 60)

# How full the context has to be before the app says so. Provider-independent.
GROK_CONTEXT_WARN_RATIO = float(env("GROK_CONTEXT_WARN_RATIO", "0.8") or 0.8)
NOVELAI_API_KEY = env("NOVELAI_API_KEY")
NAI_MODEL = env("NAI_MODEL", "nai-diffusion-5-full")

MCP_TOKEN = env("MCP_TOKEN")

# Generation limits / behaviour
MAX_UPLOAD_IMAGES_PER_MESSAGE = 6
MAX_UPLOAD_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 4

# Reflection mode: after a picture is painted, the lead model is shown its own
# work and may ask for a redraw. This is the ceiling on that loop — the user's
# slider picks a number within it, and 0 means the feature is off.
MAX_REFLECTION_ROUNDS = int(os.environ.get("MAX_REFLECTION_ROUNDS", "10"))

# Continuous mode: after a turn finishes the app sends the chosen quick phrase
# again, so one instruction produces a run of pictures. Purely a shortcut —
# nothing about it reaches a model — and this is the ceiling on the run.
MAX_CONTINUOUS_ROUNDS = int(os.environ.get("MAX_CONTINUOUS_ROUNDS", "20"))

# Inside a multi-round task (reflection, continuous), a generation that fails
# is retried this many times for free. After that each further attempt costs a
# round from the task's own budget — so one bad minute cannot end a run of ten
# pictures, and a bug cannot turn one into an unbounded loop either.
FREE_RETRIES_IN_RUN = int(os.environ.get("FREE_RETRIES_IN_RUN", "2"))

# Draw a placeholder locally instead of calling NovelAI. For the e2e stack, the
# other half of LLM_PROVIDER=fake: the browser suites are there to exercise the
# app, and paying a vendor per run is what made them too slow to run often.
# Off unless the environment says otherwise, so it cannot reach a real install.
FAKE_NOVELAI = os.environ.get("FAKE_NOVELAI", "") == "1"

# The drawing model is a translator, not an author: it turns one picture
# description into the tags that describe it. There is nothing there for
# invention to improve, and plenty for it to drift away from — the same
# description should come back as the same tags. The lead model is left on the
# vendor default, because that one IS writing.
DRAW_TEMPERATURE = float(os.environ.get("DRAW_TEMPERATURE", "0"))

# How many times each model may look tags up before it has to answer. A lookup
# is a round trip: the model answers with a `lookup` list, the app runs it
# against the local tag table (core/services/danbooru.py) and asks again, so
# every round is one more full model response for the person waiting. The table
# takes up to forty words at once and the prompt says to send them together, so
# one round is the shape this is meant to take; the rest is allowance for a
# second thought. 0 turns lookups off altogether, which is the kill switch if a
# vendor starts mishandling the extra round.
#
# The lead model needs it too, on a tighter budget: the tag lines it writes into
# a library entry are frozen afterwards -- the drawing model may pick a line but
# never reword one -- so the one call that chooses those words is the one that
# should be able to look them up. Tighter because someone is waiting on this
# turn's reply, not on a picture.
DRAW_TOOL_ROUNDS = int(env("DRAW_TOOL_ROUNDS", "4") or 4)
LEAD_TOOL_ROUNDS = int(env("LEAD_TOOL_ROUNDS", "2") or 2)

# The 「使用场景」 paragraph near the top of lead.txt and drawing.txt: what this
# installation is for, in the operator's own words. Empty leaves the section
# out of the prompt altogether. .env.example carries a default text.
LEAD_USAGE_SCENARIO = env("LEAD_USAGE_SCENARIO", "")
DRAW_USAGE_SCENARIO = env("DRAW_USAGE_SCENARIO", "")

LLM_TIMEOUT_SECONDS = int(env("LLM_TIMEOUT_SECONDS", "420") or 420)
# A wall-clock cap on one model answer. LLM_TIMEOUT_SECONDS bounds the silence
# between two bytes, and a long answer is never silent -- DeepSeek keeps the
# request alive with empty lines while it thinks -- so without this a call
# that has lost its way runs until max_tokens (one reasoned for seventeen
# minutes). Ten minutes is well above the longest answer seen that was worth
# having (under four); a capped call is asked for once more.
LLM_ANSWER_SECONDS = int(env("LLM_ANSWER_SECONDS", "600") or 600)
GROK_TIMEOUT_SECONDS = LLM_TIMEOUT_SECONDS   # legacy alias
NAI_TIMEOUT_SECONDS = 240
# A pending job whose heartbeat is older than this is considered dead (e.g.
# the backend was restarted mid-generation). A live worker refreshes the
# heartbeat every 20s from a watchdog thread, so this only needs to be
# comfortably longer than that — not longer than a whole generation.
JOB_STALE_SECONDS = int(os.environ.get("JOB_STALE_SECONDS", "180"))
# How often the sweeper looks for jobs whose worker went silent. It has to be a
# clock rather than a request, because the whole point of a long run is that
# nobody is watching it. 0 turns it off, which is what the tests want.
JOB_SWEEP_SECONDS = int(os.environ.get("JOB_SWEEP_SECONDS", "60"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
