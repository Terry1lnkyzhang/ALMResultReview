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
.\start-server.ps1 -BindAddress 0.0.0.0
```

Laptop Worker `.env`:

```dotenv
DATABASE_URL=mysql+pymysql://user:password@mysql-host:3306/almreview?charset=utf8mb4
APP_ROLE=worker
SCHEDULER_ENABLED=true
WORKER_ID=my-laptop
WORKER_POLL_SECONDS=5
WORKER_LEASE_SECONDS=900
WORKER_SINGLETON_LEASE_SECONDS=30
ALM_USERNAME=code1-id
ALM_PASSWORD=secret
AI_API_KEY=
```

Start the laptop Worker without exposing a Web port:

```powershell
.\start-worker.ps1
```

Before deploying a version that changes the database schema, stop every old Web and Worker
process connected to the shared MySQL database. Start one upgraded Web process and wait for
startup to complete so it can apply the compatibility migration, then start the remaining Web
processes and the single Worker. MySQL migrations wait at most five seconds for a metadata lock;
if startup reports a metadata-lock timeout, find and stop the remaining old process or transaction
before trying again. This limit prevents a queued `ALTER TABLE` from holding up new Dashboard
queries while an old process keeps a transaction open.

The Dashboard shows the latest Worker heartbeat and polls active re-review progress every
15 seconds. Other lists display newly committed results after a page refresh. ALM sync and
review buttons only queue work; processing continues while the laptop Worker is online.
The Worker must acquire a database singleton lease before starting its scheduler. A second
Worker connected to the same database is rejected while that lease is active, including a
second process on the same computer. The lease is renewed every poll cycle and expires after
an abnormal shutdown so a replacement Worker can take over. Final Review writes and each
incremental Sync commit fence on the same lease row, preventing a Worker that lost ownership
from committing stale work. Job-level row locks and leases still provide interrupted-work
recovery. Restart the Worker after changing the configured daily schedule so it reloads the
Cron trigger.

`Enable daily sync` creates the Workspace's scheduled ALM synchronization. When
`Auto-review after scheduled sync` is also enabled, a successful scheduled folder sync queues
both Runs whose review content was added or changed by that sync and completed Runs marked
`Review update recommended` because they use an older review policy. Manual decisions and
already queued or running Reviews are preserved. Manual Sync and Full resync actions do not
trigger this automatic review batch.

Configuration provides two independent chat-completions endpoints. Each endpoint has its own
URL, model, API key, timeout, enabled state, and `Concurrent AI Reviews` limit from 1 to 4.
The enabled capacities are added together. Each Review is bound to one endpoint for its full
text, HTML, image, equipment, and repair pipeline; when a slot finishes, that same endpoint
immediately claims the next queued Review. Faster endpoints therefore receive more work while
each endpoint stays within its own limit. ALM synchronization remains serial. The Worker reads
the pool at the start of every queue cycle, so capacity and endpoint changes do not require a
restart.

Endpoint outcomes are persisted and shown on the Dashboard. Three consecutive retryable
transport failures cool an endpoint down for 60 seconds; HTTP 401/403 errors block that endpoint
until its connection configuration is corrected and saved. Other enabled endpoints continue
processing during the cooldown. Every Review result records the actual endpoint profile, URL,
and model used.

The shared AI configuration can store an API key for the Worker. The page never displays a
saved key: leave the field blank to keep it, enter a value to replace it, or select the clear
option to remove it. A saved key takes precedence over `AI_API_KEY` in `.env`; the environment
value remains the fallback for existing deployments.

`Enable AI Review processing` enables Endpoint 1; Endpoint 2 has its own enable control. When no
endpoint is enabled, workers do not claim queued Reviews and batch/re-review actions reject new
requests. Enabling an endpoint does not create Review jobs by itself. Auto-reviewed jobs wait in
the queue while all endpoints are disabled or the Workspace Review queue is paused.

Do not expose Uvicorn or MySQL directly to the public Internet. Put the Web server behind
company VPN/internal networking and an HTTPS reverse proxy. `APP_ROLE=web` requires HTTP
Basic credentials, but TLS is still required to protect those credentials in transit.

### External evidence review

External evidence review is disabled by default. `Enable external evidence review` permits
read-only HTML and image access below the approved roots and allows bounded images to be sent to
`image-evidence-review`. The setting directly controls those checks for its Workspace. The
configured AI endpoint is used directly; there is no separate HTTP transport approval setting.

For the complete path-validation, directory-scanning, Step-matching, and Skill call flow, see
[Image evidence review flow](docs/image-evidence-review-flow.md).

The resolver reads only PNG, JPEG, and WebP files, recurses at most two directory levels,
sends at most four images per Step and twelve per Run, and rejects images larger than 5 MB.
The combined image budget is 10 MB per Step and 15 MB per Run.
Image bytes stay in memory and are not stored in the database; review results retain only
the relative filename, media type, byte size, and SHA-256 digest. Missing paths or folders
without usable images fail required evidence checks. Permission, network, and transport
policy errors require manual review.

### Review checkpoints

Each completed review displays nine independent checkpoints:

1. `Language quality`: Actual spelling, grammar, tense, and meaningful formatting issues.
2. `Expected vs actual`: whether Actual clearly answers every applicable Expected requirement.
3. `Screenshot evidence`: required screenshots exist, map to the Step, and support Expected.
4. `Path validation`: evidence uses an absolute UNC path below the approved network root.
5. `HTML report sequence`: related automation reports start with `.html`, then continue as
	`_2.html`, `_3.html`, and so on without missing numbers.
6. `Automation results`: AI locates the sections of each explicitly referenced HTML report that
	cover the current Step, then checks Description, Expected, Actual, and detailed/final result
	consistency. A relevant non-passing result or contradiction fails the checkpoint.
7. `Date validation`: reserved for deterministic execution-date rules; currently not enabled.
8. `Reference data validation`: unresolved phantom or reference data requires manual review.
9. `Equipment traceability`: controlled equipment identity and execution-date calibration validity.

HTML reports are read only when `Enable external evidence review` is enabled. The parser reads at
most 5 MB per file and extracts bounded visible text plus static JSON assigned to `var resultData`
without executing scripts, loading linked content, or modifying the source. When a configured
approved-root path is unavailable, the same validated relative path may be resolved below the
optional fallback root; arbitrary paths and directory searches remain forbidden.
`html-evidence-review` reviews only files explicitly referenced by the ALM Step and may combine
several sections or reports that jointly cover that Step. Missing reports, filename sequence gaps,
and the `checkcontent`, `checkstep`, or `fail` filename markers are unqualified; permission,
network, oversized-file, empty-content, and ambiguous AI results require manual review. A pass or
fail must cite every supplied report, and each report, block, and quoted line is validated against
the application-supplied evidence.

When `Automation release project` is configured for a Workspace, each HTML-bearing ALM Step is
also checked against `atframeworkdb.releasetable`. The application queries by the configured
`ProjectName` and the ALM Test ID, selects the latest release revision, and compares the claimed
script name and validation-document number/revision with the released record. Explicit Test ID or
document conflicts and missing release records are unqualified; database availability and
incomplete claims require manual review. Historical shortened script names are supplied to the
HTML Skill for semantic comparison, but the Skill cannot read the database or override a
deterministic conflict.

## Run

From the workspace root:

```powershell
.\alm-review-web\start-server.ps1
```

The startup script uses the single local application port `8090` and refuses to
start a duplicate server when that port is already occupied. It listens on all
network interfaces by default so other machines on the local network can connect.

Open `http://127.0.0.1:8090` on the same computer, or
`http://<computer-ip>:8090` from another computer on the local network. To restrict
the service to this computer, start it with `-BindAddress 127.0.0.1`.

