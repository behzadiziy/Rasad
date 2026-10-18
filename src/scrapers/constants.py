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

# Samples (data/debug/jobvision/, captured 2026-10-18):
#   S1: 20261018T111201_243236Z_response_004_POST_candidateapi_jobvision_ir_api_v1_JobPost_L.json
#       (keyword "ai", page 1; trimmed copy: tests/fixtures/jobvision/search_response.json)
#   S2: 20261018T111242_225098Z_response_003_POST_..._JobPost_L.json (keyword "ai", page 2) and
#       20261018T112146_794962Z_response_004_POST_..._JobPost_L.json (keyword "دورکاری", page 1)
#   S3: 20261018T112244_276906Z_page__jobs_1558759_استخدام-instagram-admin.html (remote job) plus
#       20261018T112244_281390Z_json_ld_job_posting.json, and the on-site job page
#       20261018T111405_963981Z_page__jobs_1571497_استخدام-مالک-محصول-product-owner.html
#   S4: 20261018T111201_193507Z_page__jobs_keyword_ai.html and
#       20261018T112146_749098Z_page__jobs_keyword_دورکاری.html (rendered search results)
# A JSON path is a tuple of dict keys walked from one job item (or from the response body).

# ── Search API (intercepted) ───────────────────────────────────────────────
# Source: S1/S2: every search page issues one `POST https://candidateapi.jobvision.ir/api/v1/JobPost/List`
# (content-type JSON). Anchored so `JobPost/Recommendation/Similar` (detail pages) and
# `RelatedSearch`/`Basedata` (search pages) on the same host do not match. Matched case-insensitively.
JOBVISION_API_ROUTE_PATTERN = r"^https://candidateapi\.jobvision\.ir/api/v1/JobPost/List(?:[/?#]|$)"
# Source: S1/S2: POST bodies `{"pageSize":30,"requestedPage":2,"keyword":"ai",...}`. Used to ignore a
# list response that belongs to a different page than the one being scraped.
JOBVISION_API_PAGE_FIELD = "requestedPage"
# Source: tasks/04-jobvision-scraper.md (wait up to 20 s for the job-list response).
JOBVISION_API_WAIT_S = 20.0
# VERIFY: upper bound (s) for reading one intercepted body; the S1 body (~155 KB) arrives in well
# under a second. Raise it if a slow network logs "Timed out reading" warnings.
JOBVISION_BODY_READ_TIMEOUT_S = 20.0

# Source: S1: body `{"isSuccess": true, "data": {"currentPage", "pageSize", "jobPosts": [...30 items],
# "jobPostCount", ...}}`. An empty `jobPosts` list means "no more results".
JOBVISION_JOB_LIST_PATH: tuple[str, ...] = ("data", "jobPosts")
# Source: S1: `"id": 1411778` (integer).
JOBVISION_FIELD_ID: tuple[str, ...] = ("id",)
# Source: S1: `"title": "AI Artist"`.
JOBVISION_FIELD_TITLE: tuple[str, ...] = ("title",)
# Source: S1: `company.nameFa` (Persian name, or the anonymous-employer text such as
# «سازمانی فعال در حوزه تولیدی / صنعتی» with `company.id == 0`); `nameEn` is the fallback.
JOBVISION_FIELD_COMPANY: tuple[tuple[str, ...], ...] = (("company", "nameFa"), ("company", "nameEn"))
# Source: S1/S2: `location.city.titleFa` («تهران», «تبریز») and `location.region.titleFa` («امانیه»,
# often null outside Tehran). Non-empty parts are joined with JOBVISION_CITY_SEPARATOR, matching the
# «تهران ، امانیه» text of the S4 cards. The province is used only when the city is missing, because a
# province name («تهران») would wrongly match city rules for other cities in that province.
JOBVISION_FIELD_CITY_PARTS: tuple[tuple[str, ...], ...] = (
    ("location", "city", "titleFa"),
    ("location", "region", "titleFa"),
)
# Source: S1: `location.province.titleFa`.
JOBVISION_FIELD_CITY_FALLBACK: tuple[str, ...] = ("location", "province", "titleFa")
# Source: S4 (the cards render city and region separated by «،»).
JOBVISION_CITY_SEPARATOR = "، "
# Source: S2 (keyword «دورکاری»): all 30 items have `properties.isRemote: true` and the label
# «امکان دورکاری» (labelDetails type 2); the S3 remote job's JSON-LD says `jobLocationType: TELECOMMUTE`
# and its card links to `/type/remote`. On-site items have `isRemote: false`. No hybrid flag or label
# exists in any capture, so True maps to REMOTE and False to ONSITE.
JOBVISION_FIELD_REMOTE_FLAG: tuple[str, ...] = ("properties", "isRemote")
# Source: S1/S2: `labels` is a list of short badge strings («امکان دورکاری», «پاره وقت»,
# «امکان جذب کارآموز»). Used with `detect_work_mode` only when the remote flag is missing.
JOBVISION_FIELD_LABELS: tuple[str, ...] = ("labels",)
# Source: S1: `salary` is null or `{"titleFa": "38 - 52 میلیون تومان", "min": 38, "max": 52, ...}`.
JOBVISION_FIELD_SALARY: tuple[str, ...] = ("salary", "titleFa")
# Source: S1: list items carry no description field, so every new job's detail page is visited.
JOBVISION_FIELD_DESCRIPTION: tuple[str, ...] | None = None

