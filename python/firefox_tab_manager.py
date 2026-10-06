#!/usr/bin/env python3
"""
firefox_tab_manager.py

Standalone AI-assisted Firefox tab cleanup for macOS + LM Studio.

Firefox must be running with WebDriver BiDi, for example:

  /Applications/Firefox.app/Contents/MacOS/firefox \
    -profile ~/Library/Application\\ Support/Firefox/Profiles/default.iy5 \
    --remote-debugging-port=9222

Requires:
  Python 3.11+
  websockets
  LM Studio for the classify command

Commands:
  classify
  plan REPORT.csv
  status PLAN.csv
  apply PLAN.csv
  apply PLAN.csv --apply

Safety:
  * classify never closes tabs
  * plan never closes tabs
  * apply is a dry run unless --apply is present
  * plan archives every planned closure
  * duplicate groups retain at least one survivor
  * protected pages are never planned for closure
  * apply matches current live tabs to the saved plan before closing
"""

import argparse
import asyncio
import csv
import datetime as dt
import html
import json
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

DEFAULT_MODEL = "qwen/qwen3.8-27b"
DEFAULT_BASE = "http://127.0.0.1:1234/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9222

CLOSING_ACTIONS = {"CLOSE", "ARCHIVE_THEN_CLOSE"}

TRACKING_KEYS = {
    "fbclid", "gclid", "gbraid", "wbraid", "dclid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "vero_id", "yclid",
}

PROTECTED_HOSTS = {
    "mail.google.com",
    "calendar.google.com",
    "docs.google.com",
    "drive.google.com",
    "accounts.google.com",
    "localhost",
    "127.0.0.1",
}

PROTECTED_TOKENS = (
    "/account", "/signin", "/login", "/inbox",
    "webmail", "bank", "wealth", "mychart",
    "booking-details", "boarding", "itinerary",
    "confirmation", "receipt", "checkout", "/cart",
)

SYSTEM_PROMPT = """Classify browser tabs for cleanup.
Return ONLY one compact JSON object with two arrays.
Format exactly as {"close":[ids], "review":[ids]}.
Any input id not listed is implicitly KEEP.
Do not give reasons or any other text.

Be conservative.
KEEP useful research, articles, documentation, code, academic material,
ongoing work, references, bookings, events, account pages, and purchases.
CLOSE only clearly disposable, completed, obsolete, generic, transient,
promotional, low-value, error, stale search, or blank/navigation pages.
REVIEW whenever uncertain."""


class Bidi:
    def __init__(self, host, port):
        self.uri = f"ws://{host}:{port}/session"
        self.ws = None
        self.next_id = 1

    async def __aenter__(self):
        self.ws = await websockets.connect(
            self.uri,
            open_timeout=10,
            max_size=32 * 1024 * 1024,
        )
        await self.command("session.new", {"capabilities": {}})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.ws is not None:
            try:
                # Firefox permits only one active WebDriver BiDi session.
                # End it explicitly so later classify/status/apply commands
                # can create a fresh session without restarting Firefox.
                await self.command("session.end", {})
            except Exception:
                # Always close the socket even if Firefox has already ended
                # the session or the connection is otherwise shutting down.
                pass
            finally:
                await self.ws.close()

    async def command(self, method, params):
        ident = self.next_id
        self.next_id += 1

        await self.ws.send(json.dumps({
            "id": ident,
            "method": method,
            "params": params,
        }))

        while True:
            message = json.loads(await self.ws.recv())

            if message.get("id") != ident:
                continue

            if message.get("type") == "error":
                raise RuntimeError(
                    f"{message.get('error')}: {message.get('message')}"
                )

            return message.get("result", {})


async def get_live_tabs(host, port):
    async with Bidi(host, port) as bidi:
        result = await bidi.command(
            "browsingContext.getTree",
            {"maxDepth": 0},
        )

        rows = []

        for index, context in enumerate(
            result.get("contexts", []), 1
        ):
            rows.append({
                "index": index,
                "context": context.get("context", ""),
                "url": context.get("url", ""),
            })

        return rows


async def close_contexts(host, port, contexts):
    closed = 0
    failed = []

    async with Bidi(host, port) as bidi:
        for number, context in enumerate(contexts, 1):
            try:
                await bidi.command(
                    "browsingContext.close",
                    {"context": context},
                )
                closed += 1
            except Exception as error:
                failed.append((context, str(error)))

            if number % 100 == 0:
                print(
                    f"  Closed {closed} of {number} attempted..."
                )

    return closed, failed


