#!/usr/bin/env python3
import argparse, csv, subprocess
from collections import Counter, defaultdict

GET_TABS = """
set delim to ASCII character 9
tell application "Google Chrome"
 set output to ""
 set wi to 0
 repeat with w in windows
  set wi to wi + 1
  set ti to 0
  repeat with t in tabs of w
   set ti to ti + 1
   set output to output & wi & delim & ti & delim & (URL of t) & linefeed
  end repeat
 end repeat
 return output
end tell
"""

CLOSING_ACTIONS = {"CLOSE", "ARCHIVE_THEN_CLOSE"}

def osa(script):
    r = subprocess.run(["osascript", "-"], input=script, text=True,
                       capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()

def read_plan(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "action" not in rows[0]:
        raise SystemExit("This does not look like an action-plan CSV.")
    return rows

def live_tabs():
    raw = osa(GET_TABS)
    out = []
    for line in raw.splitlines():
        x = line.split("\t", 2)
        if len(x) == 3:
            out.append((int(x[0]), int(x[1]), x[2]))
    return out

def main():
    p = argparse.ArgumentParser(
        description="Resume an existing Chrome tab action plan quickly."
    )
    p.add_argument("plan", help="Existing chrome_tab_action_plan CSV")
    p.add_argument("--apply", action="store_true",
                   help="Close remaining planned tabs in one Chrome pass")
    args = p.parse_args()

    rows = read_plan(args.plan)
    wanted = Counter(
        r["url"] for r in rows if r.get("action") in CLOSING_ACTIONS
    )
    original = Counter(r["url"] for r in rows)

    live = live_tabs()
    live_counts = Counter(u for _, _, u in live)

    # Infer planned closures that already happened during the interrupted run.
    already = Counter()
    for u, n in wanted.items():
        missing_from_original = max(0, original[u] - live_counts.get(u, 0))
        already[u] = min(n, missing_from_original)

    remaining = Counter({
        u: max(0, n - already[u])
        for u, n in wanted.items()
    })

    # Select exact live tab coordinates. Never select more copies than planned.
    selected = []
    used = Counter()
    for wi, ti, u in live:
        if used[u] < remaining.get(u, 0):
            selected.append((wi, ti, u))
            used[u] += 1

    planned = sum(wanted.values())
    done = sum(already.values())
    left = len(selected)
    unavailable = sum(remaining.values()) - left

    print(f"Live Chrome tabs: {len(live)}")
    print(f"Original planned closures: {planned}")
    print(f"Estimated already closed: {done}")
    print(f"Remaining planned closures found live: {left}")
    print(f"Planned tabs no longer found: {unavailable}")

    if not args.apply:
        print("DRY RUN. No files created and no tabs closed.")
        return

    if unavailable:
        print("WARNING: Some planned tabs are no longer present.")
        print("Only exact live matches will be closed.")

    # Close by original window/tab coordinates, in descending order.
    # This keeps indexes stable as tabs disappear.
    by_window = defaultdict(list)
    for wi, ti, u in selected:
        by_window[wi].append(ti)

    script = [
        'tell application "Google Chrome"',
        'set closedCount to 0'
    ]
    for wi in sorted(by_window, reverse=True):
        for ti in sorted(by_window[wi], reverse=True):
            script.extend([
                f'if (count of windows) >= {wi} then',
                f' if (count of tabs of window {wi}) >= {ti} then',
                f'  close tab {ti} of window {wi}',
                '  set closedCount to closedCount + 1',
                ' end if',
                'end if'
            ])
    script.extend(['return closedCount', 'end tell'])

    print("Closing remaining tabs in one Chrome pass...")
    result = osa("\n".join(script))
    print(f"Closed this pass: {result}")
    print("Existing archive and action-plan files were not modified.")

if __name__ == "__main__":
    main()
