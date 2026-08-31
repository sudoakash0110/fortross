# Fortross LinkedIn Profile API

A browserless FastAPI service built for the Ontross software engineering challenge. It accepts a LinkedIn profile URL and returns normalized profile JSON while applying strict controls around authentication, request volume, logging, and failure behavior.

## What was reverse engineered

LinkedIn currently uses two overlapping internal transports on profile pages:

1. A Voyager GraphQL bootstrap at `/voyager/api/graphql`. The observed profile query is a `GET` with `memberIdentity` and a versioned `queryId`, and returns normalized `data`, `meta`, and `included` collections.
2. A newer SDUI/RSC transport under `/flagship-web/rsc-action`. Profile documents and detail routes are server rendered; paginated sections use SDUI action requests and can return `application/octet-stream` React Server Component payloads.

The older vanity-name Voyager endpoints now return `410 Gone` with a valid session, so this implementation deliberately does not depend on them. It directly requests LinkedIn profile and detail endpoints over HTTPS using `httpx`; it does not run Playwright, Selenium, Chrome, or any browser in production.

The current parser uses the server-rendered profile/detail responses because they contain the internal profile data while avoiding brittle, rotating GraphQL query hashes. It extracts LinkedIn's `window.__como_rehydration__` bootstrap, decodes its JSON chunk array and React Flight rows, resolves row references, and traverses native list elements. Conventional HTML remains supported as a fallback. The transport and parser are separate so either can change without changing the public API.

