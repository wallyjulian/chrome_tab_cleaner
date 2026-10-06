# Chrome Tab Manager

A macOS command-line tool for cleaning up large numbers of Google Chrome tabs with a local LLM running in LM Studio.

The tool is designed for large Chrome sessions with hundreds or thousands of open tabs. It combines deterministic cleanup rules with AI classification, then creates a reviewable action plan before anything is closed.

The current default model is Qwen 3.8 27B through LM Studio.

## Main safety features

The tool is deliberately conservative.

- No tabs are closed during classification.
- No tabs are closed while the archive and action plan are created.
- Closing requires an explicit `--apply` option.
- Every tab planned for closure is saved in an HTML archive first.
- Sensitive, account, local-file, and similar tabs are protected by local rules.
- Tracking parameters are removed when comparing URLs for duplicates.
- At least one copy of every duplicated normalized URL is kept open.
- An interrupted cleanup can be resumed from the existing action plan.
- The final closing operation uses a single Chrome pass, so it remains practical with thousands of tabs.

## Requirements

- macOS
- Google Chrome
- Python 3.11 or later
- LM Studio
- A suitable local model loaded in LM Studio

The script currently defaults to

``` text
qwen/qwen3.8-27b
```

and expects the LM Studio OpenAI-compatible server at

``` text
http://127.0.0.1:1234/v1
```

No separate Python virtual environment is required if the needed Python version is already available.

## Script

The standalone program is

``` text
chrome_tab_manager_standalone.py
```

It contains the full workflow. The older classifier, planner, and resume scripts are not required.

To see the available commands, run

``` bash
python3 chrome_tab_manager_standalone.py --help
```

## Recommended workflow

The cleanup has four stages.

### 1. Classify the current tabs

Start LM Studio and load the Qwen model. Then run

``` bash
python3 chrome_tab_manager_standalone.py classify
```

The script first handles tabs that can be classified locally. This includes duplicate normalized URLs, blank tabs, and protected pages.

The remaining tabs are sent to Qwen in batches. Qwen classifies each tab as `KEEP`, `CLOSE`, or `REVIEW`.

The default batch size is 40. It can be changed with

``` bash
python3 chrome_tab_manager_standalone.py classify --batch-size 50
```

The result is a timestamped classification report such as

``` text
chrome_tab_report_20261005_143409.csv
```

No Chrome tabs are closed during this stage.

### 2. Build the archive and action plan

Use the classification report from the previous stage.

``` bash
python3 chrome_tab_manager_standalone.py plan \
  chrome_tab_report_20261005_143409.csv
```

This creates two important files.

``` text
chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
chrome_tab_archive_YYYYMMDD_HHMMSS.html
```

The action plan contains the final action for each tab.

Typical actions include

- `LEAVE`
- `LEAVE_PROTECTED`
- `LEAVE_REVIEW`
- `LEAVE_DUPLICATE_SURVIVOR`
- `CLOSE`
- `ARCHIVE_THEN_CLOSE`

The HTML archive contains every tab that the plan proposes closing. This includes tabs classified as duplicates or blank tabs.

Open the archive before applying the plan.

For example

``` bash
open chrome_tab_archive_YYYYMMDD_HHMMSS.html
```

Check several links and make sure the archive provides a useful recovery record.

No Chrome tabs are closed during the planning stage.

## Duplicate protection

URLs are normalized before duplicate detection. Common tracking parameters such as `utm_source`, `utm_medium`, `fbclid`, and similar values are ignored.

For example, these may be treated as copies of the same page.

``` text
https://example.com/article?utm_source=email
https://example.com/article?utm_source=substack
```

The final action plan has an additional safety rule.

If several tabs have the same normalized URL, at least one copy must remain open.

If necessary, the surviving tab receives the action

``` text
LEAVE_DUPLICATE_SURVIVOR
```

This rule overrides a Qwen `CLOSE` classification when needed. Duplicate cleanup therefore cannot intentionally close the last copy represented in the original action plan.

## Protected tabs

Some tabs are protected by deterministic rules rather than left to the LLM.

Examples include

- Gmail and Google account pages
- Google Calendar and Google Docs
- login and account pages
- banking and wealth-management pages
- booking and confirmation pages
- local `file://` pages
- localhost pages
- GitHub pages