def normalize_url(url):
    try:
        parsed = urlsplit(url)

        if parsed.scheme in {
            "file", "blob", "about", "moz-extension"
        }:
            return url

        query = []

        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        ):
            lower = key.lower()

            if lower.startswith("utm_"):
                continue

            if lower in TRACKING_KEYS:
                continue

            query.append((key, value))

        path = parsed.path.rstrip("/") or "/"

        return urlunsplit((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            urlencode(query, doseq=True),
            "",
        ))

    except Exception:
        return url


def get_host(url):
    try:
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def is_blank(url):
    return url in {
        "about:blank",
        "about:home",
        "about:newtab",
        "about:privatebrowsing",
    }


def is_protected(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        lower = url.lower()

        if parsed.scheme.lower() == "file":
            return True

        if host in PROTECTED_HOSTS:
            return True

        if host == "github.com" or host.endswith(".github.com"):
            return True

        return any(
            token in lower
            for token in PROTECTED_TOKENS
        )

    except Exception:
        return True


def compact_url(url, max_length=180):
    if len(url) <= max_length:
        return url
    return url[:max_length - 3] + "..."


def query_qwen(items, args):
    response_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "tab_cleanup",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "close": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "review": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["close", "review"],
                "additionalProperties": False,
            },
        },
    }

    payload = {
        "model": args.model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    items,
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": response_schema,
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
        "reasoning_effort": "none",
    }

    def send(body):
        request = urllib.request.Request(
            args.base.rstrip("/")
            + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=args.timeout,
        ) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    try:
        data = send(payload)

    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise

        payload.pop("reasoning_effort", None)
        data = send(payload)

    message = (
        data.get("choices", [{}])[0]
        .get("message", {})
    )

    content = message.get("content") or ""

    if not content:
        raise ValueError(
            "LM Studio returned empty content"
        )

    result = json.loads(content)

    valid_ids = {
        int(item["id"])
        for item in items
    }

    close_ids = {
        int(value)
        for value in result.get("close", [])
    } & valid_ids

    review_ids = {
        int(value)
        for value in result.get("review", [])
    } & valid_ids

    review_ids -= close_ids

    decisions = {}

    for tab_id in valid_ids:
        if tab_id in close_ids:
            decisions[tab_id] = "CLOSE"
        elif tab_id in review_ids:
            decisions[tab_id] = "REVIEW"
        else:
            decisions[tab_id] = "KEEP"

    return decisions


def classify_tabs(rows, args):
    for tab_id, row in enumerate(rows, 1):
        row["id"] = tab_id
        row["host"] = get_host(row["url"])
        row["normalized_url"] = normalize_url(
            row["url"]
        )
        row["decision"] = ""
        row["reason"] = ""
        row["method"] = ""

    normalized_counts = Counter(
        row["normalized_url"]
        for row in rows
        if row["normalized_url"]
    )

    seen = set()
    pending = []

    for row in rows:
        normalized = row["normalized_url"]

        if is_blank(row["url"]):
            row["decision"] = "CLOSE"
            row["reason"] = "Blank/home tab"
            row["method"] = "local"

        elif (
            normalized
            and normalized_counts[normalized] > 1
            and normalized in seen
        ):
            row["decision"] = "CLOSE"
            row["reason"] = (
                "Duplicate normalized URL"
            )
            row["method"] = "local"

        else:
            if normalized:
                seen.add(normalized)

            if is_protected(row["url"]):
                row["decision"] = "KEEP"
                row["reason"] = "Protected page"
                row["method"] = "protected"
            else:
                pending.append(row)

    print(
        f"{len(rows) - len(pending)} tabs "
        "handled locally or protected."
    )
    print(
        f"{len(pending)} tabs need Qwen "
        "classification."
    )

    pending.sort(
        key=lambda row: (
            row["host"],
            row["url"].lower(),
        )
    )

    total_batches = (
        len(pending) + args.batch_size - 1
    ) // args.batch_size

    for start in range(
        0,
        len(pending),
        args.batch_size,
    ):
        batch = pending[
            start:start + args.batch_size
        ]

        batch_number = (
            start // args.batch_size + 1
        )

        domains = len({
            row["host"]
            for row in batch
        })

        print(
            f"Qwen batch "
            f"{batch_number}/{total_batches} "
            f"({len(batch)} tabs, "
            f"{domains} domains)"
        )

        items = [
            {
                "id": row["id"],
                "url": compact_url(
                    row["normalized_url"]
                ),
            }
            for row in batch
        ]

        try:
            decisions = query_qwen(
                items,
                args,
            )

        except Exception as error:
            print(
                "  Batch failed. "
                "Marking REVIEW. "
                + str(error)[:160]
            )
            decisions = {}

        for row in batch:
            if row["id"] not in decisions:
                row["decision"] = "REVIEW"
                row["reason"] = (
                    "No reliable Qwen result"
                )
                row["method"] = "qwen-failed"
            else:
                row["decision"] = (
                    decisions[row["id"]]
                )
                row["reason"] = (
                    "Qwen structured "
                    "classification"
                )
                row["method"] = "qwen"