## Operations

- `Import ALM Word` accepts an ALM Design Verification Record `.docx` as an alternative
	to a live ALM synchronization. It imports every Passed Test Run, creates immutable
	revisions when source content changes, and does not automatically queue AI review.
	Re-importing the same content, even under a different filename, does not create
	duplicate revisions.
- The Word importer reads Test Set, Test Case, Test Run, execution metadata, Step,
	Expected, and Actual text from the standard export layout. UNC paths in Actual text
	continue through the existing evidence pipeline. Embedded Word images are not stored;
	image review still reads approved paths configured under Network image evidence.
- Live ALM synchronization downloads supported run-step image attachments only when external
	evidence review is enabled. Attachments use the authenticated ALM API and enter the same
	bounded visual review as approved network images; they are not treated as UNC paths.
- `Sync ALM now` recursively refreshes the configured Test Lab scope. Only the latest
	Passed or Failed Run for each Test Instance is imported by the Worker. The same synchronization reads the ALM
	project user directory and displays people as `Full Name (CODE1 ID)`.
- `Queue latest ALM changes` queues only current revisions whose AI review content changed
	during the latest completed live ALM synchronization. Already reviewed, queued, or running
	revisions are skipped. Review policy changes alone do not broaden this queue scope.
- `Queue re-review` creates new review jobs while retaining all previous results. Scopes
	are available for all Passed/Failed Runs, Unqualified only, Manual review only, or the combined
	Unqualified and Manual set.
- Runs already queued or running are skipped when a re-review scope is submitted again.
	The Worker processes queued reviews in the background; queue counts are shown on the
	Dashboard.