The protection rules are intentionally conservative. They can be edited in the script if different behavior is desired.

## 3. Check the action plan against Chrome

Before closing anything, compare the saved action plan with the tabs currently open.

``` bash
python3 chrome_tab_manager_standalone.py status \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

Typical output looks like

``` text
Live Chrome tabs: 2565
Original planned closures: 989
Estimated already closed: 154
Remaining planned closures found live: 835
Planned tabs no longer found: 0
```

This command creates no files and closes no tabs.

The same reconciliation mechanism makes it possible to resume an interrupted cleanup.

## 4. Dry-run the apply operation

Before actually closing tabs, run

``` bash
python3 chrome_tab_manager_standalone.py apply \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

Without `--apply`, this is a dry run.

It reports how many planned tabs are still present but closes nothing.

The command should finish with a message indicating that no files were created and no tabs were closed.

## Apply the cleanup

After reviewing the archive and checking the dry run, apply the plan with

``` bash
python3 chrome_tab_manager_standalone.py apply \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv \
  --apply
```

Only tabs that are both scheduled for closure in the action plan and still present in Chrome are selected.

The script closes the remaining tabs in one AppleScript pass. Tabs are processed backward by window and tab index so that closing one tab does not invalidate the coordinates of tabs still to be closed.

The existing archive and action-plan files are not modified.

## Interrupted cleanup

If an apply operation is interrupted, keep the existing action-plan CSV and HTML archive.

Do not rerun classification merely to resume the cleanup.

First check the existing plan.

``` bash
python3 chrome_tab_manager_standalone.py status \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

The script compares the original plan with the current live Chrome tabs and estimates how many planned closures have already occurred.

Then dry-run the remaining work.

``` bash
python3 chrome_tab_manager_standalone.py apply \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

If the counts look correct, resume with

``` bash
python3 chrome_tab_manager_standalone.py apply \
  chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv \
  --apply
```

## Files worth keeping

After a cleanup, keep at least these two files.

``` text
chrome_tab_action_plan_YYYYMMDD_HHMMSS.csv
chrome_tab_archive_YYYYMMDD_HHMMSS.html
```

The HTML file is the recovery record for closed tabs.

The CSV records why each tab was kept or selected for closure.

The original classification report can also be retained if you want a record of the model's decisions before the final safety rules were applied.

## Changing the model

The default model can be overridden when classifying.

For example

``` bash
python3 chrome_tab_manager_standalone.py classify \
  --model qwen/qwen3.8-27b
```

The LM Studio endpoint can also be changed.

``` bash
python3 chrome_tab_manager_standalone.py classify \
  --base http://127.0.0.1:1234/v1
```

## Performance

The expensive stage is AI classification.

Local rules reduce the number of tabs that need to be sent to Qwen. The remaining tabs are processed in batches.

The actual closing operation is much faster. It first identifies the exact live tabs that remain in the action plan, then closes them in one AppleScript invocation rather than launching AppleScript separately for every tab.

This distinction matters when Chrome contains thousands of tabs.

## Suggested Git workflow

A typical project directory might contain

``` text
chrome_tab_manager_standalone.py
README.md
```

Generated reports and archives can be excluded from Git if they contain private browsing information.

For example, consider adding patterns like these to `.gitignore`.

``` gitignore
chrome_tab_report_*.csv
chrome_tab_action_plan_*.csv
chrome_tab_archive_*.html
```

The generated files can contain page titles and full URLs. Treat them as private data, especially if URLs contain account, booking, document, or session information.

## Important caution

Review the HTML archive and action plan before using `--apply`.

Browser state can change between classification and application. The tool reconciles the saved plan against the current Chrome session and only closes matching live tabs, but the archive and action plan remain the primary recovery and audit records.

# Firefox workflow

Firefox uses WebDriver BiDi rather than Chrome's AppleScript tab API. The Firefox manager uses the same conservative classify, archive, review, and apply workflow.

## Requirements

The Firefox workflow needs Python 3.11 or later, the `websockets` package, Firefox, and LM Studio for AI classification.

Install `websockets` if needed.

``` bash
python3 -m pip install websockets
```

The default LM Studio model in the script is `qwen/qwen3.8-27b` and the default API endpoint is `http://127.0.0.1:1234/v1`.

## Start Firefox with WebDriver BiDi

Quit Firefox first. Then launch the desired Firefox profile from Terminal with remote debugging enabled.

