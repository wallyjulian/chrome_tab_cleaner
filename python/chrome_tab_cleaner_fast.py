#!/usr/bin/env python3
import argparse, csv, datetime as dt, json, subprocess, urllib.request
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MODEL = "qwen/qwen3.8-27b"
BASE = "http://127.0.0.1:1234/v1"

SYSTEM = """Classify browser tabs for cleanup. Return ONLY a JSON array with one object per input tab. Each object must have id, decision, reason. decision must be KEEP, CLOSE, or REVIEW. Be conservative. KEEP ongoing work, research, references, documentation, mail, calendar, bookings, account pages, financial pages, local files, and localhost work. CLOSE only clearly disposable, completed, obsolete, blank, error, or low-value pages. REVIEW whenever uncertain. Keep reasons short."""

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
            rows.append({"window": int(x[0]), "tab": int(x[1]), "pinned": False, "title": x[3], "url": x[4]})
    return rows

def normalize_url(u):
    try:
        s = urlsplit(u)
        if s.scheme in {"file", "blob", "chrome"}:
            return u
        drop = {"fbclid","gclid","gbraid","wbraid","mc_cid","mc_eid"}
        q = [(k,v) for k,v in parse_qsl(s.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in drop]
        path = s.path.rstrip("/") or "/"
        return urlunsplit((s.scheme.lower(), s.netloc.lower(), path, urlencode(q, doseq=True), ""))
    except Exception:
        return u

def ask_qwen(items, args):
    body = json.dumps({
        "model": args.model,
        "messages": [{"role":"system","content":SYSTEM},
                     {"role":"user","content":json.dumps(items, ensure_ascii=False)}],
        "temperature": 0,
        "max_tokens": 3000
    }).encode("utf-8")
    req = urllib.request.Request(args.base.rstrip("/") + "/chat/completions", data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        text = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        parts = text.splitlines()
        text = "\n".join(parts[1:-1])
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b < a:
        raise ValueError("Qwen did not return a JSON array")
    result = json.loads(text[a:b+1])
    return {int(x["id"]): (str(x.get("decision","REVIEW")).upper(), str(x.get("reason",""))) for x in result}

def main():
    p = argparse.ArgumentParser(description="Fast dry-run Chrome tab classifier")
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--base", default=BASE)
    args = p.parse_args()

    rows = get_tabs()
    if not rows:
        raise SystemExit("No Chrome tabs found.")
    print(f"Found {len(rows)} tabs.")

    for i, r in enumerate(rows, 1):
        r["id"] = i
        r["normalized_url"] = normalize_url(r["url"])
        r["decision"] = ""
        r["reason"] = ""

    counts = Counter(r["normalized_url"] for r in rows if r["normalized_url"])
    seen = set()
    pending = []
    for r in rows:
        u = r["normalized_url"]
        if r["url"] == "chrome://newtab/":
            r["decision"], r["reason"] = "CLOSE", "Blank new tab"
        elif u and counts[u] > 1 and u in seen:
            r["decision"], r["reason"] = "CLOSE", "Duplicate normalized URL"
        else:
            if u:
                seen.add(u)
            pending.append(r)

    print(f"{len(rows)-len(pending)} tabs handled locally.")
    print(f"{len(pending)} tabs will be sent to Qwen in batches of {args.batch_size}.")

    total = (len(pending) + args.batch_size - 1) // args.batch_size
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start:start+args.batch_size]
        number = start // args.batch_size + 1
        print(f"Qwen batch {number}/{total}")
        payload = [{"id":r["id"], "title":r["title"], "url":r["url"]} for r in batch]
        try:
            answers = ask_qwen(payload, args)
        except Exception as e:
            answers = {r["id"]: ("REVIEW", "Batch error: " + str(e)) for r in batch}
        for r in batch:
            d, reason = answers.get(r["id"], ("REVIEW", "No result returned"))
            if d not in {"KEEP","CLOSE","REVIEW"}:
                d = "REVIEW"
            r["decision"], r["reason"] = d, reason

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"chrome_tab_report_{stamp}.csv"
    fields = ["window","tab","pinned","decision","reason","title","url","normalized_url"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    c = Counter(r["decision"] for r in rows)
    print(f"KEEP {c['KEEP']}   CLOSE {c['CLOSE']}   REVIEW {c['REVIEW']}")
    print("DRY RUN. This version cannot close tabs.")
    print("Report written to", filename)

if __name__ == "__main__":
    main()
