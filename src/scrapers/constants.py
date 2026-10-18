"""Every site-specific value used by the scrapers, in three sections.

- `SHARED` (Task 3): browser settings, Cloudflare markers and limits, runtime
  paths, and work-mode cues.
- `JOBVISION` (Task 4 may edit only this section).
- `JOBINJA` (Task 5 may edit only this section).

Each site-specific value carries either `# Source: <sample>` (derived from a real
capture) or `# VERIFY: <what and how to confirm>` (not yet confirmed). Fixing a
broken scraper should require editing this file only.

URL templates use `str.format` placeholders: `{keyword}` receives the
URL-encoded keyword and `{page}` the 1-based page number.
"""

from __future__ import annotations

from pathlib import Path

# ════════════════════════════════════════════════════════════════════════════
# SHARED
# ════════════════════════════════════════════════════════════════════════════

# ── Browser context ────────────────────────────────────────────────────────
# VERIFY: desktop Chrome UA. Keep the major version in sync with the Chromium that
# Playwright bundles (`Browser.version`; 156 for playwright 1.64) and the OS token
# consistent with the host, because Cloudflare compares the UA with client hints.
# Overriding it also removes "HeadlessChrome" from the default headless UA.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080
LOCALE = "fa-IR"
TIMEZONE_ID = "Asia/Tehran"
ACCEPT_LANGUAGE = "fa-IR,fa;q=0.9,en;q=0.8"
# VERIFY: default timeout (ms) for navigations and page actions; raise it if Iranian
# sites load slowly from your network.
NAVIGATION_TIMEOUT_MS = 60_000

# ── Cloudflare challenge detection ─────────────────────────────────────────
# VERIFY: lower-cased substrings of the interstitial's <title>. Cloudflare may
# localize the title for `fa-IR`, so the DOM markers below are the primary signal.
# Confirm by saving the HTML of a challenged page with the probe.
CLOUDFLARE_TITLE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "attention required",
    "one more step",
)
# VERIFY: CSS selectors present only on Cloudflare challenge/block pages. Do not add
# `/cdn-cgi/challenge-platform/` scripts: Cloudflare injects them into normal pages too.
CLOUDFLARE_DOM_MARKERS: tuple[str, ...] = (
    "#challenge-form",
    "#challenge-stage",
    "#challenge-running",
    "#challenge-error-text",
    "#cf-challenge-running",
    "#cf-please-wait",
    "#cf-error-details",
    'iframe[src*="challenges.cloudflare.com"]',
)
CLOUDFLARE_WAIT_HEADLESS_S = 30.0
CLOUDFLARE_WAIT_HEADFUL_S = 120.0
CLOUDFLARE_POLL_INTERVAL_S = 1.0

# ── Runtime paths (relative to the project root) ───────────────────────────
BROWSER_STATE_DIR = Path("data/browser_state")
DEBUG_DIR = Path("data/debug")

# ── Work-mode cues (matched after `normalize_text`, so ZWNJ/Yeh/Kaf variants and
# letter case do not matter). Use on short labels and badges only. ─────────
# VERIFY: hybrid labels; confirm against the badges in captured search results.
HYBRID_CUES: tuple[str, ...] = (
    "هیبریدی",
    "هیبرید",
    "نیمه حضوری",
    "hybrid",
)
# VERIFY: remote labels; confirm against the badges in captured search results.
REMOTE_CUES: tuple[str, ...] = (
    "دورکاری",
    "دور کاری",
    "remote",
    "work from home",
)

# ════════════════════════════════════════════════════════════════════════════
# JOBVISION
# ════════════════════════════════════════════════════════════════════════════

# Source: data/debug/jobvision/20261018T111201_193507Z_page__jobs_keyword_ai.html (job links are relative to this host)
JOBVISION_BASE_URL = "https://jobvision.ir"
# Source: probe runs of `/jobs/keyword/AI` and `/jobs/keyword/AI?page=2` (2026-10-18): the SPA
# sent `{"keyword": "ai", "requestedPage": 2, ...}` to the job-list API. The bare `/jobs?keyword=`
# form is ignored by the SPA (no keyword in the API request).
JOBVISION_SEARCH_URL_TEMPLATE = "https://jobvision.ir/jobs/keyword/{keyword}?page={page}"

# ════════════════════════════════════════════════════════════════════════════
# JOBINJA
# ════════════════════════════════════════════════════════════════════════════

# Source: data/debug/jobinja/20261018T111307_142562Z_page__jobs.html (absolute job links use this host)
JOBINJA_BASE_URL = "https://jobinja.ir"
# Source: data/debug/jobinja/20261018T111307_142562Z_page__jobs.html: server-rendered results for
# the keyword, and the page's own pagination links use `filters[keywords][0]=...&page=N`.
JOBINJA_SEARCH_URL_TEMPLATE = "https://jobinja.ir/jobs?filters%5Bkeywords%5D%5B0%5D={keyword}&page={page}"
