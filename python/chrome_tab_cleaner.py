#!/usr/bin/env python3
import argparse, csv, datetime as dt, json, subprocess, sys
import urllib.error, urllib.request
from collections import Counter

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"

SYSTEM_PROMPT = '''You classify open browser tabs for cleanup.
Return exactly one JSON object with keys "decision" and "reason".
decision must be KEEP, CLOSE, or REVIEW.
Be conservative. KEEP ongoing work, research, documentation, papers,
projects, unfinished tasks, or useful references. CLOSE only clearly
transient, obsolete, completed, disposable, error, or low-value pages.
Use REVIEW whenever uncertain. Never close a page merely because it is old.'''

APPLE_GET_TABS = '''
set delim to ASCII character 9

tell application "Google Chrome"
    set output to ""
    set winIndex to 0

    repeat with w in windows
        set winIndex to winIndex + 1
        set tabIndex to 0

        repeat with t in tabs of w
            set tabIndex to tabIndex + 1
            set tabTitle to title of t
            set tabURL to URL of t

            -- Chrome does not expose pinned reliably through AppleScript
            set isPinned to false

            set output to output & winIndex & delim & tabIndex & delim & isPinned & delim & tabTitle & delim & tabURL & linefeed
        end repeat
    end repeat

    return output
end tell
'''

def run_osascript(source, args=None):
    cmd = ["osascript", "-"]
    if args:
        cmd += args
    p = subprocess.run(cmd, input=source, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "AppleScript failed")
    return p.stdout

def get_tabs():
    raw = run_osascript(APPLE_GET_TABS)
    out = []
    for line in raw.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            w, t, pinned, title, url = parts
            out.append(dict(window=int(w), tab=int(t),
                            pinned=pinned.lower() == "true",
                            title=title, url=url))
    return out

def get_page_text(w, t, max_chars):
    js = "document.body ? document.body.innerText.slice(0,%d) : ''" % max_chars
    apple = '''
on run argv
    set wnum to (item 1 of argv) as integer
    set tnum to (item 2 of argv) as integer
    set jsCode to item 3 of argv
    tell application "Google Chrome"
        return execute tab tnum of window wnum javascript jsCode
    end tell
end run
'''
    try:
        return run_osascript(apple, [str(w), str(t), js]).strip()
    except Exception:
        return ""

def lmstudio(base_url, model, user_text):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0,
        "max_tokens": 120
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"].strip()

def parse_result(s):
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    try:
        x = json.loads(s)
        d = str(x.get("decision", "REVIEW")).upper()
        reason = str(x.get("reason", "")).strip()
    except Exception:
        return "REVIEW", "Model response was not valid JSON"
    if d not in {"KEEP", "CLOSE", "REVIEW"}:
        return "REVIEW", "Model returned an unknown decision"
    return d, reason

def classify(tab, args):
    text = get_page_text(tab["window"], tab["tab"], args.max_chars)
    payload = json.dumps({
        "title": tab["title"],
        "url": tab["url"],
        "page_text": text
    }, ensure_ascii=False)
    try:
        decision, reason = parse_result(lmstudio(args.base_url, args.model, payload))
    except Exception as e:
        decision, reason = "REVIEW", "LM Studio error: " + str(e)
    return decision, reason, bool(text)

def close_tabs(rows):
    targets = [r for r in rows if r["decision"] == "CLOSE" and not r["pinned"]]
    targets.sort(key=lambda x: (x["window"], x["tab"]), reverse=True)
    apple = '''
on run argv
    set wnum to (item 1 of argv) as integer
    set tnum to (item 2 of argv) as integer
    tell application "Google Chrome"
        if wnum <= (count of windows) then
            tell window wnum
                if tnum <= (count of tabs) then close tab tnum
            end tell
        end if
    end tell
end run
'''
    for r in targets:
        try:
            run_osascript(apple, [str(r["window"]), str(r["tab"])])
            r["closed"] = True
        except Exception as e:
            r["close_error"] = str(e)

def main():
    p = argparse.ArgumentParser(description="Review Chrome tabs with local LM Studio.")
    p.add_argument("--close", action="store_true",
                   help="Close tabs classified CLOSE. Default is dry run.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--max-chars", type=int, default=6000)
    args = p.parse_args()

    try:
        tabs = get_tabs()
    except Exception as e:
        sys.exit("Could not read Chrome tabs. " + str(e))

    if not tabs:
        print("No Chrome tabs found.")
        return

    counts_by_url = Counter(t["url"] for t in tabs if t["url"])
    seen = set()
    rows = []

    for i, tab in enumerate(tabs, 1):
        print(f"[{i}/{len(tabs)}] {tab['title'][:75]}")
        r = dict(tab, closed=False, close_error="", page_text_read=False)

        if tab["pinned"]:
            r["decision"], r["reason"] = "KEEP", "Pinned tab protected"
        elif tab["url"] and counts_by_url[tab["url"]] > 1 and tab["url"] in seen:
            r["decision"], r["reason"] = "CLOSE", "Exact duplicate URL"
        else:
            if tab["url"]:
                seen.add(tab["url"])
            r["decision"], r["reason"], r["page_text_read"] = classify(tab, args)
        rows.append(r)

    if args.close:
        close_tabs(rows)
    else:
        print("\nDRY RUN. No tabs were closed.")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = f"chrome_tab_report_{stamp}.csv"
    fields = ["window","tab","pinned","decision","reason","title","url",
              "page_text_read","closed","close_error"]
    with open(report, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)

    c = Counter(r["decision"] for r in rows)
    print(f"\nKEEP   {c['KEEP']}\nCLOSE  {c['CLOSE']}\nREVIEW {c['REVIEW']}")
    print(f"\nReport written to {report}")
    if not args.close:
        print("Review the CSV. Run again with --close only when satisfied.")

if __name__ == "__main__":
    main()