``` bash
/Applications/Firefox.app/Contents/MacOS/firefox \
  -profile ~/Library/Application\\ Support/Firefox/Profiles/default.iy5 \
  --remote-debugging-port=9222
```

Wait for Firefox to report

``` text
WebDriver BiDi listening on ws://127.0.0.1:9222
```

The profile path above is an example from the machine used to develop this project. Use the correct Firefox profile path on another machine.

The manager explicitly ends each BiDi session. This is important because Firefox may otherwise report `Maximum number of active sessions` on the next command.

## 1. Classify Firefox tabs

Make sure LM Studio is running, then run

``` bash
python3 python/firefox_tab_manager.py classify
```

The manager reads the current top-level Firefox tabs, handles simple cases locally, protects sensitive pages, and sends the remaining URLs to Qwen in batches.

The output uses the same three decisions as the Chrome workflow.

``` text
KEEP
CLOSE
REVIEW
```

Classification does not close any tabs. It creates a timestamped report such as

``` text
firefox_tab_report_YYYYMMDD_HHMMSS.csv
```

## 2. Create the Firefox archive and action plan

Use the report from the classification step.

``` bash
python3 python/firefox_tab_manager.py plan \
  firefox_tab_report_YYYYMMDD_HHMMSS.csv
```

This creates two files.

``` text
firefox_tab_archive_YYYYMMDD_HHMMSS.html
firefox_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

All planned closures are included in the HTML archive, including direct `CLOSE` actions and `ARCHIVE_THEN_CLOSE` actions.

No Firefox tabs are closed during this step.

Open the archive before applying the plan.

``` bash
open firefox_tab_archive_YYYYMMDD_HHMMSS.html
```

The same duplicate-survivor rule used by the Chrome workflow is applied when the plan is built. At least one copy of each normalized duplicate group is forced to remain open when necessary.

## 3. Check the Firefox action plan

Run

``` bash
python3 python/firefox_tab_manager.py status \
  firefox_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

This compares the saved plan with the current Firefox tabs and closes nothing.

Firefox BiDi context IDs are session-specific. The manager therefore falls back to exact URL matching when an original context ID is no longer valid.

### Current status limitation

Treat `status` as advisory after Firefox has restarted or after tabs have been closed. Exact URL fallback can be ambiguous when several live tabs have the same URL. A reported remaining match does not always prove that the original planned tab is still open.

Do not repeatedly run `--apply` merely because a later `status` command reports a small number of remaining matches. Review those cases first.

## 4. Dry-run the Firefox apply operation

Run

``` bash
python3 python/firefox_tab_manager.py apply \
  firefox_tab_action_plan_YYYYMMDD_HHMMSS.csv
```

Without `--apply`, the command closes nothing.

The apply implementation enumerates and reconciles the live Firefox tabs inside one WebDriver BiDi session. This matters because Firefox context IDs cannot safely be carried from one BiDi session into another.

## 5. Apply the Firefox cleanup

After reviewing the archive and dry run, run

``` bash
python3 python/firefox_tab_manager.py apply \
  firefox_tab_action_plan_YYYYMMDD_HHMMSS.csv \
  --apply
```

The manager gets the current tab contexts, matches the action plan, and closes the selected contexts inside the same BiDi session.

The existing archive and action-plan files are not modified.

## Firefox troubleshooting

If Firefox reports

``` text
session not created: Maximum number of active sessions
```

make sure you are using the current manager, which calls `session.end` when it disconnects. If an older script left an orphaned session, quit Firefox completely and relaunch it with `--remote-debugging-port=9222`.

If Firefox reports that the profile cannot be loaded when launched from a third-party terminal, macOS privacy permissions may be blocking access to the Firefox profile. Apple's Terminal worked during development after it had the needed file access. Do not change profile ownership or permissions until macOS privacy access has been checked.

If an apply attempt reports `no such frame`, use the current manager. The final apply implementation discovers and closes contexts within the same BiDi session. Earlier development versions incorrectly carried context IDs between sessions.

## Generated Firefox files

The generated report, archive, and action-plan files are working data and normally should not be committed to Git. Suggested `.gitignore` entries are

``` gitignore
firefox_tab_report_*.csv
firefox_tab_action_plan_*.csv
firefox_tab_archive_*.html
```
