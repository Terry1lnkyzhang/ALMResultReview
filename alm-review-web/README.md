# ALM Review Workspace

FastAPI application for recursive ALM Test Lab synchronization, immutable Run
revisions, AI review, and manual decisions. It supports a single-machine combined mode
and a split deployment with a remote Web server and a laptop Worker sharing MySQL.

## Environment

This project uses exactly one virtual environment:

```text
alm-review-web/.venv
```

The environment-local `pip.ini` uses the Tsinghua PyPI mirror:

```text
https://pypi.tuna.tsinghua.edu.cn/simple
```

Always run Python, pip, tests, and the server through this interpreter:

```powershell
.\alm-review-web\.venv\Scripts\python.exe
```

## Local Configuration

Copy values from `.env.example` into the ignored `.env` file. Keep MySQL, ALM, and
AI credentials out of source control and logs.

External automation is disabled by default. Open the non-navigated operations page
at `/ops/configuration` to configure the ALM folder, schedule, local AI endpoint, and
prompt. Enable ALM synchronization and AI review only after their credentials and
endpoints are ready.

## Web and Worker deployment

Use split mode when ALM, the AI endpoint, or approved network evidence paths can only be
reached from the laptop:

```text
Browsers -> remote Web server -> shared MySQL <- laptop Worker
											 -> ALM / AI / UNC evidence
```

Both machines must use the same `DATABASE_URL`. Keep `ALM_USERNAME`, `ALM_PASSWORD`, and
`AI_API_KEY` only on the laptop Worker. The Web server creates database jobs and displays
committed results; it does not connect to ALM, call AI, or read evidence paths.

On the remote Web server, run these commands from the `alm-review-web` directory in
PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.web.example .env
```

`requirements.txt` mirrors the runtime dependencies declared in `pyproject.toml` for
deployment tools that expect a requirements file. Edit the ignored `.env` file and replace
the database host, database credentials, and both `CHANGE_ME` values:

```dotenv
DATABASE_URL=mysql+pymysql://alm_web:CHANGE_ME@mysql-host:3306/almreview?charset=utf8mb4
APP_ROLE=web
SCHEDULER_ENABLED=false
WEB_AUTH_ENABLED=true
WEB_AUTH_USERNAME=review-user
WEB_AUTH_PASSWORD=CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD
```

Start the remote server on an internal or VPN interface:

```powershell
.\start-server.ps1 -BindAddress 0.0.0.0 -Port 8010
```

Laptop Worker `.env`:

```dotenv
DATABASE_URL=mysql+pymysql://user:password@mysql-host:3306/almreview?charset=utf8mb4
APP_ROLE=worker
SCHEDULER_ENABLED=true
WORKER_ID=my-laptop
WORKER_POLL_SECONDS=5
WORKER_LEASE_SECONDS=900
ALM_USERNAME=code1-id
ALM_PASSWORD=secret
AI_API_KEY=
```

Start the laptop Worker without exposing a Web port:

```powershell
.\start-worker.ps1
```

The Dashboard shows the latest Worker heartbeat and polls active re-review progress every
15 seconds. Other lists display newly committed results after a page refresh. ALM sync and
review buttons only queue work; processing continues while the laptop Worker is online.
Worker claims use database row locks and expiring leases so two Workers cannot normally
process the same job and interrupted work can be retried. Restart the Worker after changing
the configured daily schedule so it reloads the Cron trigger.

Do not expose Uvicorn or MySQL directly to the public Internet. Put the Web server behind
company VPN/internal networking and an HTTPS reverse proxy. `APP_ROLE=web` requires HTTP
Basic credentials, but TLS is still required to protect those credentials in transit.

### Network image evidence

Network image review is disabled by default and uses three independent settings:

- `Read approved network evidence` permits read-only access below the configured UNC root.
- `Send evidence images to AI` adds supported images to the review request.
- `Allow image transfer over HTTP` explicitly accepts unencrypted image transport. HTTPS
	and loopback endpoints do not require this exception.

The resolver reads only PNG, JPEG, and WebP files, recurses at most two directory levels,
sends at most four images per Step and twelve per Run, and rejects images larger than 5 MB.
The combined image budget is 10 MB per Step and 15 MB per Run.
Image bytes stay in memory and are not stored in the database; review results retain only
the relative filename, media type, byte size, and SHA-256 digest. Missing paths or folders
without usable images fail required evidence checks. Permission, network, and transport
policy errors require manual review.

## Run

From the workspace root:

```powershell
.\alm-review-web\start-server.ps1
```

The startup script uses the single local application port `8010` and refuses to
start a duplicate server when that port is already occupied.

Open `http://127.0.0.1:8010`.

## Operations

- `Sync ALM now` recursively refreshes the configured Test Lab scope. Only the latest
	Passed Run for each Test Instance is imported by the Worker. The same synchronization reads the ALM
	project user directory and displays people as `Full Name (CODE1 ID)`.
- `Queue re-review` creates new review jobs while retaining all previous results. Scopes
	are available for all Passed Runs, Unqualified only, Manual review only, or the combined
	Unqualified and Manual set.
- Runs already queued or running are skipped when a re-review scope is submitted again.
	The Worker processes queued reviews in the background; queue counts are shown on the
	Dashboard.
- A temporary ALM user-directory failure does not block Run synchronization. Existing
	cached names remain available and CODE1 IDs are used when no name is known.

## Validate

```powershell
Set-Location .\alm-review-web
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

## Review Rules

- `run_id` is the global primary key for the configured ALM project.
- A changed source hash creates an immutable Run revision and queues a new review.
- AI `qualified` is final without manual confirmation.
- AI `needs_manual_review` accepts qualified or unqualified confirmation.
- AI `unqualified` remains unqualified unless an operator records a force-qualified override.
- Manual decisions are bound to the current revision and source hash; a source change invalidates them.

### Equipment registry validation

The controlled equipment registry is available at `/ops/equipment`. Master Calibration
List `.xlsx` files are upserted by case-insensitive Equipment ID; imports retain source
workbook, sheet, and row provenance.

Each review checks Step Description, Expected, Actual, and the Step execution date against
the current registry snapshot:

- Exact Equipment ID matching has priority, followed by exact full or component serial matching.
- `NA` and `N/A` serial values, plain words from composite serial descriptions, and DUT serials
	do not create deterministic equipment matches.
- Explicit unknown equipment IDs, conflicting Equipment ID/serial combinations, incorrect
	reported calibration ranges, and execution outside the inclusive registry calibration range
	fail the equipment criterion.
- A current non-use equipment status is retained as a warning because it does not prove the
	device status on the historical execution date.
- Ambiguous DUT/controlled-equipment roles use a separate constrained AI request. The model may
	only select candidate Equipment IDs supplied from the registry and cannot invent an ID.
- Equipment results and the registry fields used for the decision are stored in each Step result.
	Registry changes update the review policy key and queue affected Passed Runs for re-review.

ALM is currently configured with an HTTP URL. Credentials and ALM content therefore
travel without TLS unless the server is moved behind HTTPS or a trusted encrypted tunnel.