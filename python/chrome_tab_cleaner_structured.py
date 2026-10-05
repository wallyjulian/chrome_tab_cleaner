#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import subprocess
import urllib.request
import urllib.error
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

DEFAULT_MODEL = "qwen/qwen3.8-27b"
DEFAULT_BASE = "http://127.0.0.1:1234/v1"

SYSTEM = '''Classify browser tabs for cleanup.
Return ONLY one compact JSON object with two arrays.
Format exactly as {"close":[ids], "review":[ids]}.
Any input id not listed is implicitly KEEP.
Do not give reasons or any other text.
Be conservative.
KEEP useful research, articles, documentation, code, academic material,
ongoing work, references, bookings, and events.
CLOSE only clearly disposable, completed, obsolete, generic, transient,
promotional, low-value, error, or stale navigation/search pages.
REVIEW whenever uncertain.
Keep each reason under 10 words.'''

GET_TABS = '''
set delim to ASCII character 9
tell application "Google Chrome"
    set output to ""
    set wi to 0
    repeat with w in windows
        set wi to wi + 1
        set ti to 0
        repeat with t in tabs of w
            set ti to ti + 1
            set output to output & wi & delim & ti & delim & false & delim & (title of t) & delim & (URL of t) & linefeed
        end repeat
    end repeat
    return output
end tell
'''

TRACKING_KEYS = {
    "fbclid","gclid","gbraid","wbraid","dclid","msclkid",
    "mc_cid","mc_eid","igshid","vero_id","yclid"
}

PROTECTED_HOSTS = {
    "mail.google.com", "calendar.google.com", "docs.google.com",
    "drive.google.com", "accounts.google.com", "localhost", "127.0.0.1"
}