# Source: probe run of `https://jobvision.ir/jobs/1558759` (2026-10-18): the ID-only URL redirects to
# the slugged page of S3. `{id}` receives the URL-encoded external ID.
JOBVISION_JOB_URL_TEMPLATE = "https://jobvision.ir/jobs/{id}"

# ── Detail page ────────────────────────────────────────────────────────────
# Source: S3: the description is the <div> right after the «شرح شغل و وظایف» / «Job Description»
# <h2> inside `<section class="job-specification">`; the first match is the description in both the
# Persian and the English page (the second is the requirements block). Tried before JSON-LD, whose
# description (S3 json_ld_job_posting.json) drops line breaks and fuses words («defined toneRequirements:»).
JOBVISION_DETAIL_DESCRIPTION_SELECTOR = "section.job-specification h2 + div"
# VERIFY: how long (ms) to wait for the description element or JSON-LD to be attached. Both are server-rendered
# in S3, so this only matters on slow loads.
JOBVISION_DETAIL_WAIT_TIMEOUT_MS = 10_000

# ── Search page DOM (fallback when no API response is captured) ─────────────
# Source: S4: one <job-card> element per result (30 per page).
JOBVISION_CARD_SELECTOR = "job-card"
# Source: S4: `<a id="el-1569330" href="/jobs/1569330/استخدام-...?row=1&...">` (repeated in each card).
JOBVISION_CARD_LINK_SELECTOR = 'a[id^="el-"][href*="/jobs/"]'
# Source: S4: the numeric job ID in the link path `/jobs/{id}/{slug}`.
JOBVISION_CARD_ID_PATTERN = r"/jobs/(\d+)(?:[/?#]|$)"
# Source: S4: `<div class="job-card-title ...">AI Engineer</div>`.
JOBVISION_CARD_TITLE_SELECTOR = ".job-card-title"
# Source: S4: `<a href="/companies/2985/...">` for named employers, `<a href="/companies">` for anonymous ones.
JOBVISION_CARD_COMPANY_SELECTOR = 'a[href^="/companies"]'
# Source: S4: `<div class="... jvt-whitespace-nowrap ..."><span class="jvt-pointer-events-none ...">
# <a>تهران</a> ، امانیه</span><span class="jvt-mx-2 ..."> | </span><span>38 - 52 میلیون تومان</span>`.
# VERIFY when the fallback runs: these are Tailwind utility classes, which change more often than
# component names. Confirm with `--debug-capture` and the saved `search_dom_*.html`.
JOBVISION_CARD_CITY_SELECTOR = "div.jvt-whitespace-nowrap > span.jvt-pointer-events-none"
JOBVISION_CARD_SALARY_SELECTOR = "div.jvt-whitespace-nowrap > span.jvt-mx-2 + span"
# Source: S4: badges `<span class="filter-label ..."><a href=".../type/remote">امکان دورکاری</a>`.
JOBVISION_CARD_LABEL_SELECTOR = ".filter-label"
# Source: S4: the remote badge links to the site's remote filter; present exactly on the cards whose
# API items have `isRemote: true` (1506282 and 1535897 in the "ai" search).
JOBVISION_CARD_REMOTE_SELECTOR = 'a[href$="/type/remote"]'

# ════════════════════════════════════════════════════════════════════════════
# JOBINJA
# ════════════════════════════════════════════════════════════════════════════

# Source: data/debug/jobinja/20261018T111307_142562Z_page__jobs.html (absolute job links use this host)
JOBINJA_BASE_URL = "https://jobinja.ir"
# Source: data/debug/jobinja/20261018T111307_142562Z_page__jobs.html: server-rendered results for
# the keyword, and the page's own pagination links use `filters[keywords][0]=...&page=N`.
JOBINJA_SEARCH_URL_TEMPLATE = "https://jobinja.ir/jobs?filters%5Bkeywords%5D%5B0%5D={keyword}&page={page}"
