#!/usr/bin/env python3
import argparse, csv, datetime as dt, html, subprocess
from pathlib import Path
from collections import Counter, defaultdict
from urllib.parse import urlsplit

PROTECTED_HOSTS={"mail.google.com","calendar.google.com","docs.google.com","drive.google.com","accounts.google.com","localhost","127.0.0.1"}
TOKENS=("/account","/signin","/login","/inbox","bank","wealth","mychart","booking-details","boarding","itinerary","confirmation","receipt")

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
   set output to output & wi & delim & ti & delim & (title of t) & delim & (URL of t) & linefeed
  end repeat
 end repeat
 return output
end tell
"""

CLOSE_ONE = """
on run argv
 set targetURL to item 1 of argv
 tell application "Google Chrome"
  repeat with wi from (count of windows) to 1 by -1
   tell window wi
    repeat with ti from (count of tabs) to 1 by -1
     if URL of tab ti is targetURL then
      close tab ti
      return "closed"
     end if
    end repeat
   end tell
  end repeat
 end tell
 return "not-found"
end run
"""

def osa(script,args=None):
 r=subprocess.run(["osascript","-"]+(args or []),input=script,text=True,capture_output=True)
 if r.returncode: raise RuntimeError(r.stderr.strip())
 return r.stdout.strip()

def protected(url):
 try:
  p=urlsplit(url); host=p.hostname or ""; low=url.lower()
  return p.scheme.lower()=="file" or host in PROTECTED_HOSTS or any(x in low for x in TOKENS)
 except Exception: return True

def action(r):
 if r.get("decision")!="CLOSE": return "LEAVE"
 if protected(r.get("url","")): return "LEAVE_PROTECTED"
 if r.get("method")=="local" and r.get("reason") in {"Duplicate normalized URL","Blank tab"}: return "CLOSE"
 if r.get("method")=="qwen": return "ARCHIVE_THEN_CLOSE"
 return "LEAVE_REVIEW"

def archive_html(rows,path):
 a=[r for r in rows if r["action"] in {"ARCHIVE_THEN_CLOSE","CLOSE"}]; g=defaultdict(list)
 for r in a: g[r.get("host") or "(no domain)"].append(r)
 parts=["<!doctype html><html><head><meta charset='utf-8'><title>Archived Chrome Tabs</title>",
 "<style>body{font-family:-apple-system,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px}li{margin:9px 0}.u{font-size:12px;color:#666;word-break:break-all}</style></head><body>",
 f"<h1>Archived Chrome Tabs</h1><p>{len(a)} tabs archived {html.escape(dt.datetime.now().isoformat(timespec='minutes'))}</p>"]
 for host in sorted(g):
  parts.append("<h2>"+html.escape(host)+"</h2><ul>")
  for r in g[host]:
   u=r.get("url",""); title=r.get("title") or u or "(untitled)"
   parts.append("<li><strong>["+html.escape(r["action"])+"]</strong> <a href='"+html.escape(u,quote=True)+"'>"+html.escape(title)+"</a><div class='u'>"+html.escape(u)+"</div></li>")
  parts.append("</ul>")
 parts.append("</body></html>")
 path.write_text("\n".join(parts),encoding="utf-8")
 return len(a)

def main():
 p=argparse.ArgumentParser()
 p.add_argument("report")
 p.add_argument("--apply",action="store_true")
 p.add_argument("--archive")
 a=p.parse_args()
 with open(a.report,newline="",encoding="utf-8") as f: rows=list(csv.DictReader(f))
 for r in rows: r["action"]=action(r)

 # Duplicate-safety invariant.
 # For every normalized URL with multiple open-tab records, force at least
 # one copy to remain open, regardless of Qwen's classification.
 groups=defaultdict(list)
 for r in rows:
  u=r.get("normalized_url","")
  if u: groups[u].append(r)

 duplicate_survivors=0
 for u,grp in groups.items():
  if len(grp)<2: continue

  # Prefer a tab that is already being left open. Otherwise keep the first
  # original occurrence from the report.
  survivor=next((r for r in grp if r["action"] in {
   "LEAVE","LEAVE_PROTECTED","LEAVE_REVIEW","LEAVE_DUPLICATE_SURVIVOR"
  }),None)
  if survivor is None:
   survivor=min(grp,key=lambda r:int(r.get("id") or 10**12))

  # Make the safeguard explicit in the action plan.
  if survivor["action"] not in {"LEAVE","LEAVE_PROTECTED","LEAVE_REVIEW"}:
   survivor["action"]="LEAVE_DUPLICATE_SURVIVOR"
   duplicate_survivors+=1

 stamp=dt.datetime.now().strftime("%Y%m%d_%H%M%S")
 arc=Path(a.archive or f"chrome_tab_archive_{stamp}.html")
 plan=Path(f"chrome_tab_action_plan_{stamp}.csv")
 n=archive_html(rows,arc)
 if not arc.exists() or arc.stat().st_size<100: raise SystemExit("Archive verification failed. Nothing closed.")
 fields=list(rows[0].keys()) if rows else ["action"]
 if "action" not in fields: fields.append("action")
 with open(plan,"w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)
 c=Counter(r["action"] for r in rows)
 print("Archive verified:",arc); print("Archived entries:",n); print("All planned closures are included in the archive.")
 print("Direct CLOSE planned:",c["CLOSE"]); print("ARCHIVE_THEN_CLOSE planned:",c["ARCHIVE_THEN_CLOSE"])
 print("Protected/left open:",c["LEAVE_PROTECTED"]); print("Forced duplicate survivors:",c["LEAVE_DUPLICATE_SURVIVOR"]); print("Action plan:",plan)
 if not a.apply:
  print("DRY RUN. No Chrome tabs were closed."); return
 wanted=Counter(r["url"] for r in rows if r["action"] in {"CLOSE","ARCHIVE_THEN_CLOSE"})
 closed=missing=errors=0
 for url,count in wanted.items():
  for _ in range(count):
   try:
    z=osa(CLOSE_ONE,[url])
    if z=="closed": closed+=1
    else: missing+=1; break
   except Exception: errors+=1; break
 print("Closed:",closed); print("Not found:",missing); print("Errors:",errors); print("Archive remains on disk.")

if __name__=="__main__": main()