The data-only Flight decoder handles JSON model rows, UTF-8 byte-length text rows, lazy
references, and property paths such as `$1:props:children`. It does not execute JavaScript,
SDUI actions, imports, or server callbacks. It implements a limited decoding subset, not a
React runtime. Protocol reference: [React Flight client source](https://github.com/facebook/react/blob/main/packages/react-client/src/ReactFlightClient.js).

### Parser correction status

The first user-run live check revealed incorrect profile fields and an unrecognized posts
document. The previous escaped-string regex and page-wide text ordering were unsafe parsing
assumptions; passing the original synthetic tests did not establish live correctness.

The corrected parser decodes JSON before reading values, uses the requested URL for the public
identifier, restricts identity metadata to matching profile objects, and scopes display fields
to the profile header. It excludes skills filters, retains company ancestry for grouped roles,
and no longer treats comma-containing descriptions as locations. Unlabelled location text is
left null. Missing base fields produce warnings; a missing profile identity produces a 502.
Profile image candidates are limited to the header or a matching profile object, not unrelated
avatars anywhere on the page.

Regression fixtures cover these failures using invented people and text. No raw user response,
session cookies, or personal profile capture is included. Corrected profile-field completeness
still needs live review. The posts transport has now returned a successful live feed response,
and the full posts route has been verified offline against that saved response.

### Posts transport diagnosis

A controlled activity-page GET returned HTTP 200 with an Ember/Voyager FastBoot document,
not React Flight. It contained profile bootstrap records in `code[id^="bpr-guid-"]` blocks,
but no activity records or post cards. The document parser therefore correctly refused
to return an empty success, although its assumption that posts would be in that document
was wrong. A working profile request does not establish that the posts transport works.

The page's public JavaScript identifies a separate initial-feed GraphQL query,
`voyagerFeedDashProfileUpdates.20c70fe0314184158516a7ec004c0408`, with `count`, `start:0`,
and `profileUrn`. The target URN can be read from the saved bootstrap by matching
`publicIdentifier`, without fetching the profile again. A browserless first-page
implementation for this format needs a bootstrap GET plus a feed GET, not pagination.

`scripts/debug_posts.py` makes one guarded request and saves the decoded response under
Git-ignored `responses/`, with directory permissions 0700 and file permissions 0600.
It saves no headers and redacts literal configured credentials. Captures can still contain
private profile/viewer data and must not be shared or committed. The API itself does not
save responses. Running the script again makes another live request.

The optional `--feed-from <saved-body.html>` mode reuses that local bootstrap and makes
one feed GET, capped at five items, instead of another document GET.

That controlled feed request returned HTTP 200 with five ordered `*elements` references
and seven included update records. Two of those records were embedded originals of reposts,
not extra feed entries. The parser resolves only the collection's ordered references, maps
`commentary.text.text`, actor information, content images, and `*resharedUpdate`, and keeps
the original's content separate from the reposter's commentary. It does not fetch any of the
returned URLs or follow the pagination token. No explicit publication timestamps were present.

The transport and parser are now wired into `/v1/posts`. A network-blocked replay of both
saved responses through the full API route returned HTTP 200 and five posts, including two
reposts. The updated route was not tested with another pair of live requests. Tests use
invented records and cover ordering, missing references, empty feeds, GraphQL errors,
request budgets, and upstream failures. `LINKEDIN_POSTS_QUERY_ID` can override the observed
versioned query ID if it changes. There is no automatic query-ID probing or fallback request.

## Safety model

- Live LinkedIn access defaults to off.
- The service fails closed when credentials are missing.
- `LINKEDIN_PROFILE_ACCESS=allowlist` is the default and requires an explicit allowlist.
- `LINKEDIN_PROFILE_ACCESS=any` accepts any valid LinkedIn profile URL visible to the session.
- Inbound API-key authentication and minute/hour/day limits are applied.
- LinkedIn concurrency defaults to one, with a minimum delay between upstream requests.
- A shared session budget limits actual upstream attempts to 30/hour and 60/day by default,
  regardless of caller IP. Profiles and posts consume the same budget.
- There are no automatic retries by default.
- Each upstream response is capped at 5 MB by default.
- Authentication failures, checkpoints, challenges, and `429` responses open a circuit breaker.
- The API never caches or stores responses. The explicit local diagnostic script can save
  private captures. Logs must contain request IDs and status metadata only.
- Turso, when configured, stores only hashed rate-limit keys and integer counters.
- `.gitignore` rejects `.env`, HAR files, captures, and response dumps.

Use a dedicated low-value test account if LinkedIn permits it. LinkedIn can change these private endpoints at any time and may restrict accounts that automate access. This project is a constrained technical demonstration, not a recommendation to run unapproved collection at scale.

## Local setup

Python 3.12 or 3.13 is supported.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set a long random `API_KEY`. To enable the live path, set:

```dotenv
LINKEDIN_LIVE_ENABLED=true
LINKEDIN_LI_AT=<session-cookie>
LINKEDIN_JSESSIONID=<csrf-cookie>
LINKEDIN_PROFILE_ACCESS=allowlist
ALLOWED_PROFILE_SLUGS=your-test-profile
```

Do not enter a LinkedIn password. Obtain session values manually, keep them only in your local `.env` or Render secret settings, and rotate/revoke them after the demo.

Before enabling live mode, verify that `.env` is ignored:

```bash
git check-ignore .env
```

The first live test should target only the slug in `ALLOWED_PROFILE_SLUGS`, retain concurrency `1`, and retain `LINKEDIN_MAX_RETRIES=0`. The application never logs or stores the raw response. Stop immediately if it reports an authentication, checkpoint, challenge, or rate-limit error.

To permit other profile targets, explicitly set `LINKEDIN_PROFILE_ACCESS=any` and restart the
server. `ALLOWED_PROFILE_SLUGS` is then ignored, but authentication, URL validation, and budgets
remain active. This does not accept arbitrary websites, company URLs, or private information
the session cannot access. The old `LINKEDIN_FAIL_CLOSED` setting has been removed; setting it
to false never enabled unrestricted targets.

Run locally:

```bash
python -m uvicorn fortross.main:app --host 127.0.0.1 --port 8000
```

Run tests:

```bash
pytest
ruff check .
```

## API

### Health

```http
GET /healthz
```

### Fetch a profile

```http
POST /v1/profiles
X-API-Key: <your-api-key>
Content-Type: application/json

{"url":"https://www.linkedin.com/in/example/"}
```

Example response shape:

```json
{
  "request_id": "0e2cbb65-b699-4f1f-b285-63ed77d87aed",
  "source": "linkedin_internal_web",
  "profile": {
    "source_url": "https://www.linkedin.com/in/example/",
    "public_identifier": "example",
    "name": "Example Person",
    "headline": "Staff Engineer",
    "location": "Singapore",
    "about": "I build reliable systems.",
    "images": {"profile": null, "background": null},
    "experience": [],
    "education": [],
    "skills": [],
    "certifications": [],
    "languages": []
  },
  "warnings": []
}
```

Interactive OpenAPI documentation is available at `/docs`.

### One-request profile smoke test

Start from the project directory so `.env` is loaded:

```bash
python -m uvicorn fortross.main:app --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000/docs`, expand `POST /v1/profiles`, click **Try it out**, enter
your own API key in the `x-api-key` header field, and execute:

```json
{"url":"https://www.linkedin.com/in/your-test-profile/","include_sections":false}
```

With retries set to zero, this issues one upstream GET. The result includes base fields and
empty detail arrays, plus a warning that detail sections were not requested. After reviewing
the output, set `include_sections` to true for the full fetch, which makes up to six sequential
upstream GETs. Do not repeatedly press Execute when an error appears.

### First-page posts (experimental)

`POST /v1/posts` accepts the same API-key header and profile URL restrictions:

```json
{"url":"https://www.linkedin.com/in/your-test-profile/","limit":50}
```

It first retrieves `/in/<slug>/recent-activity/shares/`. For Voyager bootstrap documents,
it resolves the requested member's profile URN and makes one initial-feed GraphQL GET with
`start:0` and `count:limit`. This is at most **two upstream attempts**, each subject to the
same pacing, session budgets, and circuit breaker. Neither request retries, even if the
general retry setting is enabled. A missing or ambiguous target URN stops before the feed GET.
HTML/RSC documents with embedded posts still use the one-request document parser.

It does not fetch a separate profile page, follow redirects, retrieve post detail pages,
or follow a pagination cursor. `limit` is between 1 and 50, caps returned items, and sets
the Voyager feed request's count. Fewer than the requested count is normal.

The response contains `profile_url`, `scope: "first_page"`, `pagination_followed: false`,
`has_more: null`, `truncated`, `posts`, and `warnings`. `truncated` means the caller's limit
discarded parsed items; it says nothing about later LinkedIn pages. Posts include their activity
ID, permalink, text when recognized, author, media, timestamps when explicit, and repost metadata.
`is_repost: true` means an explicit repost reference or marker was found. For Voyager updates,
`false` means the response explicitly marks a root share without an embedded repost; null
means unknown. `original_post` contains the embedded original's ID, URL, text, author, images,
and relative date label when available. The outer `text` is only the reposter's commentary
and can be null. `original_post_url` is retained for compatibility. Original content is never
added as another top-level feed entry unless LinkedIn separately lists it in the collection.
Timestamps are not guessed from relative labels or IDs. There is no 30-day filter or guaranteed
chronological ordering in this first-page version.

The parser supports conventional post-card markup and native cards decoded from the RSC
hydration stream. These paths are verified with **synthetic fixtures only**, not a live posts
response. If LinkedIn returns a shell or an unsupported transport, the endpoint returns
`502 profile_parse_failed` rather than a misleading empty posts list. More protocol discovery
would then be required. The error reports only hydration presence, decoded row count, and
activity-link count, never response text or credentials. A clearly labeled empty-feed page
may return an empty list. Post parsing now shares the corrected Flight decoder and recognizes
activity IDs on SDUI component keys as well as native HTML cards. It still does not fetch a
second endpoint if the first response is only a shell.

Two API calls per minute are allowed by default, shared between both endpoints. Wait until
the next minute window before a third call. HTTP 429 with `api_rate_limited` is our local
limit. HTTP 503 with `linkedin_safety_limit` is the upstream budget or unavailable budget
storage. Auth/challenge errors pause the upstream client; do not retry them in a loop.

## Turso safety state

Turso is optional. When both values are set, the API uses Turso's SQL-over-HTTP `/v2/pipeline` endpoint for rate-limit counters:

```dotenv
TURSO_DATABASE_URL=libsql://database-organization.turso.io
TURSO_AUTH_TOKEN=<token>
```

No LinkedIn response or extracted profile/post field is written to the database. Session-wide
upstream keys use a hash of the session cookie, not the cookie itself. Budget increments are
atomic; denied attempts can conservatively consume counters. In-memory budgets and circuit state
reset on restart. Turso persists counters, but the circuit breaker, pacing, and semaphore remain
process-local. Run one worker/instance for this demo. Changing sessions creates a new budget key.

## Render deployment

1. Push the repository to GitHub.
2. Create a Render Blueprint from `render.yaml` on the free plan.
3. Add `API_KEY`, the two LinkedIn session secrets, `ALLOWED_PROFILE_SLUGS`, and optional Turso settings in Render's secret environment UI.
4. Keep `LINKEDIN_LIVE_ENABLED=false` until the deployment health check passes, then enable it for the controlled demo.
5. Rotate the LinkedIn session after the evaluation.

## Known limitations

- LinkedIn's private web contracts, query IDs, SDUI versions, and markup can change without notice.
- The parser intentionally uses conservative RSC, semantic HTML, and embedded-data extraction. Some fields may be absent or returned with a warning after a LinkedIn UI rollout.
- SDUI skills pagination beyond the server-rendered page is not enabled yet. This avoids sending a partially understood binary-protocol action during the initial safety-first implementation.
- Visibility depends on the configured LinkedIn account and the target profile's privacy settings.
- Render's free service can cold start. In-memory limits reset on restart unless Turso is configured.
- This API is intentionally single-account and low-throughput. It defaults to allowlist-only
  access and is not designed for bulk enrichment.
- Posts are a best-effort, first-document feature with no pagination and no live validation yet.

## Responsible-use note

LinkedIn's terms and technical controls may prohibit automated collection or reverse engineering. Use this repository only where you have permission, keep request volume minimal, and prefer an official integration for production use.