def write_report(rows, path):
    fields = [
        "id",
        "context",
        "decision",
        "reason",
        "method",
        "host",
        "url",
        "normalized_url",
    ]

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(
            sorted(
                rows,
                key=lambda row: int(row["id"]),
            )
        )


def read_csv(path):
    with open(
        path,
        newline="",
        encoding="utf-8",
    ) as file:
        return list(csv.DictReader(file))


def assign_actions(rows):
    for row in rows:
        if row.get("decision") != "CLOSE":
            row["action"] = "LEAVE"

        elif is_protected(
            row.get("url", "")
        ):
            row["action"] = "LEAVE_PROTECTED"

        elif (
            row.get("method") == "local"
            and row.get("reason") in {
                "Duplicate normalized URL",
                "Blank/home tab",
            }
        ):
            row["action"] = "CLOSE"

        elif row.get("method") == "qwen":
            row["action"] = (
                "ARCHIVE_THEN_CLOSE"
            )

        else:
            row["action"] = "LEAVE_REVIEW"

    groups = defaultdict(list)

    for row in rows:
        normalized = row.get(
            "normalized_url",
            "",
        )

        if normalized:
            groups[normalized].append(row)

    for group in groups.values():
        if len(group) < 2:
            continue

        survivor = next(
            (
                row
                for row in group
                if row["action"] in {
                    "LEAVE",
                    "LEAVE_PROTECTED",
                    "LEAVE_REVIEW",
                }
            ),
            None,
        )

        if survivor is None:
            survivor = min(
                group,
                key=lambda row: int(
                    row.get("id") or 10**12
                ),
            )

            survivor["action"] = (
                "LEAVE_DUPLICATE_SURVIVOR"
            )


def write_plan(rows, path):
    fields = [
        "id",
        "context",
        "decision",
        "reason",
        "method",
        "action",
        "host",
        "url",
        "normalized_url",
    ]

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(
            sorted(
                rows,
                key=lambda row: int(
                    row.get("id") or 0
                ),
            )
        )


def write_archive(rows, path):
    archived = [
        row
        for row in rows
        if row.get("action")
        in CLOSING_ACTIONS
    ]

    groups = defaultdict(list)

    for row in archived:
        groups[
            row.get("host")
            or "(no domain)"
        ].append(row)

    created = dt.datetime.now().isoformat(
        timespec="minutes"
    )

    parts = [
        "<!doctype html>",
        "<html><head>",
        "<meta charset='utf-8'>",
        "<title>Archived Firefox Tabs</title>",
        "<style>",
        "body{font-family:-apple-system,"
        "BlinkMacSystemFont,sans-serif;"
        "max-width:1100px;margin:40px auto;"
        "padding:0 20px;line-height:1.45}",
        "li{margin:9px 0}",
        ".u{font-size:12px;color:#666;"
        "word-break:break-all}",
        "</style></head><body>",
        "<h1>Archived Firefox Tabs</h1>",
        f"<p>{len(archived)} planned "
        f"closures archived "
        f"{html.escape(created)}</p>",
    ]

    for host in sorted(groups):
        parts.append(
            f"<h2>{html.escape(host)}</h2>"
        )
        parts.append("<ul>")

        for row in groups[host]:
            url = row.get("url", "")
            action = row.get("action", "")

            parts.append(
                "<li>"
                f"<strong>["
                f"{html.escape(action)}"
                f"]</strong> "
                f"<a href='"
                f"{html.escape(url, quote=True)}"
                f"'>"
                f"{html.escape(url)}"
                f"</a>"
                "</li>"
            )

        parts.append("</ul>")

    parts.extend([
        "</body>",
        "</html>",
    ])

    path.write_text(
        "\n".join(parts),
        encoding="utf-8",
    )


