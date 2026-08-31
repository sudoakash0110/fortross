# Implementation notes

Browserless FastAPI service for LinkedIn profile data and first-page posts. Each caller supplies
their own LinkedIn session cookie, receives a temporary bearer token, and requests
structured profile data or first-page posts. No operator account or shared API key is needed.

## Status

Tested responses support profile name, headline, location, images, experience, and posts
including reposts. **About, education, skills, certifications, and languages are not yet
fully verified against current LinkedIn responses.** Missing fields return null/empty
values with warnings, not a guarantee that the source profile has no such data.
The maintainer has successfully smoke-tested the hosted service on Render. That check
does not establish complete section coverage. Complete profile extraction is not yet supported.

## Run locally

Use Python 3.12 or 3.13 and an activated virtual environment:

```bash
pip install -e '.[dev]'
cp .env.example .env  # Only if you do not already have .env.
python -m fortross.check
python -m uvicorn fortross.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Open `http://127.0.0.1:8000/playground`. The template starts with live requests disabled;
set `LINKEDIN_LIVE_ENABLED=true` and restart when ready to request LinkedIn data.
Login and logout work with live mode off. Never put cookies or tokens in `.env`.

## Playground flow

1. Open `POST /login-with-cookie`, click **Try it out**, and paste your full Cookie header
   value into the `cookie` form field. Keep embedded quotes unchanged. It is not JSON.
2. Copy `access_token`. Click **Authorize**, paste the raw token into **SessionToken**
   without a `Bearer` prefix, and authorize.
3. Try `/profiles` with `include_sections:false`, or `/posts` with `limit:5`.
4. Execute `POST /logout`. Reusing the revoked token returns 401. The Authorize dialog's
   Logout button alone only forgets the token in the UI; it does not revoke it server-side.

Login only parses the cookie; `linkedin_session_verified:false` means no LinkedIn request
has verified it. Cookies are session credentials: only submit your own to a server you trust.
This is not LinkedIn OAuth, and automation can result in account restrictions.
Keep cookies, tokens, and private data out of recordings and screenshots.

## API

| Method / path | Input | Authentication |
| --- | --- | --- |
| `GET /healthz` | None; no LinkedIn request | Public |
| `POST /login-with-cookie` | Form field `cookie` | Public, rate-limited |
| `POST /profiles` | JSON: `url`, `include_sections` (default true) | Bearer token |
| `POST /posts` | JSON: `url`, `limit` (1–50, default 50) | Bearer token |
| `POST /logout` | No body | Bearer token |

Interactive schemas and responses: `/playground`; OpenAPI: `/openapi.json`.
`/docs` redirects to the playground. Legacy `/v1/profiles` and `/v1/posts` aliases remain.

```http
POST /profiles
Authorization: Bearer <token>
Content-Type: application/json

{"url":"https://www.linkedin.com/in/example/","include_sections":false}
```

```http
POST /posts
Authorization: Bearer <token>
Content-Type: application/json

{"url":"https://www.linkedin.com/in/example/","limit":5}
```

Profiles return `request_id`, `source`, `profile`, and `warnings`. Sections and images are
included when recognized. Posts include text, author, media, links, and repost metadata;
`original_post` is separate from the reposter's optional commentary. Posts are first-page
only: no pagination or 30-day filter, and no invented timestamps from relative date labels.

Common failures: 400 invalid cookie; 401 invalid/expired/revoked token; 403 disallowed
profile; 429 API/login limit; 503 live disabled or safety stop; 502 upstream/parsing failure.
Inspect the response's `detail.code`. Stop on limits/checkpoints; do not retry in a loop.

## Approach

The service uses `httpx` to request LinkedIn profile/detail documents directly over HTTPS.
The parser handles conventional HTML and the SDUI `__como_rehydration__` bootstrap,
decoding a limited subset of React Flight rows and references without executing JavaScript.
Structural section/card markers identify fields and grouped experience roles.

For Voyager activity pages, it reads the requested profile's URN from the document's
bootstrap, then makes one `/voyager/api/graphql` feed GET with `start:0` and a versioned
query ID. Ordered feed references are resolved separately from embedded repost originals.
There is no browser runtime, pagination, automatic endpoint probing, or production retry.
Transport, parsing, session handling, and safety controls are separate modules.

## Safety and limitations

- Cookies and token hashes stay in process memory. Tokens expire after one hour by default;
  logout revokes them, and a server restart invalidates every session. Use one worker/instance.
- LinkedIn authentication rejection/checkpoints revoke sessions sharing that cookie.
  Expiry at LinkedIn is discovered on a request, not by background polling.
- API calls and actual LinkedIn GETs have separate limits, shared across tokens using the
  same credential. Global upstream budgets, pacing, and cooldowns also apply. A header-only
  profile costs one GET; a full profile up to six; posts up to two.
- SQLite stores only safety counters/cooldowns, never cookies, tokens, or profile data.
  Render Free can lose this file on restart/redeploy/spin-down. Do not restart to bypass limits.
- Normal API logs exclude credentials and profile content. Diagnostic tools can save private
  responses under ignored `responses/`; never commit these or run diagnostics in production.
- Missing sections may be lazy-loaded or unsupported. Endpoints, markup, and query IDs can
  change. Video/document downloads are unsupported, and posts may return fewer than the limit.
- Memory-only credentials still require trust in the server/host. Hosted IPs may receive
  different responses or checkpoints; passing offline tests does not prove hosted extraction.

## Checks and deployment

```bash
pytest
ruff check .
python scripts/check_secrets.py
python -m build
```

Tests use synthetic fixtures and block network access. The secret scan checks Git-visible
working files, not full Git history. Keep `.env`, cookies, captures, and local state ignored.
See [deployment.md](deployment.md) for Render Free setup and environment variables,
or return to the [quick start](../README.md).
