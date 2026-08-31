# Deploy on Render Free

## Deploy

1. Review changes, run the checks in the README, and push the project to your public GitHub
   repository. Never force-add `.env`, credentials, captures, or SQLite state.
2. In Render, create a **Blueprint**, connect the repository, and use `render.yaml`.
   Confirm the **Free** plan. The repository root should contain `pyproject.toml` and
   `render.yaml`; if using a parent repository, select the nested Blueprint path and set
   the service root directory to `fortross`.
3. Deploy. The Blueprint sets Python 3.13.7, production mode, one worker, temporary SQLite
   safety storage, and the safety limits below. No API key, operator cookie, or Turso needed.
4. Open `https://<service>.onrender.com/healthz` and `/playground`.
5. With live mode still off, test login using the synthetic form value
   `li_at=synthetic; JSESSIONID="ajax:synthetic"`. Authorize with the returned token;
   `/profiles` should return `linkedin_live_disabled`. Execute `/logout`, then confirm
   the revoked token returns 401. This makes no LinkedIn requests.
6. When ready for real extraction, change `LINKEDIN_LIVE_ENABLED` to `true` in
   `render.yaml`, push, and sync the Blueprint. Each evaluator logs in with their own cookie.
   Start with one header-only profile request. Stop on a limit or checkpoint; do not
   switch hosts/restart to bypass an existing cooldown.

[Render Blueprint documentation](https://render.com/docs/blueprint-spec) ·
[Render FastAPI guide](https://render.com/docs/deploy-fastapi)

If creating a Web Service manually instead, use these same settings:

| Setting | Value |
| --- | --- |
| Runtime / plan | Python 3 / Free |
| Build command | `pip install .` |
| Start command | `uvicorn fortross.main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log` |
| Health check | `/healthz` |
| Environment | Copy the entries from `render.yaml` |

## Environment variables

The Blueprint already supplies deployment settings. `.env.example` is for local use;
do not upload your private `.env` to GitHub. There are no required credential variables.

| Variable | Value / purpose |
| --- | --- |
| `APP_ENV` | `production`: enforce production safety requirements. |
| `PYTHON_VERSION` | `3.13.7`: Render's runtime version, not an API setting. |
| `LINKEDIN_LIVE_ENABLED` | `false` for auth-only checks; `true` enables actual LinkedIn requests. |
| `LINKEDIN_PROFILE_ACCESS` | `any`: any valid LinkedIn `/in/<slug>/` URL, not arbitrary websites. |
| `SAFETY_STATE_FILE` | `.state/safety.sqlite3`: local counters/cooldowns only. |
| `SESSION_TTL_SECONDS` | `3600`: bearer token lifetime (one hour). |
| `MAX_ACTIVE_SESSIONS` | `20`: maximum sessions held in memory. |
| `LOGIN_LIMIT_PER_MINUTE` | `5`: public login attempts per minute, globally and per IP. |
| `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_PER_DAY` | Defaults `2`, `10`, `20`: profile/posts API calls per credential. |
| `LINKEDIN_MAX_REQUESTS_PER_HOUR`, `LINKEDIN_MAX_REQUESTS_PER_DAY` | `10`, `20`: actual LinkedIn GETs per credential. |
| `LINKEDIN_GLOBAL_MAX_REQUESTS_PER_HOUR`, `LINKEDIN_GLOBAL_MAX_REQUESTS_PER_DAY` | `60`, `120`: actual GETs across all callers. |
| `LINKEDIN_MIN_REQUEST_INTERVAL_SECONDS` | Default `2`: minimum spacing between upstream request starts. |
| `LINKEDIN_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | Default `3600`: configured safety-stop cooldown. |

Production keeps `LINKEDIN_MAX_CONCURRENCY=1` and `LINKEDIN_MAX_RETRIES=0`.
Optional transport overrides remain in `settings.py`; they do not need environment entries.
To restrict targets later, use `LINKEDIN_PROFILE_ACCESS=allowlist` plus comma-separated
`ALLOWED_PROFILE_SLUGS`. Optional Turso support remains in code but is not needed here.

## Free-tier and submission caveats

Render Free sleeps after 15 minutes without inbound traffic; waking can take about a minute.
Its filesystem is ephemeral: restart/redeploy/spin-down can remove SQLite safety history.
All in-memory API sessions also disappear, so callers must log in again. Free services
cannot attach a persistent disk. [Render Free limitations](https://render.com/docs/free)

This is suitable for a limited demo, not durable production safety enforcement. Local
extraction does not guarantee that LinkedIn accepts requests from Render's IP. About,
education, skills, certifications, and languages remain incompletely verified.

Submit the HTTPS `/playground` URL, public GitHub repository, and optional short video.
State that evaluators need their own LinkedIn cookie and disclose the extraction limitations.
For a demo, show login, a profile/posts result, and logout followed by 401; keep credentials
off-screen. Use labelled saved responses for retakes. Disable live access after evaluation.