def validate_plan(rows):
    if not rows:
        raise SystemExit(
            "Action plan is empty."
        )

    if "action" not in rows[0]:
        raise SystemExit(
            "CSV has no action column. "
            "Run the plan command first."
        )


def reconcile_plan(plan_rows, live_rows):
    live_by_context = {
        row["context"]: row
        for row in live_rows
        if row.get("context")
    }

    live_by_url = defaultdict(list)

    for row in live_rows:
        live_by_url[row["url"]].append(row)

    planned = [
        row
        for row in plan_rows
        if row.get("action")
        in CLOSING_ACTIONS
    ]

    selected = []
    used_contexts = set()
    already_gone = 0

    for row in planned:
        context = row.get("context", "")
        url = row.get("url", "")

        # Best case. The original BiDi context is still live.
        if (
            context
            and context in live_by_context
            and context not in used_contexts
        ):
            selected.append(
                live_by_context[context]
            )
            used_contexts.add(context)
            continue

        # Context IDs may change after Firefox restarts.
        # Fall back conservatively to an exact URL match.
        candidate = next(
            (
                live
                for live in live_by_url.get(
                    url,
                    [],
                )
                if live["context"]
                not in used_contexts
            ),
            None,
        )

        if candidate is not None:
            selected.append(candidate)
            used_contexts.add(
                candidate["context"]
            )
        else:
            already_gone += 1

    return {
        "live": live_rows,
        "planned": planned,
        "selected": selected,
        "already_gone": already_gone,
    }


def print_status(state):
    print(
        f"Live Firefox tabs: "
        f"{len(state['live'])}"
    )
    print(
        f"Original planned closures: "
        f"{len(state['planned'])}"
    )
    print(
        f"Estimated already closed "
        f"or no longer present: "
        f"{state['already_gone']}"
    )
    print(
        f"Remaining planned closures "
        f"found live: "
        f"{len(state['selected'])}"
    )


def command_classify(args):
    try:
        rows = asyncio.run(
            get_live_tabs(
                args.host,
                args.port,
            )
        )
    except Exception as error:
        raise SystemExit(
            "Could not connect to Firefox "
            f"WebDriver BiDi at "
            f"ws://{args.host}:{args.port}/session\n"
            f"{error}"
        )

    if not rows:
        raise SystemExit(
            "No Firefox tabs found."
        )

    print(
        f"Found {len(rows)} Firefox tabs."
    )

    classify_tabs(rows, args)

    stamp = dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    report = Path(
        args.output
        or f"firefox_tab_report_{stamp}.csv"
    )

    write_report(rows, report)

    counts = Counter(
        row["decision"]
        for row in rows
    )

    print()
    print(f"KEEP   {counts['KEEP']}")
    print(f"CLOSE  {counts['CLOSE']}")
    print(f"REVIEW {counts['REVIEW']}")
    print(
        f"Report written to {report}"
    )
    print(
        "No Firefox tabs were closed."
    )


def command_plan(args):
    rows = read_csv(args.report)

    if not rows:
        raise SystemExit(
            "Classification report is empty."
        )

    assign_actions(rows)

    stamp = dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    plan = Path(
        args.plan_output
        or f"firefox_tab_action_plan_"
           f"{stamp}.csv"
    )

    archive = Path(
        args.archive_output
        or f"firefox_tab_archive_"
           f"{stamp}.html"
    )

    write_plan(rows, plan)
    write_archive(rows, archive)

    if (
        not archive.exists()
        or archive.stat().st_size < 100
    ):
        raise SystemExit(
            "Archive verification failed."
        )

    actions = Counter(
        row["action"]
        for row in rows
    )

    print(
        f"Archive verified: {archive}"
    )
    print(
        "Archived entries: "
        f"{sum(actions[a] for a in CLOSING_ACTIONS)}"
    )
    print(
        "Direct CLOSE planned: "
        f"{actions['CLOSE']}"
    )
    print(
        "ARCHIVE_THEN_CLOSE planned: "
        f"{actions['ARCHIVE_THEN_CLOSE']}"
    )
    print(
        "Protected/left open: "
        f"{actions['LEAVE_PROTECTED']}"
    )
    print(
        "Forced duplicate survivors: "
        f"{actions['LEAVE_DUPLICATE_SURVIVOR']}"
    )
    print(
        f"Action plan: {plan}"
    )
    print(
        "No Firefox tabs were closed."
    )