def osa(script):
    r = subprocess.run(["osascript", "-"], input=script, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout

def get_tabs():
    rows = []
    for line in osa(GET_TABS).splitlines():
        x = line.split("\t", 4)
        if len(x) == 5:
            rows.append({
                "window": int(x[0]), "tab": int(x[1]), "pinned": False,
                "title": x[3], "url": x[4]
            })
    return rows

def normalize_url(url):
    try:
        s = urlsplit(url)
        if s.scheme in {"file", "blob", "chrome"}:
            return url
        q = []
        for k, v in parse_qsl(s.query, keep_blank_values=True):
            kl = k.lower()
            if kl.startswith("utm_") or kl in TRACKING_KEYS:
                continue
            q.append((k, v))
        path = s.path.rstrip("/") or "/"
        return urlunsplit((s.scheme.lower(), s.netloc.lower(), path,
                           urlencode(q, doseq=True), ""))
    except Exception:
        return url

def host_of(url):
    try:
        return urlsplit(url).hostname or ""
    except Exception:
        return ""

def protect(row):
    url = row["url"].lower()
    host = row["host"]
    scheme = urlsplit(row["url"]).scheme.lower()
    if scheme == "file":
        return "Local file protected"
    if host in PROTECTED_HOSTS:
        return "Work or local page protected"
    if host == "github.com" or host.endswith(".github.com"):
        return "GitHub page protected"
    tokens = ("/account", "/signin", "/login", "/inbox", "bank",
              "wealth", "mychart", "booking-details", "confirmation", "receipt")
    if any(x in url for x in tokens):
        return "Account or transaction page protected"
    return None

def compact(url, n=180):
    return url if len(url) <= n else url[:n-3] + "..."

def ask_qwen(items, args):
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "tab_cleanup",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "close": {
                        "type": "array",
                        "items": {"type": "integer"}
                    },
                    "review": {
                        "type": "array",
                        "items": {"type": "integer"}
                    }
                },
                "required": ["close", "review"],
                "additionalProperties": False
            }
        }
    }

    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)}
        ],
        "response_format": schema,
        "temperature": 0,
        "max_tokens": 800,
        "stream": False
    }

    # Qwen 3.8 in LM Studio may accept this OpenAI-compatible setting.
    # If LM Studio rejects it, retry once without the setting.
    payload["reasoning_effort"] = "none"

    def send(body):
        req = urllib.request.Request(
            args.base.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = send(payload)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            payload.pop("reasoning_effort", None)
            data = send(payload)
        else:
            raise

    message = data.get("choices", [{}])[0].get("message", {})
    content = message.get("content") or ""

    if not content:
        diagnostic = {
            "message": message,
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason"),
            "usage": data.get("usage"),
        }
        raise ValueError(
            "Empty content. Diagnostic: " +
            json.dumps(diagnostic, ensure_ascii=False)[:1000]
        )

    result = json.loads(content)
    close_ids = {int(x) for x in result.get("close", [])}
    review_ids = {int(x) for x in result.get("review", [])}

    valid_ids = {int(x["id"]) for x in items}
    close_ids &= valid_ids
    review_ids &= valid_ids
    review_ids -= close_ids

    out = {}
    for ident in valid_ids:
        if ident in close_ids:
            out[ident] = ("CLOSE", "Qwen structured classification")
        elif ident in review_ids:
            out[ident] = ("REVIEW", "Qwen requested review")
        else:
            out[ident] = ("KEEP", "Implicit keep")
    return out

def main():
    p = argparse.ArgumentParser(description="Dry-run Chrome classifier for thousands of tabs")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--timeout", type=int, default=90)
    args = p.parse_args()

    rows = get_tabs()
    if not rows:
        raise SystemExit("No Chrome tabs found.")
    print(f"Found {len(rows)} tabs.")

    for i, r in enumerate(rows, 1):
        r["id"] = i
        r["host"] = host_of(r["url"])
        r["normalized_url"] = normalize_url(r["url"])
        r["decision"] = ""
        r["reason"] = ""
        r["method"] = ""

    counts = Counter(r["normalized_url"] for r in rows if r["normalized_url"])
    seen = set()
    pending = []

    for r in rows:
        u = r["normalized_url"]
        if r["url"] in {"chrome://newtab/", "about:blank"}:
            r["decision"], r["reason"], r["method"] = "CLOSE", "Blank tab", "local"
        elif u and counts[u] > 1 and u in seen:
            r["decision"], r["reason"], r["method"] = "CLOSE", "Duplicate normalized URL", "local"
        else:
            if u:
                seen.add(u)
            reason = protect(r)
            if reason:
                r["decision"], r["reason"], r["method"] = "KEEP", reason, "protected"
            else:
                pending.append(r)

    print(f"{len(rows)-len(pending)} tabs handled locally or protected.")
    print(f"{len(pending)} tabs need Qwen classification.")

    pending.sort(key=lambda r: (r["host"], r["title"].lower()))
    total = (len(pending) + args.batch_size - 1) // args.batch_size

    for start in range(0, len(pending), args.batch_size):
        batch = pending[start:start+args.batch_size]
        num = start // args.batch_size + 1
        domains = len(set(r["host"] for r in batch))
        print(f"Qwen batch {num}/{total} ({len(batch)} tabs, {domains} domains)")
        payload = [{"id": r["id"], "title": r["title"][:220],
                    "url": compact(r["normalized_url"])} for r in batch]
        try:
            answers = ask_qwen(payload, args)
        except Exception as e:
            print("  Batch failed. Marking REVIEW.", str(e)[:120])
            answers = {}
        for r in batch:
            d, reason = answers.get(r["id"], ("REVIEW", "No reliable batch result"))
            r["decision"], r["reason"] = d, reason
            r["method"] = "qwen" if r["id"] in answers else "qwen-failed"

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"chrome_tab_report_large_{stamp}.csv"
    fields = ["id","window","tab","pinned","decision","reason","method",
              "host","title","url","normalized_url"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["id"]))

    c = Counter(r["decision"] for r in rows)
    m = Counter(r["method"] for r in rows)
    print()
    print(f"KEEP   {c['KEEP']}")
    print(f"CLOSE  {c['CLOSE']}")
    print(f"REVIEW {c['REVIEW']}")
    print(f"Qwen classified {m['qwen']}")
    print(f"Qwen unresolved {m['qwen-failed']}")
    print()
    print("DRY RUN. This version contains no tab-closing code.")
    print("Report written to", filename)

if __name__ == "__main__":
    main()