- `Current Workspace Review` counts each Passed/Failed Run once using its current revision. It
	shows current outcomes together with waiting and running jobs, regardless of which action
	created those jobs.
- Workspace queue controls on the Dashboard can pause new Review claims, resume them,
	or make a Workspace the next priority. Pausing preserves queued jobs and lets an
	already-running job finish safely. Higher numeric priorities run first; jobs within the
	same priority remain FIFO. The queue preview shows the next queued Review jobs across
	Workspaces. Waiting Review jobs can be removed individually or all at once for the
	current Workspace; running jobs continue safely. Persistent Review and Sync pause
	settings are also available in Workspace Configuration.
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
- A changed source hash creates an immutable Run revision without queuing a review.
- Reviews are queued only through the explicit latest ALM changes, Re-review, or Review now actions.
- Isolated extra spaces, normal paragraph blank lines, trailing spaces, and minor indentation
	are ignored. Whitespace formatting is reported only when repeated or extensive enough to
	clearly disrupt reading, sentence continuity, list structure, or understanding.
- AI `qualified` is final without manual confirmation.
- AI `needs_manual_review` accepts qualified or unqualified confirmation.
- AI `unqualified` remains unqualified unless an operator records a force-qualified override.
- Manual decisions are bound to the current revision and source hash; a source change invalidates them.

### Staged review pipeline

Each explicit Review Job builds one plan and completes these stages before saving a result:

1. Normalize ALM Description, Expected, Actual, numbered items, formatting signals, and
	program-detected path and equipment candidates. Every candidate receives a stable ID.
2. Run `alm-text-review` once. It reviews applicability, language quality, completeness, and
	Expected/Actual support, then classifies the semantic role of every supplied candidate ID.
3. Build the specialist plan from the validated first-pass output. ALM image attachments remain
	mandatory checks and are routed directly to image review, while the application retains all
	path-access and final-routing authority.
4. Resolve approved images with deterministic path, file-signature, count, byte, and pixel
	guards. `image-evidence-review` sees only the bounded images granted by the application.
5. Resolve and parse explicitly referenced HTML Reports with deterministic security and size
	guards, then call `html-evidence-review` to judge current-Step coverage and result consistency.
6. Validate equipment against the registry with deterministic rules. A clear first-pass role is
	reused; only an uncertain role invokes `equipment-role`, which may select supplied IDs only.
7. Aggregate all stage outcomes with code: any failure is Unqualified, otherwise any
	manual outcome needs manual review, and all-pass results are Qualified.

Each Review always runs `alm-text-review`. `Enable external evidence review` independently controls
approved HTML parsing and image review; `Enable equipment registry validation` independently
controls registry, calibration, and `equipment-role` checks. When both are disabled, the Review
runs only `alm-text-review` and records both optional stages as `disabled` in the pipeline.

The semantic Skill packages live under `app/review_skills/<skill-id>/`. Each package contains
`skill.toml`, `instructions.md`, `input.schema.json`, `output.schema.json`, and `examples.json`.
Changing semantic rules normally means editing `instructions.md` and examples, then bumping the
manifest version. Contract field changes also require matching Python types and adapters. A
manifest-only package with `status = "planned"` appears in the catalog but cannot be executed.

Each text Review uses one AI call. Image batches and unresolved equipment roles add narrowly
scoped calls only when applicable. Unknown or unreadable external evidence requires manual review
rather than an unbounded AI fallback. Results retain each Skill version, policy/input/output
hashes, capability grants, duration, output, stage status, and total AI call count in the pipeline
trace.

### Skill capabilities

Skills declare required, optional, and forbidden capabilities in `skill.toml`, but they never
execute filesystem, network, or database operations. The application orchestrator decides whether
a Workspace policy grants a capability and executes the corresponding deterministic provider.

- `evidence.path_metadata` supplies detected path text only; it does not permit path access.
- `evidence.image.content` is granted only after the application validates the approved UNC root,
	blocks traversal/reparse points, and enforces image budgets.
- `equipment.registry.candidates` supplies read-only candidate snapshots selected by application
	code. Returned IDs are checked again against that exact whitelist.
- Missing or forbidden capabilities fail before an AI HTTP request. Skill failures never bypass
	path security and are converted to the stage's defined failure or manual-review behavior.

The Configuration page lists loaded packages and declared capabilities. A new Run Review displays
a unified Skill execution trace. Historical Reviews retain the trace format used when they ran.

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
	Registry changes update the review policy key and queue affected Passed/Failed Runs for re-review.

ALM is currently configured with an HTTP URL. Credentials and ALM content therefore
travel without TLS unless the server is moved behind HTTPS or a trusted encrypted tunnel.