def command_status(args):
    plan_rows = read_csv(args.plan)
    validate_plan(plan_rows)

    live_rows = asyncio.run(
        get_live_tabs(
            args.host,
            args.port,
        )
    )

    state = reconcile_plan(
        plan_rows,
        live_rows,
    )

    print_status(state)
    print(
        "STATUS ONLY. "
        "No files created and "
        "no tabs closed."
    )



async def apply_plan_single_session(host, port, plan_rows, do_apply):
    """Reconcile and optionally close tabs without changing BiDi sessions."""
    async with Bidi(host, port) as bidi:
        result = await bidi.command(
            "browsingContext.getTree",
            {"maxDepth": 0},
        )

        live_rows = []
        for index, context in enumerate(result.get("contexts", []), 1):
            live_rows.append({
                "index": index,
                "context": context.get("context", ""),
                "url": context.get("url", ""),
            })

        state = reconcile_plan(plan_rows, live_rows)

        if not do_apply:
            return state, 0, []

        closed = 0
        failed = []

        # These context IDs were obtained in this exact BiDi session.
        for number, row in enumerate(state["selected"], 1):
            context = row["context"]
            try:
                await bidi.command(
                    "browsingContext.close",
                    {"context": context},
                )
                closed += 1
            except Exception as error:
                failed.append((context, str(error)))

            if number % 100 == 0:
                print(f"  Closed {closed} of {number} attempted...")

        return state, closed, failed

def command_apply(args):
    plan_rows = read_csv(args.plan)
    validate_plan(plan_rows)

    try:
        state, closed, failed = asyncio.run(
            apply_plan_single_session(
                args.host,
                args.port,
                plan_rows,
                args.apply,
            )
        )
    except Exception as error:
        raise SystemExit(
            "Could not complete Firefox apply operation.\n"
            f"{error}"
        )

    print_status(state)

    if not args.apply:
        print(
            "DRY RUN. "
            "No files created and "
            "no tabs closed."
        )
        return

    if not state["selected"]:
        print("Nothing remains to close.")
        return

    print(f"Closed this pass: {closed}")

    if failed:
        print(f"Failed to close: {len(failed)}")
        for context, error in failed[:10]:
            print(f"  {context}  {error}")

    print(
        "Existing archive and "
        "action-plan files were "
        "not modified."
    )

def add_bidi_args(parser):
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Standalone AI-assisted "
            "Firefox tab manager."
        )
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    classify = sub.add_parser(
        "classify",
        help=(
            "Classify current Firefox tabs"
        ),
    )

    add_bidi_args(classify)

    classify.add_argument(
        "--batch-size",
        type=int,
        default=40,
    )
    classify.add_argument(
        "--model",
        default=DEFAULT_MODEL,
    )
    classify.add_argument(
        "--base",
        default=DEFAULT_BASE,
    )
    classify.add_argument(
        "--timeout",
        type=int,
        default=90,
    )
    classify.add_argument(
        "--output",
    )
    classify.set_defaults(
        func=command_classify,
    )

    plan = sub.add_parser(
        "plan",
        help=(
            "Create archive and "
            "safe action plan"
        ),
    )
    plan.add_argument("report")
    plan.add_argument("--plan-output")
    plan.add_argument("--archive-output")
    plan.set_defaults(
        func=command_plan,
    )

    status = sub.add_parser(
        "status",
        help=(
            "Compare saved plan with "
            "current Firefox tabs"
        ),
    )
    add_bidi_args(status)
    status.add_argument("plan")
    status.set_defaults(
        func=command_status,
    )

    apply_parser = sub.add_parser(
        "apply",
        help=(
            "Dry-run or apply an "
            "existing action plan"
        ),
    )
    add_bidi_args(apply_parser)
    apply_parser.add_argument("plan")
    apply_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually close planned tabs"
        ),
    )
    apply_parser.set_defaults(
        func=command_apply,
    )

    return parser


def main():
    args = build_parser().parse_args()

    if (
        args.command == "classify"
        and not 1 <= args.batch_size <= 200
    ):
        raise SystemExit(
            "--batch-size must be "
            "between 1 and 200."
        )

    args.func(args)


if __name__ == "__main__":
    main()
