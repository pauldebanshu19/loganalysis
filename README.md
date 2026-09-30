# Log Analyzer

Production systems write log files continuously. When something goes wrong, the
first question is always the same: **which component is failing, and how badly?**

This project answers that question. Give it a log file and it returns the error
count for every service, the service with the most errors, how many lines were
processed, and how many could not be parsed. It has three parts:

| Part | Where | What it does |
|---|---|---|
| 1. Analyzer | [`app/services/`](app/services/) | Reads log lines and produces the summary. Plain Python: no web framework, no I/O. |
| 2. HTTP server | [`app/`](app/) | A REST API (FastAPI). A client submits a log file and receives the analysis as JSON. |
| 3. Client | [`client/logscan.py`](client/logscan.py) | A command-line program. Reads a log file from disk, sends it to the server, prints a readable summary. |

How the parts fit together, and why they are built the way they are, is covered
in [System architecture](#system-architecture).

## Contents

1. [Features](#features)
2. [System architecture](#system-architecture)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Running the server](#running-the-server)
6. [Using the client](#using-the-client)
7. [Demo: what to show in the terminal](#demo-what-to-show-in-the-terminal)
8. [HTTP API reference](#http-api-reference)
9. [How log lines are parsed](#how-log-lines-are-parsed)
10. [How the summary is calculated](#how-the-summary-is-calculated)
11. [Design decisions](#design-decisions)
12. [Configuration](#configuration)
13. [Sample log files](#sample-log-files)
14. [Running the tests](#running-the-tests)
15. [Project layout](#project-layout)
16. [Troubleshooting](#troubleshooting)
17. [Known limitations](#known-limitations)

## Features

- **Error count for every service**, including services with no errors, sorted
  worst first.
- **Top offender**: the service with the most errors. Ties are reported, not
  hidden.
- **Lines processed and unparseable lines**, with the first 20 bad lines returned
  along with their line number and the reason they could not be parsed.
- **Handles large files in constant memory.** The upload is parsed as it arrives
  and never held in memory, so a 100 MB file uses no more memory than a 1 KB one.
- **Tolerant parsing**: tabs or repeated spaces between fields, Windows (CRLF)
  line endings, trailing whitespace and invalid UTF-8 are all handled.
- **RESTful API**: create an analysis with `POST`, fetch it again with `GET`,
  remove it with `DELETE`. Interactive documentation is built in.
- **Clear errors**: every failure returns the same JSON shape, with a stable
  error code and a request id that matches the server's log.
- **Protected under load**: a per-client rate limit, a cap on concurrent
  analyses, an upload size limit and an upload timeout.
- **A client built for scripts as well as people**: automatic retries, a JSON
  output mode, and exit codes that say what went wrong.
- **279 automated tests**, none of which need a running server.

## System architecture

### Overview

The system is a command-line client and one HTTP service. Inside the service,
each layer only calls the layer below it. The web layer receives requests, the
analyzer does the counting, and the store keeps the answers.

```text
  ┌─────────────────────────────────────────────┐
  │ Clients                                     │
  │   client/logscan.py (Part 3)                │
  │   curl, or a browser on /docs               │
  └──────────────────────┬──────────────────────┘
                         │  HTTP + JSON
                         │  POST, GET, DELETE /api/v1/analyses
                         ▼
  ┌─────────────────────────────────────────────┐
  │ uvicorn: HTTP server on port 8000           │
  └──────────────────────┬──────────────────────┘
                         │  ASGI
                         ▼
  ┌─────────────────────────────────────────────┐
  │ Middleware                    app/utils/    │
  │   request id, access log, metrics           │
  │   rate limit: 30 requests/min per IP        │
  ├─────────────────────────────────────────────┤
  │ API layer (Part 2)            app/api/      │
  │   POST, GET, DELETE /api/v1/analyses        │
  │   GET /api/v1/health, GET /metrics          │
  │   optional API key, 8 upload slots          │
  ├─────────────────────────────────────────────┤
  │ Upload reader                 app/services/ │
  │   streams the body, cuts it into lines      │
  ├─────────────────────────────────────────────┤
  │ Analyzer (Part 1)             app/services/ │
  │   parser: a line → fields, or a reason      │
  │   analyzer: counts, top offender, samples   │
  ├─────────────────────────────────────────────┤
  │ Result store                  app/storage/  │
  │   the summary, kept for 1 hour              │
  └──────────────────────┬──────────────────────┘
```

Settings (`app/config.py`), the error format, logging and metrics are used by
every layer.

### Components

| Component | Code | Responsibility |
|---|---|---|
| Client | [`client/logscan.py`](client/logscan.py) | Checks the file locally, streams it to the API, retries on failure, and prints the summary. It talks to the server only over HTTP and imports nothing from `app/`. |
| HTTP server | uvicorn | Accepts connections, speaks HTTP, and calls the application through ASGI, the standard interface between Python web servers and apps. |
| Application | [`app/main.py`](app/main.py) | Builds the FastAPI app at startup: settings, middleware, routes, error handlers, and the objects every request shares (store, slots, rate limiter). Closes them at shutdown. |
| Middleware | [`app/utils/middleware.py`](app/utils/middleware.py), [`app/utils/rate_limit.py`](app/utils/rate_limit.py) | Wraps every request. It assigns a request id, writes one access log line, records metrics, and turns away clients over the rate limit before any of the body is read. |
| API | [`app/api/analyses.py`](app/api/analyses.py), [`app/api/health.py`](app/api/health.py), [`app/utils/security.py`](app/utils/security.py), [`app/utils/slots.py`](app/utils/slots.py) | The REST endpoints. Validates input, checks the API key when one is configured, and caps how many uploads are analysed at once. |
| Upload reader | [`app/services/upload.py`](app/services/upload.py) | Reads the request body as it arrives, enforces the size limit, idle timeout and binary check, and splits the bytes into lines. |
| Analyzer | [`app/services/parser.py`](app/services/parser.py), [`app/services/analyzer.py`](app/services/analyzer.py) | Parses each line and keeps a counter per service and level, then builds the summary. It contains no web or file code, so it can be used on its own. |
| Result model | [`app/models/analysis.py`](app/models/analysis.py) | `AnalysisResult`: the JSON shape shared by the API and the client. |
| Result store | [`app/storage/store.py`](app/storage/store.py) | Keeps each summary for one hour, in memory or in Redis, behind one interface. |
| Shared utilities | [`app/config.py`](app/config.py), [`app/utils/errors.py`](app/utils/errors.py), [`app/utils/logger.py`](app/utils/logger.py), [`app/utils/metrics.py`](app/utils/metrics.py) | Settings from the environment and `.env`, the single error format, JSON logs, and Prometheus metrics. |

### Request flow

What happens when a file is analysed, from the command to the printed table:

1. **The client** checks that the file exists and is at most 100 MB, then streams
   it as `multipart/form-data` to `POST /api/v1/analyses`.
2. **uvicorn** receives the request and calls the FastAPI application.
3. **The request context middleware** gives the request an id, or keeps the one
   the client sent in `X-Request-ID`, and starts a timer.
4. **The rate limit middleware** counts this IP address's requests in the current
   minute. Over 30 returns `429`.
5. **FastAPI** routes the request to `create_analysis`, validates the `samples`
   parameter, and runs the API key check.
6. **The route takes one of 8 analysis slots.** If none frees up within 5 seconds,
   it returns `503`.
7. **The upload reader** reads the body chunk by chunk as it arrives. It rejects
   a binary file (`415`), a file over 100 MB (`413`) and a stalled upload
   (`408`), and splits the bytes into lines.
8. **The analyzer** matches each line against the log format and adds 1 to that
   service's counter for that level. Lines that do not match are counted as
   unparseable, and the first 20 are kept with the reason.
9. **At the end of the file,** the analyzer builds the result: services sorted
   worst first, the top offender(s), the time range and the metadata. The slot
   is released.
10. **The summary is stored** for one hour under a new id. The log itself is
    discarded.
11. **The response** is `201 Created`, with a `Location` header and the JSON
    body. On the way out, the middleware adds `X-Request-ID`, writes one JSON log
    line and updates the metrics.
12. **The client** prints the summary table and exits with code 0.

`GET` and `DELETE /api/v1/analyses/{id}` take a shorter path: middleware, route,
then a lookup or removal in the store. Any failure, at any step, becomes the
same JSON error body described in [Errors](#errors).

### How a large file stays in constant memory

The upload is never held whole, in memory or on disk. The server keeps at most
one 256 KB block and one unfinished line at a time:

```text
 bytes arriving from the network
        │
        ▼
 event loop ───── reads chunks as they arrive (async, so other requests keep being served)
        │         checks the size limit and the idle timeout
        │         collects chunks into 256 KB blocks
        ▼
 worker thread ── splits each block into lines; an unfinished line waits for the next block
        │         parses each line and adds 1 to its (service, level) counter
        │         then the block is thrown away
        ▼
 counters per service + the first 20 bad lines      ← the only state kept
        │
        ▼
 AnalysisResult, returned as JSON
```

Reading from the network is asynchronous: while the server waits for bytes, it
serves other requests. Parsing is CPU work, so it runs in a worker thread, which
keeps the event loop free.

### Architectural decisions

- **Layers depend in one direction only.** The analyzer knows nothing about HTTP,
  and the client knows only the HTTP API, so each part can change or be reused
  without the others.
- **Streaming, not buffering.** FastAPI's usual `UploadFile` saves the whole
  upload before the route runs. The upload reader parses it as it arrives
  instead, so memory use does not depend on file size.
- **Cheap checks first.** The rate limit runs before the body is read, and the
  declared size (`Content-Length`) is checked before the first byte is read, so
  a rejected request costs almost nothing.
- **Bounded concurrency.** At most 8 uploads are analysed at once. Extra
  requests get a clear `503` with `Retry-After` instead of slowing everything
  down or running out of memory.
- **Storage behind an interface.** The routes only know "put, get, delete". The
  in-memory store or Redis is chosen by configuration, with no code change.
- **One error format.** Every failure, from any layer, returns the same JSON
  body with a stable `code`.
- **Traceable requests.** The request id is in every response and every server
  log line, so a user's error report leads straight to the matching log entry.

### Deployment and scaling

```text
 Default: uvicorn main:app --port 8000

    ┌───────────────────────────────┐
    │ uvicorn, 1 process            │
    │ results and rate limits kept  │
    │ in the process's own memory   │
    └───────────────────────────────┘

 Scaled: REDIS_URL set, uvicorn main:app --workers 4

    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │ worker 1 │  │ worker 2 │  │ worker N │
    └────┬─────┘  └────┬─────┘  └────┬─────┘
         └─────────────┼─────────────┘
                       ▼
                 ┌───────────┐
                 │   Redis   │  results + rate-limit counts
                 └───────────┘
```

By default the server is one process and needs nothing else installed. To use
more CPU cores, run several worker processes and set `REDIS_URL`. Each process
has its own memory, so without Redis a result stored by one worker could not be
fetched from another. With Redis, the workers keep no state of their own, and
more can be added, on one machine or behind a load balancer, without changing
any code.

### Technology stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | FastAPI | Validates input and generates the `/docs` page from type hints. |
| HTTP server | uvicorn | A fast ASGI server. |
| Data models and settings | Pydantic, pydantic-settings | Typed models that convert to and from JSON; settings read and validated from the environment. |
| Streaming uploads | streaming-form-data | Parses a multipart upload as it arrives, instead of saving it first. |
| Async and threads | anyio (installed with FastAPI) | Hands parsing to a worker thread and enforces the upload timeout. |
| Metrics | prometheus-client | Counters and timings served at `/metrics`. |
| Shared store (optional) | redis | Results and rate limits shared between worker processes. |
| Client HTTP | httpx | Streams the file from disk; timeouts and clear connection errors. |
| Client interface | Typer, Rich | Command-line options from the function's parameters; formatted output. |

## Requirements

- **Python 3.12 or newer.** Check with `python --version`.
- **pip**, which comes with Python.
- Optional: **curl**, to call the API by hand. It is included with Windows 10
  and later as `curl.exe`.
- Optional: **Redis**, only if you run the server with more than one worker.
  See [Configuration](#configuration).

## Installation

From the project folder, create a virtual environment and install the
dependencies. This installs everything the server, the client and the tests
need.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

When the environment is active, your prompt starts with `(.venv)`. Activate it
again in every new terminal you open. If PowerShell refuses to run the activate
script, see [Troubleshooting](#troubleshooting).

No other setup is needed. The server runs with sensible defaults; a `.env` file
is only needed to change them.

## Running the server

```bash
uvicorn main:app --port 8000
```

Leave this terminal open; the server runs until you press `Ctrl+C`. Add
`--reload` while editing code, and the server restarts on every change.

The server logs in JSON, one object per line. At startup it prints its
settings, then one line for every request it serves:

```text
{"ts": "...", "level": "INFO", "logger": "app.main", "message": "startup", "env": "development", "store": "memory", "slots": 8, "max_upload_mb": 100, "auth": false}
{"ts": "...", "level": "INFO", "logger": "uvicorn.error", "message": "Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)"}
{"ts": "...", "level": "INFO", "logger": "app.access", "message": "request", "request_id": "req_a1ac6ecd", "method": "POST", "route": "/api/v1/analyses", "path": "/api/v1/analyses", "status": 201, "duration_ms": 3.1, "client_ip": "127.0.0.1", "analysis_id": "an_01M3SVT2FNR21GNT", "bytes": 410, "lines_processed": 7, "unparseable_lines": 1, "services": 3}
```

The contents of uploaded logs are never written to the server's log.

Check the server is up:

```bash
curl http://localhost:8000/api/v1/health
```

```json
{"status": "ok", "store_reachable": true, "slots": {"in_use": 0, "capacity": 8}, "analyzer_version": "1.0.0"}
```

While the server is running, interactive API documentation is at
<http://localhost:8000/docs>. You can upload a file and try every endpoint
from the browser there.

## Using the client

In a second terminal, with the virtual environment active:

```bash
python client/logscan.py samples/sample.log
```

```text
Uploading: samples/sample.log

Job completed.

=================================================================
                    LOG ANALYSIS RESULT
=================================================================

Lines processed   : 7
Unparseable lines : 1

Service Name                         Error count
-----------------------------------------------------------------
payment-service                                2
billing-service                                1
auth-service                                   0

-----------------------------------------------------------------

Top offender      : payment-service


Processing time   : 2.45 ms
=================================================================
```

What each part means:

| Line | Meaning |
|---|---|
| `Lines processed` | Every non-blank line in the file, whether it could be parsed or not. |
| `Unparseable lines` | Lines that do not match the log format. Here, the last line has no date and time. |
| Service table | Every service seen in the file and its error count (`ERROR` + `FATAL` lines), worst first. |
| `Top offender` | The service with the most errors. Several names are shown when there is a tie, and `none` when there are no errors at all. |
| `Processing time` | How long the server spent analysing the file. It varies from run to run and does not include network time. |

When two services tie, both are named:

```text
Top offender      : billing-service, payment-service (tied, 3 errors each)
```

### Options

```text
python client/logscan.py LOGFILE [OPTIONS]
```

| Option | Default | What it does |
|---|---|---|
| `--server URL` | `http://localhost:8000` | Address of the server. Can also be set with the `LOGSCAN_SERVER` environment variable. |
| `--show-unparseable` | off | Also list the unparseable lines, with line number and reason. |
| `--json` | off | Print the server's raw JSON instead of the summary. |
| `--samples N` | server default (20) | How many unparseable lines to ask for, from 0 to 100. |
| `--fail-on-errors` | off | Exit with code 4 if any service has errors. Useful in CI pipelines. |
| `--timeout SEC` | 120 | Give up on a request after this many seconds. |
| `--retries N` | 3 | How many times to retry a failed or timed-out connection, a `429`, or a `5xx`. |
| `--api-key KEY` | none | Sent as the `X-API-Key` header, for a server that requires one. Can also be set with `LOGSCAN_API_KEY`. |
| `--no-color` | off | Plain output. Colour is also off when output is redirected, or when `NO_COLOR` is set. |
| `-h`, `--help` | | Show all options. |

### Examples

**See which lines could not be parsed, and why:**

```bash
python client/logscan.py samples/edge-cases.log --show-unparseable
```

```text
...
Top offender      : payment-service

Unparseable lines (showing 5 of 5):
  line  6  invalid_timestamp: 2026-02-30 25:10:00 ERROR payment-service impossible date and time
  line  7  unknown_level: 2026-09-18 10:23:49 WARNING auth-service level outside the five
  line  8  unknown_level: 2026-09-18 10:23:50 error auth-service lowercase level
  line  9  missing_timestamp: ERROR billing-service No Auth token
  line 10  missing_service: 2026-09-18 10:23:51 ERROR

Processing time   : 1.00 ms
=================================================================
```

**Get the raw JSON**, for example to save it or pipe it into another tool. The
`Uploading:` and `Job completed.` lines go to stderr in this mode, so the
output is valid JSON:

```bash
python client/logscan.py samples/sample.log --json > result.json
```

**Use it in a script or CI job**, failing the step when any service logged errors:

```bash
python client/logscan.py app.log --fail-on-errors
echo $?          # PowerShell: $LASTEXITCODE
```

**Talk to a server somewhere else:**

```bash
python client/logscan.py app.log --server http://logs.example.internal:8000
```

### Exit codes

| Code | Meaning | Example |
|---|---|---|
| 0 | The summary was printed. | |
| 1 | A local problem, found before anything was sent. | `logscan: samples/nope.log: no such file` |
| 2 | The server rejected the request (a `4xx` error). | An empty file: `logscan: The uploaded file is empty.` |
| 3 | The server could not be reached, timed out, or failed (`5xx`). | The server is not running. |
| 4 | `--fail-on-errors` was set and at least one service had errors. | |

When the server rejects a request, the client prints the reason, the error code
and the request id. The request id lets you find the matching line in the
server's log:

```text
Uploading: samples/empty.log
logscan: The uploaded file is empty.
  code: empty_file  status: 400
  request id: req_7dcefedc
```

When the server cannot be reached, the client retries with increasing waits
(0.5 s, 1 s, 2 s) before giving up:

```text
Uploading: samples/sample.log
logscan: could not connect (ConnectError); retrying in 0.5s (1)
logscan: could not connect (ConnectError); retrying in 1s (2)
logscan: could not connect (ConnectError); retrying in 2s (3)
logscan: http://localhost:8000: could not connect (ConnectError)
```

If the server says how long to wait (a `Retry-After` header on a `429` or
`503`), the client waits that long instead, up to 60 seconds. In a terminal,
files over 5 MB show an upload progress bar.

## Demo: what to show in the terminal

This walkthrough covers all three parts of the brief, in order. Commands are
written for Windows PowerShell; on macOS or Linux, use `curl` instead of
`curl.exe`.

**Terminal 1: start the server and leave it running.** It prints one log line
for every request in the steps below.

```powershell
.venv\Scripts\activate
uvicorn main:app --port 8000
```

**Terminal 2:**

| # | Command | What it shows |
|---|---|---|
| 1 | `python client/logscan.py samples/sample.log` | Part 3: the summary, in the format the brief asks for. |
| 2 | `python client/logscan.py samples/edge-cases.log --show-unparseable` | Part 1: bad lines are counted, not dropped, each with its line number and reason. |
| 3 | `python client/logscan.py samples/sample.log --json` | Part 2: the raw JSON the server returns. |
| 4 | `curl.exe -i -F "file=@samples/sample.log" http://localhost:8000/api/v1/analyses` | Part 2 without the client: `201 Created`, a `Location` header and the JSON body. |
| 5 | `curl.exe http://localhost:8000/api/v1/analyses/<id>` | Fetching the same result again, using the `id` from step 4. |
| 6 | `curl.exe -i -X DELETE http://localhost:8000/api/v1/analyses/<id>` | Deleting it: `204 No Content`. Repeating step 5 now returns `404`. |
| 7 | `python client/logscan.py samples/empty.log` | Error handling: a clear message, the error code and a request id. Exit code 2. |
| 8 | Stop the server with `Ctrl+C`, then repeat step 1 | The client retries, then reports it could not connect. Exit code 3. |
| 9 | `pytest` | 279 tests passing. |

Optionally, open <http://localhost:8000/docs> in a browser to show the
interactive API documentation.

## HTTP API reference

All endpoints are under `http://localhost:8000` by default.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/api/v1/analyses` | Analyse a log file. | `201 Created` |
| `GET` | `/api/v1/analyses/{id}` | Fetch a stored result. | `200 OK` |
| `DELETE` | `/api/v1/analyses/{id}` | Delete a stored result. | `204 No Content` |
| `GET` | `/api/v1/health` | Is the server up, and can it store results? | `200 OK` |
| `GET` | `/metrics` | Prometheus metrics. | `200 OK` |
| `GET` | `/docs` | Interactive documentation (Swagger UI). | `200 OK` |
| `GET` | `/openapi.json` | The machine-readable API description. | `200 OK` |

### `POST /api/v1/analyses`: analyse a log file

The log can be sent in either of two ways.

**As a file upload** (`multipart/form-data`, file in a field named `file`). This
is what the client and the `/docs` page send:

```bash
curl -F "file=@samples/sample.log" http://localhost:8000/api/v1/analyses
```

**As a plain request body** (`text/plain`), with an optional `X-Filename` header
to name it:

```bash
curl --data-binary @samples/sample.log \
     -H "Content-Type: text/plain" -H "X-Filename: sample.log" \
     http://localhost:8000/api/v1/analyses
```

Optional query parameter:

| Parameter | Range | Default | Meaning |
|---|---|---|---|
| `samples` | 0 to 100 | 20 | How many unparseable lines to return as examples. `0` returns none; they are still counted. |

Optional request headers:

| Header | Meaning |
|---|---|
| `X-API-Key` | Required only when the server has `API_KEYS` set. |
| `X-Request-ID` | Your own id for the request. It is echoed back and written to the server log. If you do not send one, the server makes one up. |
| `X-Filename` | Names a `text/plain` upload. Multipart uploads use the file's own name. |

**Response: `201 Created`.** The `Location` header gives the result's address,
for example `Location: /api/v1/analyses/an_01M3SVT2FNR21GNT`. The body for
`samples/sample.log`:

```json
{
  "id": "an_01M3SVT2FNR21GNT",
  "lines_processed": 7,
  "unparseable_lines": 1,
  "blank_lines": 0,
  "services": [
    {
      "service": "payment-service",
      "error_count": 2,
      "error_rate": 0.6667,
      "levels": { "DEBUG": 0, "INFO": 1, "WARN": 0, "ERROR": 2, "FATAL": 0 }
    },
    {
      "service": "billing-service",
      "error_count": 1,
      "error_rate": 1.0,
      "levels": { "DEBUG": 0, "INFO": 0, "WARN": 0, "ERROR": 1, "FATAL": 0 }
    },
    {
      "service": "auth-service",
      "error_count": 0,
      "error_rate": 0.0,
      "levels": { "DEBUG": 0, "INFO": 1, "WARN": 1, "ERROR": 0, "FATAL": 0 }
    }
  ],
  "top_offenders": ["payment-service"],
  "unparseable_samples": [
    { "line_number": 7, "reason": "missing_timestamp", "text": "error billing-service No Auth token" }
  ],
  "time_range": { "first": "2026-09-18T10:23:45", "last": "2026-09-18T10:24:30" },
  "meta": { "filename": "sample.log", "bytes": 410, "duration_ms": 2.45, "analyzer_version": "1.0.0" }
}
```

Response fields:

| Field | Meaning |
|---|---|
| `id` | The result's id. Use it with `GET` or `DELETE`. |
| `lines_processed` | Every non-blank line read, parsed or not. |
| `unparseable_lines` | Lines that broke a parsing rule. |
| `blank_lines` | Empty or whitespace-only lines. Not counted as processed. |
| `services[].service` | The service name, exactly as it appeared in the log. |
| `services[].error_count` | `ERROR` plus `FATAL` lines for this service. |
| `services[].error_rate` | `error_count` divided by this service's parsed lines, from 0.0 to 1.0. |
| `services[].levels` | How many lines of each level this service logged. |
| `top_offenders` | The service or services with the most errors. Empty when there are no errors. |
| `unparseable_samples` | The first N bad lines: line number in the file, reason code, and the line itself (cut at 500 characters). |
| `time_range` | The earliest and latest timestamps in the file. Both are `null` if no line parsed. |
| `meta.filename` | The uploaded file's name, cleaned of any path. |
| `meta.bytes` | The size of the uploaded log. |
| `meta.duration_ms` | Time spent analysing, in milliseconds. |
| `meta.analyzer_version` | Version of the analysis rules. |

### `GET /api/v1/analyses/{id}`: fetch a stored result

Returns the same body as the `POST` that created it, with `200 OK`. Results are
kept for one hour (`RESULT_TTL_S`), then removed.

```bash
curl http://localhost:8000/api/v1/analyses/an_01M3SVT2FNR21GNT
```

### `DELETE /api/v1/analyses/{id}`: delete a stored result

Removes the result before it would expire. Returns `204 No Content` with an
empty body. Deleting an id that does not exist, has expired, or was already
deleted returns `404`.

```bash
curl -i -X DELETE http://localhost:8000/api/v1/analyses/an_01M3SVT2FNR21GNT
```

### `GET /api/v1/health`: health check

Always returns `200` while the server is running. `status` is `degraded`
instead of `ok` when the result store is unreachable, which can only happen
when Redis is in use.

```json
{"status": "ok", "store_reachable": true, "slots": {"in_use": 0, "capacity": 8}, "analyzer_version": "1.0.0"}
```

`slots` shows how many analyses are running now, out of how many are allowed
at once.

### `GET /metrics`

Counters and timings in Prometheus format: requests by route and status,
request durations, errors by code, lines and bytes analysed, analysis time and
slot usage.

### Errors

Every error, from every endpoint, has the same shape:

```json
{
  "error": {
    "code": "analysis_not_found",
    "message": "No analysis with that id. Results are kept for a limited time and then expire.",
    "details": { "analysis_id": "an_01M3SVT2GFD9H9P3" },
    "request_id": "req_dacbdbe4"
  }
}
```

- `code` is stable and safe to program against. `message` is for people and
  may be reworded.
- `details` carries specifics where there are any: the size limit, the invalid
  parameter, and so on.
- `request_id` matches the `X-Request-ID` response header and the server's log
  line for that request.

| Status | `code` | When |
|---|---|---|
| 400 | `missing_file` | A multipart upload with no field named `file`. |
| 400 | `empty_file` | The file has no bytes. |
| 401 | `unauthorized` | `API_KEYS` is set and `X-API-Key` is missing or wrong. |
| 404 | `analysis_not_found` | Unknown, expired or deleted id, or an unknown path. |
| 405 | | The path exists but not with that method, e.g. `PUT`. The message is `Method Not Allowed`. |
| 408 | `upload_timeout` | The client stopped sending data for 30 seconds. |
| 413 | `file_too_large` | The file is over 100 MB. |
| 415 | `unsupported_media_type` | The body is not `multipart/form-data` or `text/plain`, or the file is binary (a NUL byte in its first 8 KB). |
| 422 | `validation_error` | A query parameter is invalid, e.g. `?samples=500`. |
| 429 | `rate_limited` | More than 30 requests in a minute from one IP address. |
| 500 | `internal_error` | An unexpected failure. Details are logged on the server, not returned. |
| 503 | `server_busy` | Every analysis slot stayed busy for 5 seconds. |

Errors worth retrying (`408`, `429`, `503`) include a `Retry-After` header
saying how many seconds to wait.

A bad line inside a log is **not** an error. It is counted in
`unparseable_lines`, and the request still succeeds.

## How log lines are parsed

Each line must have this format:

```text
2026-09-18 10:23:45 ERROR payment-service Connection timeout after 30s
└── date ─┘ └time─┘ └level┘ └─ service ──┘ └────── message ───────┘
```

- **date** is `YYYY-MM-DD` and must be a real date.
- **time** is `HH:MM:SS` on a 24-hour clock and must be a real time.
- **level** is one of `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`, in upper case.
- **service** is any run of non-space characters.
- **message** is everything after the service. It may be empty.

A line that breaks a rule is **counted, not dropped**. Each gets one reason:
the first rule it breaks, reading left to right.

| Reason | The line... | Example |
|---|---|---|
| `missing_timestamp` | does not start with a date and time | `error billing-service No Auth token` |
| `invalid_timestamp` | has a date or time that does not exist | `2026-02-30 25:10:00 ERROR payment-service ...` |
| `unknown_level` | has no level, or a level outside the five | `2026-09-18 10:23:49 WARNING auth-service ...` |
| `missing_service` | stops after the level | `2026-09-18 10:23:51 ERROR` |
| `line_too_long` | is longer than 64 KB | |

Also accepted:

- tabs, or several spaces, between fields;
- Windows (CRLF) line endings;
- trailing spaces;
- invalid UTF-8 bytes, which are replaced with `�` so the rest of the line still
  parses.

Not accepted, on purpose:

- lowercase or mixed-case levels (`error`, `Warn`), which are `unknown_level`;
- `WARNING`, which is not one of the five levels;
- leading spaces before the date.

## How the summary is calculated

- **Lines processed** counts every non-blank line: the ones that parsed plus the
  ones that did not.
- **Blank lines** are counted separately and are not "processed". They still
  count towards line numbers, so a line number in the output matches the line
  in your editor.
- **Error count** for a service is its `ERROR` lines plus its `FATAL` lines.
- **Error rate** is the error count divided by that service's parsed lines.
- **Top offender** is every service that shares the highest error count. If no
  service has any errors, there is no top offender.
- **Services are sorted** by error count, highest first, then by name.
- **Time range** is the earliest and latest timestamp seen, not the first and
  last lines, because logs from several sources are not always in order.

## Design decisions

These are the places where the brief left room for interpretation, and what
was chosen.

| Question | Decision | Why |
|---|---|---|
| Is `FATAL` an error? | Yes, it counts towards the error count. | A fatal error is a worse error, not a different category. |
| Is `error` the same as `ERROR`? | No. Lowercase levels are unparseable. | The brief defines the levels in upper case. Guessing would hide malformed logs. The line is still counted and reported. |
| Should services with no errors be listed? | Yes. | "Which component is failing" is easier to answer when you can also see which ones are not. |
| What if two services tie? | Both are reported. | Picking one would be arbitrary and could hide a problem. |
| Do blank lines count as processed? | No. They are reported as `blank_lines`. | They are neither log entries nor broken ones, and a trailing newline should not change the count. |
| What does an empty file return? | `400 empty_file`. | Sending nothing is almost certainly a mistake, and a summary of zeros would hide it. |
| Is the log stored? | No. Only the summary, for one hour. | Logs can contain sensitive data. The summary is enough to fetch the result again. |
| How are large files handled? | Streamed and parsed chunk by chunk. | Memory use stays flat whatever the file size. |

## Configuration

The server works without any configuration. To change a setting, set an
environment variable of the same name, or put it in a `.env` file in the
project folder. [`.env.example`](.env.example) lists every setting with its
default.

| Setting | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | `100` | Largest file accepted. Bigger files get `413`. |
| `MAX_LINE_KB` | `64` | Longest line accepted. Longer lines are counted as `line_too_long`. |
| `UPLOAD_IDLE_TIMEOUT_S` | `30` | Cancel an upload that sends nothing for this many seconds (`408`). |
| `MAX_CONCURRENT_ANALYSES` | `8` | How many uploads are analysed at once. |
| `SLOT_WAIT_S` | `5` | How long a request waits for a free slot before getting `503`. |
| `RATE_LIMIT_PER_MIN` | `30` | Requests per minute per client IP. `0` turns the limit off. |
| `RESULT_TTL_S` | `3600` | How long a result can be fetched by id, in seconds. |
| `DEFAULT_SAMPLES` | `20` | Unparseable lines returned when the request does not say. |
| `MAX_SAMPLES` | `100` | The most unparseable lines a request can ask for. |
| `API_KEYS` | empty | Comma-separated list of accepted keys. When set, every request needs a matching `X-API-Key` header. |
| `REDIS_URL` | unset | Store results in Redis instead of memory. Needed only when running several workers. |
| `LOG_LEVEL` | `INFO` | Server log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `ENV` | `development` | A label shown in the startup log line. |

**Example: require an API key.** In `.env`:

```text
API_KEYS=my-secret-key
```

Restart the server. Requests without the key now get `401`. The client sends it
with `--api-key my-secret-key`, or with the `LOGSCAN_API_KEY` environment
variable.

**Example: change a setting for one run** without editing `.env`:

```powershell
$env:RATE_LIMIT_PER_MIN = "0"; uvicorn main:app --port 8000     # PowerShell
```

```bash
RATE_LIMIT_PER_MIN=0 uvicorn main:app --port 8000               # macOS / Linux
```

The client has its own settings: `LOGSCAN_SERVER`, `LOGSCAN_API_KEY` and
`NO_COLOR` (see [Options](#options)).

## Sample log files

The [`samples/`](samples/) folder has a file for each situation worth trying:

| File | What it contains | Result |
|---|---|---|
| `sample.log` | The brief's example, word for word. | 7 processed, 1 unparseable. `payment-service` has 2 errors, `billing-service` 1, `auth-service` 0. |
| `edge-cases.log` | One line per parsing rule: tabs, repeated spaces, `FATAL`, an impossible date, `WARNING`, a lowercase level, a missing timestamp, a missing service, a service name in different case, a blank line. | 10 processed, 5 unparseable, 1 blank. |
| `crlf.log` | Windows line endings. | 2 processed, 0 unparseable. |
| `invalid-utf8.log` | A line containing bytes that are not valid UTF-8. | 1 processed, 0 unparseable. The bad bytes become `�`. |
| `empty.log` | Nothing. | `400 empty_file`. The client exits with code 2. |
| `binary.log` | Binary data, not text. | `415 unsupported_media_type`. The client exits with code 2. |

## Running the tests

From the project folder, with the virtual environment active:

```bash
pytest
```

```text
279 passed in 6.09s
```

The tests do not need the server running. They call the app directly in the
same process, replace Redis with an in-memory fake, and replace the client's
network call with a canned response.

Useful variations:

```bash
pytest -v                       # one line per test, with its name
pytest tests/test_parser.py     # one file
pytest -k delete                # only tests with "delete" in their name
pytest -x                       # stop at the first failure
```

What each test file covers:

| File | Covers |
|---|---|
| `tests/test_parser.py` | Every parsing rule, plus the accepted variations (tabs, CRLF, trailing spaces). |
| `tests/test_analyzer.py` | The brief's sample end to end, ties, empty input, combining partial results. |
| `tests/test_invariants.py` | Randomly generated logs (Hypothesis): every line is accounted for, level counts add up, errors are exactly the `ERROR` and `FATAL` lines. |
| `tests/test_upload.py` | The streaming reader: the answer is the same however the upload is split into chunks, including splits inside a line ending or a multi-byte character. |
| `tests/test_api.py` | Every endpoint, every error code, request ids, health, metrics, and the upload size limit. |
| `tests/test_concurrency.py` | Many uploads at once, and `503` when every slot is busy. |
| `tests/test_store.py` | Storing, fetching, deleting and expiring results, in memory and in Redis. |
| `tests/test_infra.py` | Rate limiting, JSON log format, and reading settings. |
| `tests/test_cli.py` | The client's printed summary, compared character for character with the expected format, plus ties, empty results and `--json`. |

## Project layout

```text
app/                    the server: Parts 1 and 2
  main.py               builds the FastAPI application
  config.py             every setting, with its default
  api/
    analyses.py         POST, GET and DELETE /api/v1/analyses
    health.py           GET /api/v1/health
  models/
    analysis.py         AnalysisResult: the JSON the API returns
  services/
    parser.py           Part 1: one line in, parsed fields or a reason out
    analyzer.py         Part 1: counts per service, top offender, samples
    upload.py           reads the upload in chunks and feeds the analyzer
  storage/
    store.py            keeps results for an hour, in memory or in Redis
  utils/
    errors.py           the error codes and the shared error format
    logger.py           JSON logging
    metrics.py          Prometheus metrics
    middleware.py       request ids and the access log
    rate_limit.py       requests per minute per client
    security.py         the optional API key check
    slots.py            the limit on concurrent analyses
client/
  logscan.py            Part 3: the command-line client
tests/                  the test suite
samples/                example log files
main.py                 lets `uvicorn main:app` run from the project folder
requirements.txt        dependencies for the server, the client and the tests
pytest.ini              test settings
.env.example            every setting, with its default
```

## Troubleshooting

**PowerShell says "running scripts is disabled on this system" when activating.**
Allow local scripts for your user once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.venv\Scripts\activate
```

Or skip activation and call the environment's Python directly:
`.venv\Scripts\python.exe -m uvicorn main:app --port 8000`.

**`uvicorn` or `pytest` is "not recognized", or `ModuleNotFoundError: No module
named 'httpx'` (or `fastapi`, `typer`, ...).** The virtual environment is not
active in this terminal, so `python` is your system Python, which does not have
the project's packages. Your prompt should start with `(.venv)`. Activate it
(every new terminal needs this):

```powershell
.venv\Scripts\activate
```

Or run the environment's Python directly, without activating:

```powershell
.venv\Scripts\python.exe client/logscan.py samples/sample.log
.venv\Scripts\python.exe -m uvicorn main:app --port 8000
.venv\Scripts\python.exe -m pytest
```

**The client says `could not connect` and exits with code 3.** The server is not
running, or is on a different port. Start it in another terminal, or pass
`--server` with the right address.

**`address already in use` when starting the server.** Something else is using
port 8000. Use another port, and point the client at it:

```bash
uvicorn main:app --port 8001
python client/logscan.py samples/sample.log --server http://localhost:8001
```

**`curl` in PowerShell asks for parameters or prints something strange.** In
Windows PowerShell, `curl` is a different command. Type `curl.exe`.

**`429 rate_limited` while testing.** More than 30 requests in a minute came from
your machine. Wait for the number of seconds in `Retry-After`, or start the
server with `RATE_LIMIT_PER_MIN=0`.

**A result id that worked before now returns `404`.** Results expire after an
hour, and results kept in memory are lost when the server restarts.

## Known limitations

- **Results are kept in memory by default**, so they are lost when the server
  restarts, and each worker has its own set. Set `REDIS_URL` to keep them in
  Redis instead.
- **Timestamps have no time zone.** They are compared as written in the log.
- **One log format.** Lines in any other format are counted as unparseable,
  not interpreted.
- **The per-IP rate limit trusts the `X-Forwarded-For` header**, which is right
  behind a load balancer. On a server exposed directly to the internet, clients
  could fake it.
