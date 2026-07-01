import gspread
from google.oauth2.service_account import Credentials
import anthropic
import pandas as pd
from datetime import datetime, timedelta
import json
import os
import re
import argparse
import time
import random
import hashlib

# Load .env variables
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                try:
                    key, value = line.strip().split("=", 1)
                    os.environ[key] = value.strip('"').strip("'")
                except ValueError:
                    pass

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gspread_client = gspread.authorize(creds)
workbook = gspread_client.open("Child Watch Schedule")

# Initialize Claude Client safely
api_key = os.environ.get("ANTHROPIC_API_KEY")
ai_client = None
if api_key:
    try:
        ai_client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        print(f"[WARN] Claude client init failed: {e}")
        ai_client = None

CLAUDE_MODEL = "claude-opus-4-8"

# Errors where retrying the exact same request will never succeed — a bad request, an invalid
# or unauthorized API key, or a model/resource that doesn't exist. Distinct from rate limits,
# server errors, and connection/timeout issues, which ARE worth retrying and fall through to
# the generic retry-with-backoff path instead.
NON_RETRYABLE_CLAUDE_ERRORS = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)

def fetch_clean_dataframe(worksheet_name, fallback_columns):
    """Fetches worksheet data, strips whitespace, and enforces baseline schema."""
    try:
        worksheet = workbook.worksheet(worksheet_name)
        records = worksheet.get_all_records()
        if not records:
            return pd.DataFrame(columns=fallback_columns)
        df = pd.DataFrame(records)
        df.columns = df.columns.str.strip()
        for col in df.select_dtypes(include=['object', 'string']):
            df[col] = df[col].astype(str).str.strip()
        for col in fallback_columns:
            if col not in df.columns:
                df[col] = ""
        return df
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame(columns=fallback_columns)

def calculate_hours(start_str, end_str):
    """Calculates the duration of a shift in hours."""
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            start = datetime.strptime(start_str.strip(), fmt)
            end = datetime.strptime(end_str.strip(), fmt)
            if end <= start:
                end += timedelta(days=1)
            return (end - start).total_seconds() / 3600.0
        except ValueError:
            continue
    return 0.0

def normalize_date_string(value):
    """Normalizes a date string to zero-padded 'YYYY-MM-DD', returning '' for anything blank
    or unparseable. Accepts multiple common input formats. The canonical output form matters
    because every date comparison in this app is a plain string comparison — '2026-10-01' <
    '2026-9-15' as strings (chronologically backwards), so always compare the normalized form."""
    value = str(value).strip()
    if not value or value.lower() in ("nan", "none"):
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

def normalize_name(name):
    """Lowercase-stripped employee name for case-insensitive comparisons.
    Use original casing for storage and display; only use this for dict-key lookups."""
    return str(name).strip().lower()

def local_python_date_parse(deviations_text):
    """Optimization: Extract standalone explicit YYYY-MM-DD dates directly via regex to guarantee accuracy."""
    if not deviations_text:
        return []
    return re.findall(r'\b\d{4}-\d{2}-\d{2}\b', deviations_text)

RULES_CACHE_SHEET = "Rules Cache"

def hash_rules_text(rules, deviations):
    """Stable fingerprint of an employee's rules text, used to detect unchanged rules between runs."""
    return hashlib.sha256(f"{rules}|{deviations}".encode("utf-8")).hexdigest()

def get_or_create_rules_cache_sheet():
    try:
        return workbook.worksheet(RULES_CACHE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=RULES_CACHE_SHEET, rows=100, cols=3)
        sheet.update(range_name='A1', values=[["Employee Name", "Rules Hash", "Cached Profile"]], value_input_option="USER_ENTERED")
        return sheet

def load_rules_cache():
    sheet = get_or_create_rules_cache_sheet()
    records = sheet.get_all_records()
    cache = {}
    for row in records:
        name = str(row.get("Employee Name", "")).strip()
        if not name:
            continue
        cache[name] = {"hash": row.get("Rules Hash", ""), "profile": row.get("Cached Profile", "")}
    return cache

def save_rules_cache(cache):
    sheet = get_or_create_rules_cache_sheet()
    rows = [["Employee Name", "Rules Hash", "Cached Profile"]]
    for name, entry in cache.items():
        rows.append([name, entry["hash"], entry["profile"]])
    sheet.clear()
    sheet.update(range_name='A1', values=rows, value_input_option="USER_ENTERED")

HISTORY_SHEET = "History"
HISTORY_HEADERS = ["Timestamp", "Method", "Date", "Day of Week", "Start Time", "Old Employee", "New Employee", "Edited By", "Details"]
APP_EDITED_BY = "App (UI)"

def get_or_create_history_sheet():
    try:
        return workbook.worksheet(HISTORY_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=HISTORY_SHEET, rows=1000, cols=len(HISTORY_HEADERS))
        sheet.update(range_name='A1', values=[HISTORY_HEADERS], value_input_option="USER_ENTERED")
        return sheet

def log_history_entries(entries):
    """Appends rows to the History sheet. Each entry is a dict keyed by the lowercase,
    underscored version of a HISTORY_HEADERS column (e.g. 'old_employee' -> 'Old Employee').
    No-ops on an empty list so callers don't need to guard every call site."""
    if not entries:
        return
    sheet = get_or_create_history_sheet()
    key_map = {h: h.lower().replace(" ", "_") for h in HISTORY_HEADERS}
    rows = [[str(entry.get(key_map[h], "")) for h in HISTORY_HEADERS] for entry in entries]
    # RAW (not USER_ENTERED) so Sheets stores the Timestamp as literal text instead of parsing
    # it into a date/time value and re-displaying it in the sheet's own (24-hour) number format,
    # which would silently override the "%I:%M:%S %p" string we deliberately constructed.
    sheet.append_rows(rows, value_input_option="RAW")

def fetch_history():
    """Returns the full History log as a DataFrame, newest entries first. Includes a
    '_sheet_row' column (the entry's literal row number in the History sheet, 2-indexed past
    the header) so callers can delete specific entries later without re-deriving their position
    after the newest-first sort reorders everything."""
    sheet = get_or_create_history_sheet()
    records = sheet.get_all_records()
    df = pd.DataFrame(records, columns=HISTORY_HEADERS)
    if df.empty:
        df["_sheet_row"] = []
        return df
    df["_sheet_row"] = range(2, 2 + len(df))
    df["_sort_ts"] = pd.to_datetime(df["Timestamp"], format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
    df = df.sort_values("_sort_ts", ascending=False, na_position="last").drop(columns="_sort_ts")
    return df.reset_index(drop=True)

def count_unseen_direct_edits(history_df, last_seen_timestamp):
    """Counts 'Direct Sheet Edit' entries strictly newer than last_seen_timestamp, and returns
    the latest Direct Sheet Edit timestamp seen (for the caller to persist as the new
    last-seen marker). Deliberately timestamp-based rather than a raw count: a count-based
    'last seen N entries' goes stale the moment any History row is deleted — the total can drop
    below N and then silently fail to flag a genuinely new direct edit that happens afterward.
    Comparing by timestamp identity has no such failure mode."""
    if history_df.empty:
        return 0, last_seen_timestamp

    direct_edits = history_df[history_df["Method"] == "Direct Sheet Edit"]
    if direct_edits.empty:
        return 0, last_seen_timestamp

    parsed = pd.to_datetime(direct_edits["Timestamp"], format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
    latest_timestamp = direct_edits.loc[parsed.idxmax(), "Timestamp"] if not parsed.isna().all() else last_seen_timestamp

    if not last_seen_timestamp:
        return len(direct_edits), latest_timestamp

    last_seen_parsed = pd.to_datetime(last_seen_timestamp, format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
    if pd.isna(last_seen_parsed):
        return len(direct_edits), latest_timestamp

    unseen_count = int((parsed > last_seen_parsed).sum())
    return unseen_count, latest_timestamp

def delete_history_rows(sheet_rows):
    """Deletes the given literal History-sheet row numbers (as produced by fetch_history()'s
    '_sheet_row' column), in a single batched API call regardless of how many rows are deleted.
    Requests are ordered highest row number first — within one batchUpdate call, Google Sheets
    still applies requests in list order against the progressively-updated state, so an earlier
    (smaller) row number must never be deleted before a later (larger) one, or it would shift
    the larger one's position out from under it."""
    if not sheet_rows:
        return
    sheet = get_or_create_history_sheet()
    # int(...) matters: row numbers arriving from a pandas column (e.g. fetch_history()'s
    # '_sheet_row') are numpy.int64, which the JSON request body can't serialize.
    rows_descending = sorted({int(r) for r in sheet_rows}, reverse=True)
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "ROWS",
                    "startIndex": row - 1,  # batchUpdate ranges are 0-indexed
                    "endIndex": row,
                }
            }
        }
        for row in rows_descending
    ]
    workbook.batch_update({"requests": requests})

APP_STATE_SHEET = "App State"

def get_or_create_app_state_sheet():
    try:
        return workbook.worksheet(APP_STATE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=APP_STATE_SHEET, rows=20, cols=2)
        sheet.update(range_name='A1', values=[["Key", "Value"]], value_input_option="USER_ENTERED")
        return sheet

def get_app_state(key, default=""):
    """Small persisted key-value store (e.g. for the 'last seen direct-edit count' notification
    marker) — backed by a sheet tab rather than session state, so it survives app restarts and
    is shared across anyone using the app against this same spreadsheet."""
    sheet = get_or_create_app_state_sheet()
    for row in sheet.get_all_records():
        if str(row.get("Key", "")) == key:
            return row.get("Value", default)
    return default

def set_app_state(key, value):
    sheet = get_or_create_app_state_sheet()
    records = sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row.get("Key", "")) == key:
            sheet.update_cell(i + 2, 2, str(value))
            return
    sheet.append_row([key, str(value)], value_input_option="USER_ENTERED")

def diff_assignment_rows(old_rows, new_rows):
    """Compares two lists of [Date, Day of Week, Day Type, Start Time, End Time, Assigned
    Employee, Issues] rows (the same shape write_to_spreadsheet writes) and returns a list of
    (date, day_of_week, start_time, old_employee, new_employee) tuples for every slot whose
    Assigned Employee actually changed. Matches slots by (Date, Start Time) plus occurrence
    order within that pair — needed because Staff Required > 1 can put multiple rows at the
    same Date/Start Time — so this is robust to row reordering, not just positional diffing."""
    from collections import defaultdict

    def group(rows):
        groups = defaultdict(list)
        for row in rows:
            date, day_of_week, start_time = row[0], row[1], row[3]
            assigned = row[5]
            groups[(date, start_time)].append((day_of_week, assigned))
        return groups

    old_groups = group(old_rows)
    new_groups = group(new_rows)

    changes = []
    for key in set(old_groups) | set(new_groups):
        date, start_time = key
        old_slots = old_groups.get(key, [])
        new_slots = new_groups.get(key, [])
        for i in range(max(len(old_slots), len(new_slots))):
            old_day, old_emp = old_slots[i] if i < len(old_slots) else (None, "")
            new_day, new_emp = new_slots[i] if i < len(new_slots) else (None, "")
            old_emp = "" if old_emp in ("", "GAP") else old_emp
            new_emp = "" if new_emp in ("", "GAP") else new_emp
            if old_emp != new_emp:
                day_of_week = new_day or old_day or ""
                changes.append((date, day_of_week, start_time, old_emp, new_emp))
    return changes

VALID_DAY_NAMES = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}

RULES_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "profiles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "forbidden_days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
                    },
                    "allowed_days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
                    },
                    "time_restriction": {"type": "string", "enum": ["any", "morning", "afternoon"]},
                    "vacation_dates": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["name", "forbidden_days", "allowed_days", "time_restriction", "vacation_dates"],
                "additionalProperties": False
            }
        }
    },
    "required": ["profiles"],
    "additionalProperties": False
}

def is_valid_parsed_profile(profile):
    """Defense in depth: the structured-output schema constrains forbidden_days/allowed_days to
    weekday-name enums, so a malformed profile (e.g. dates instead of day names) shouldn't reach this
    point anymore — but a cached profile written before this validator existed could still be stale,
    so keep re-checking shape before trusting or reusing a cached entry."""
    if not isinstance(profile, dict):
        return False
    for field in ("forbidden_days", "allowed_days"):
        values = profile.get(field, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.lower() in VALID_DAY_NAMES for v in values):
            return False
    if profile.get("time_restriction", "any") not in ("any", "morning", "afternoon"):
        return False
    vacation_dates = profile.get("vacation_dates", [])
    if not isinstance(vacation_dates, list) or not all(isinstance(d, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', d) for d in vacation_dates):
        return False
    return True

def warn_local_fallback(name):
    print(f"[WARN] '{name}' is using the degraded local keyword fallback instead of Claude-parsed rules. "
          f"This fallback cannot reliably handle implied day ranges (e.g. 'Tuesday through Thursday') and "
          f"treats negation words ('not'/'never'/'no') as applying globally, which can misfire on phrases "
          f"like 'no problem working mornings'. Verify this employee's schedule manually.")

def batch_normalize_rules_with_claude(active_employees, target_year, target_month):
    """Optimization: Batches all active employee rules into a single LLM request to avoid rate limits.
    Rules rarely change between runs, so a persisted cache (keyed by a hash of each employee's rules
    text) lets unchanged employees skip Claude entirely."""
    employee_rules_map = {}
    llm_batch_payload = {}
    pending_hashes = {}

    cache = load_rules_cache()

    for _, emp in active_employees.iterrows():
        name = emp['Employee Name']
        rules = emp['Default Rules'].strip()
        deviations = emp['Blocked Dates'].strip()

        # Local Pre-Filtering
        local_dates = local_python_date_parse(deviations)

        # Safeguard: Bypass Claude entirely if rules are blank
        if not rules and not deviations:
            employee_rules_map[name] = {
                "forbidden_days": [], "allowed_days": [],
                "time_restriction": "any", "vacation_dates": []
            }
            continue

        current_hash = hash_rules_text(rules, deviations)
        cached_entry = cache.get(name)
        if cached_entry and cached_entry["hash"] == current_hash:
            try:
                cached_profile = json.loads(cached_entry["profile"])
                if is_valid_parsed_profile(cached_profile):
                    employee_rules_map[name] = cached_profile
                    continue
                print(f"    ! Discarding invalid cached profile for '{name}' — re-requesting from Claude.")
            except (json.JSONDecodeError, TypeError):
                pass  # Cached profile is unreadable; fall through and re-request from Claude.

        llm_batch_payload[name] = {
            "default_rules": rules,
            "deviations_text": deviations,
            "pre_parsed_dates": local_dates
        }
        pending_hashes[name] = current_hash

    if not llm_batch_payload:
        print(" -> Rules cache: all active employees unchanged since last run. Skipping Claude entirely.")
        return employee_rules_map

    skipped_count = len(active_employees) - len(llm_batch_payload)
    print(f" -> Rules cache: {skipped_count} employee(s) unchanged (skipped), {len(llm_batch_payload)} require Claude parsing.")

    if not ai_client:
        for name in llm_batch_payload:
            employee_rules_map[name] = {"use_local_fallback": True, "raw_rules": llm_batch_payload[name]["default_rules"], "raw_deviations": llm_batch_payload[name]["deviations_text"]}
            warn_local_fallback(name)
        return employee_rules_map

    prompt = f"""
    You are an expert scheduling normalization system. Analyze the rules and deviations for the following list of employees and return one availability profile per employee.

    Context Timeline Window: Year {target_year}, Month {target_month}.

    Input Batch Profiles:
    {json.dumps(llm_batch_payload, indent=2)}

    FIELD DEFINITIONS — read carefully, these fields use two different and non-interchangeable formats:
    - "forbidden_days" and "allowed_days" come ONLY from 'default_rules' and must ALWAYS be full weekday
      names from this fixed set: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].
      NEVER put a calendar date (e.g. '2026-07-01') in either of these two fields, even if the employee is
      only unavailable on specific occurrences of that weekday this month — that distinction belongs in
      'vacation_dates' instead, not in forbidden_days/allowed_days.
    - "vacation_dates" comes ONLY from 'deviations_text' and 'pre_parsed_dates', and must ALWAYS be explicit
      'YYYY-MM-DD' calendar date strings — never a weekday name.

    CRITICAL CONSTRAINTS:
    1. Fully expand any implied date ranges found in 'deviations_text' into distinct explicit 'YYYY-MM-DD'
       strings inside 'vacation_dates' (e.g., 'July 1 to July 3' becomes ['2026-07-01', '2026-07-02', '2026-07-03']).
       This expansion applies ONLY to 'vacation_dates' — never expand a weekday rule from 'default_rules' into
       a list of dates.
    2. Incorporate and pass along any dates provided in 'pre_parsed_dates' into 'vacation_dates'.
    3. Return one profile object per employee in 'Input Batch Profiles', with "name" set to that employee's exact name.
    """

    max_retries = 3
    base_delay = 2

    for attempt in range(max_retries):
        try:
            print(f" -> Sending consolidated batch rules request to Claude (Attempt {attempt+1}/{max_retries})...")
            response = ai_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                output_config={"format": {"type": "json_schema", "schema": RULES_PROFILE_SCHEMA}},
                messages=[{"role": "user", "content": prompt}]
            )
            text = next(b.text for b in response.content if b.type == "text")
            parsed_batch = {p["name"]: {k: v for k, v in p.items() if k != "name"} for p in json.loads(text)["profiles"]}

            for name in llm_batch_payload:
                if name in parsed_batch and is_valid_parsed_profile(parsed_batch[name]):
                    employee_rules_map[name] = parsed_batch[name]
                    cache[name] = {"hash": pending_hashes[name], "profile": json.dumps(parsed_batch[name])}
                else:
                    if name in parsed_batch:
                        print(f"    ! Claude returned a malformed profile for '{name}' — falling back to local parsing instead of caching it.")
                    employee_rules_map[name] = {"use_local_fallback": True, "raw_rules": llm_batch_payload[name]["default_rules"], "raw_deviations": llm_batch_payload[name]["deviations_text"]}
                    warn_local_fallback(name)

            save_rules_cache(cache)
            print("    Successfully synchronized all employee profiles in a single network transaction.")
            return employee_rules_map

        except NON_RETRYABLE_CLAUDE_ERRORS as e:
            # Fail fast — a 400/401/403/404 will not resolve itself on retry, so don't burn
            # 3 attempts' worth of backoff delay before falling back to local parsing.
            print(f"    !! Non-retryable Claude API error ({type(e).__name__}): {e}")
            print("    !! Activating global keyword degradation engine immediately — retrying would not help.")
            for name in llm_batch_payload:
                employee_rules_map[name] = {"use_local_fallback": True, "raw_rules": llm_batch_payload[name]["default_rules"], "raw_deviations": llm_batch_payload[name]["deviations_text"]}
                warn_local_fallback(name)
            return employee_rules_map

        except Exception as e:
            print(f"    ! Batch request validation failure on attempt {attempt+1}: {e}")
            if attempt < max_retries - 1:
                sleep_time = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
            else:
                print("    !! Batch parsing failed. Activating global keyword degradation engine.")
                for name in llm_batch_payload:
                    employee_rules_map[name] = {"use_local_fallback": True, "raw_rules": llm_batch_payload[name]["default_rules"], "raw_deviations": llm_batch_payload[name]["deviations_text"]}
                    warn_local_fallback(name)
                return employee_rules_map

def is_available(parsed_rules, slot):
    """Evaluates availability using either Claude-parsed JSON or local keyword parsing."""
    day_of_week = slot['Day of Week'].lower()
    date_str = slot['Date']
    start_time = slot['Start Time'].lower()

    if parsed_rules.get("use_local_fallback", False):
        default_rules = parsed_rules.get("raw_rules", "").lower()
        deviations = parsed_rules.get("raw_deviations", "")

        days_list = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        days_mentioned = [d for d in days_list if d in default_rules]

        if "weekend" in default_rules:
            if any(neg in default_rules for neg in ["not", "never", "no"]):
                if day_of_week in ['saturday', 'sunday']: return False
            else:
                if day_of_week not in ['saturday', 'sunday']: return False

        if days_mentioned:
            if any(neg in default_rules for neg in ["not", "never", "no"]):
                if day_of_week in days_mentioned: return False
            else:
                if day_of_week not in days_mentioned: return False

        if "morning" in default_rules and "pm" in start_time: return False
        if ("afternoon" in default_rules or "evening" in default_rules) and "am" in start_time: return False

        if deviations:
            date_segments = re.findall(r'(\d{4}-\d{2}-\d{2})', deviations)
            current_date = datetime.strptime(date_str, "%Y-%m-%d")
            for i in range(0, len(date_segments) - 1, 2):
                if datetime.strptime(date_segments[i], "%Y-%m-%d") <= current_date <= datetime.strptime(date_segments[i+1], "%Y-%m-%d"):
                    return False
            # Handle any trailing unpaired date (odd-length list) as a single blocked date
            if len(date_segments) % 2 == 1:
                if datetime.strptime(date_segments[-1], "%Y-%m-%d") == current_date:
                    return False
        return True

    if date_str in parsed_rules.get("vacation_dates", []): return False

    forbidden = [d.lower() for d in parsed_rules.get("forbidden_days", [])]
    if day_of_week in forbidden: return False

    allowed = [d.lower() for d in parsed_rules.get("allowed_days", [])]
    if allowed and day_of_week not in allowed: return False

    restriction = parsed_rules.get("time_restriction", "any")
    if restriction == "morning" and "pm" in start_time: return False
    if restriction == "afternoon" and "am" in start_time: return False
    return True

def build_empty_schedule_matrix(target_year, target_month, templates_df=None, holidays_df=None):
    if templates_df is None or holidays_df is None:
        _, templates_df, holidays_df = load_tab_data()
    if templates_df.empty: return []

    start_date = datetime(target_year, target_month, 1)
    end_date = datetime(target_year + 1, 1, 1) if target_month == 12 else datetime(target_year, target_month + 1, 1)
    
    # Pre-index holidays as a dict for O(1) daily lookup instead of O(n) DataFrame scan
    holiday_map = {}
    if not holidays_df.empty and 'Date' in holidays_df.columns:
        holiday_map = dict(zip(holidays_df['Date'].astype(str), holidays_df['Override Type'].astype(str)))

    new_assignments = []
    current = start_date

    while current < end_date:
        date_str = current.strftime("%Y-%m-%d")
        day_of_week = current.strftime("%A")
        current += timedelta(days=1)

        if day_of_week == "Sunday": continue

        override_type = holiday_map.get(date_str)

        if override_type == "Closed": continue
        
        active_day_type = "Saturday" if (day_of_week == "Saturday" or override_type == "Saturday Template") else "Weekday"
        matching_shifts = templates_df[templates_df['Day Type'] == active_day_type]

        for _, shift in matching_shifts.iterrows():
            try:
                staff_needed = int(shift['Staff Required'])
            except (ValueError, TypeError):
                staff_needed = 1
            for _ in range(staff_needed):
                new_assignments.append({
                    "Date": date_str, "Day of Week": day_of_week, "Day Type": active_day_type,
                    "Start Time": shift['Start Time'], "End Time": shift['End Time'],
                    "Assigned Employee": "", "Issues": "GAP"
                })
    return new_assignments

def load_tab_data():
    employee_cols = ['Employee Name', 'Status', 'Default Rules', 'Blocked Dates', 'Min Hours', 'Max Hours', 'Start Date']
    template_cols = ['Day Type', 'Start Time', 'End Time', 'Staff Required']
    holiday_cols = ['Date', 'Day of Week', 'Day Type', 'Name', 'Override Type']
    return fetch_clean_dataframe("Employees", employee_cols), fetch_clean_dataframe("Shift Templates", template_cols), fetch_clean_dataframe("Holidays", holiday_cols)

def _normalize_field_for_diff(col, val):
    """Normalize a single field value for apples-to-apples diffing across sheets."""
    s = str(val).strip()
    if s.lower() in ('nan', 'none', ''):
        return ''
    if col == 'Status':
        return s.lower()
    if col == 'Start Date':
        return normalize_date_string(s) or s
    if col in ('Min Hours', 'Max Hours', 'Staff Required'):
        try:
            f = float(s)
            return str(int(f)) if f == int(f) else str(round(f, 2))
        except (ValueError, TypeError):
            return s
    return s

def log_employee_changes(old_df, new_df, edited_by=APP_EDITED_BY):
    """Diffs old vs new Employees DataFrames and appends adds/edits/removals to History."""
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    employee_cols = ['Employee Name', 'Status', 'Default Rules', 'Blocked Dates', 'Min Hours', 'Max Hours', 'Start Date']
    entries = []

    def norm(row, col):
        return _normalize_field_for_diff(col, row.get(col, ''))

    old_map = {_normalize_field_for_diff('Employee Name', r.get('Employee Name', '')): dict(r)
               for _, r in old_df.iterrows() if str(r.get('Employee Name', '')).strip()}
    new_map = {_normalize_field_for_diff('Employee Name', r.get('Employee Name', '')): dict(r)
               for _, r in new_df.iterrows() if str(r.get('Employee Name', '')).strip()}

    for key, new_row in new_map.items():
        name = str(new_row.get('Employee Name', '')).strip()
        if key not in old_map:
            entries.append({"timestamp": timestamp, "method": "Employee Added",
                            "date": "", "day_of_week": "", "start_time": "",
                            "old_employee": "", "new_employee": name,
                            "edited_by": edited_by, "details": f"New employee added: {name}"})
        else:
            old_row = old_map[key]
            changes = [f"{col}: {norm(old_row, col)} → {norm(new_row, col)}"
                       for col in employee_cols
                       if norm(old_row, col) != norm(new_row, col)]
            if changes:
                entries.append({"timestamp": timestamp, "method": "Employee Updated",
                                "date": "", "day_of_week": "", "start_time": "",
                                "old_employee": name, "new_employee": name,
                                "edited_by": edited_by, "details": "; ".join(changes)})

    for key, old_row in old_map.items():
        if key not in new_map:
            name = str(old_row.get('Employee Name', '')).strip()
            entries.append({"timestamp": timestamp, "method": "Employee Removed",
                            "date": "", "day_of_week": "", "start_time": "",
                            "old_employee": name, "new_employee": "",
                            "edited_by": edited_by, "details": f"Employee removed: {name}"})

    try:
        log_history_entries(entries)
    except Exception as e:
        print(f"[WARN] Failed to log employee changes to History: {e}")

def log_template_changes(old_df, new_df, edited_by=APP_EDITED_BY):
    """Diffs old vs new Shift Templates DataFrames and appends adds/edits/removals to History."""
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    template_cols = ['Day Type', 'Start Time', 'End Time', 'Staff Required']
    entries = []

    def norm(row, col):
        return _normalize_field_for_diff(col, row.get(col, ''))

    def make_key(row):
        return (str(row.get('Day Type', '')).strip().lower(), str(row.get('Start Time', '')).strip().lower())

    old_map = {make_key(r): dict(r) for _, r in old_df.iterrows() if str(r.get('Day Type', '')).strip()}
    new_map = {make_key(r): dict(r) for _, r in new_df.iterrows() if str(r.get('Day Type', '')).strip()}

    for key, new_row in new_map.items():
        day_type = str(new_row.get('Day Type', '')).strip()
        start_time = str(new_row.get('Start Time', '')).strip()
        if key not in old_map:
            entries.append({"timestamp": timestamp, "method": "Template Added",
                            "date": "", "day_of_week": day_type, "start_time": start_time,
                            "old_employee": "", "new_employee": "",
                            "edited_by": edited_by,
                            "details": f"New shift: {day_type} {start_time}–{str(new_row.get('End Time', '')).strip()}, Staff: {norm(new_row, 'Staff Required')}"})
        else:
            old_row = old_map[key]
            changes = [f"{col}: {norm(old_row, col)} → {norm(new_row, col)}"
                       for col in template_cols
                       if norm(old_row, col) != norm(new_row, col)]
            if changes:
                entries.append({"timestamp": timestamp, "method": "Template Updated",
                                "date": "", "day_of_week": day_type, "start_time": start_time,
                                "old_employee": "", "new_employee": "",
                                "edited_by": edited_by, "details": "; ".join(changes)})

    for key, old_row in old_map.items():
        if key not in new_map:
            day_type = str(old_row.get('Day Type', '')).strip()
            start_time = str(old_row.get('Start Time', '')).strip()
            entries.append({"timestamp": timestamp, "method": "Template Removed",
                            "date": "", "day_of_week": day_type, "start_time": start_time,
                            "old_employee": "", "new_employee": "",
                            "edited_by": edited_by, "details": f"Shift removed: {day_type} {start_time}"})

    try:
        log_history_entries(entries)
    except Exception as e:
        print(f"[WARN] Failed to log template changes to History: {e}")

def write_templates_to_sheet(templates_df):
    """Overwrites the Shift Templates sheet with the provided dataframe."""
    template_cols = ['Day Type', 'Start Time', 'End Time', 'Staff Required']
    sheet = workbook.worksheet("Shift Templates")
    sheet.clear()
    rows = [template_cols]
    for _, row in templates_df.iterrows():
        staff_val = row.get('Staff Required', '')
        try:
            staff_val = int(float(staff_val)) if str(staff_val).strip() not in ('', 'nan', 'None') else ''
        except (ValueError, TypeError):
            staff_val = ''
        rows.append([
            str(row.get('Day Type', '')).strip(),
            str(row.get('Start Time', '')).strip(),
            str(row.get('End Time', '')).strip(),
            staff_val,
        ])
    sheet.update(range_name='A1', values=rows, value_input_option="USER_ENTERED")

def write_employees_to_sheet(employees_df):
    """Overwrites the Employees sheet with the provided dataframe. Clears existing content first."""
    employee_cols = ['Employee Name', 'Status', 'Default Rules', 'Blocked Dates', 'Min Hours', 'Max Hours', 'Start Date']
    sheet = workbook.worksheet("Employees")
    sheet.clear()
    rows = [employee_cols]
    for _, row in employees_df.iterrows():
        rows.append([str(row.get(col, "")).strip() if str(row.get(col, "")) not in ("nan", "None") else "" for col in employee_cols])
    sheet.update(range_name='A1', values=rows, value_input_option="USER_ENTERED")
    setup_sheet_validation(employees_df)

def setup_sheet_validation(employees_df):
    """Applies data-validation rules to the Employees and Assignments sheets.
    Run after every employee save so the Assigned Employee dropdown in Assignments
    always reflects the current roster."""
    try:
        emp_sheet = workbook.worksheet("Employees")
        asgn_sheet = workbook.worksheet("Assignments")
    except gspread.exceptions.WorksheetNotFound:
        return

    emp_id = emp_sheet.id
    asgn_id = asgn_sheet.id
    # Cover enough rows to avoid rebuilding validation on every save.
    emp_rows = max(len(employees_df) + 10, 200)

    requests = [
        # Employees!B — Status: strict dropdown (active / inactive)
        {"setDataValidation": {
            "range": {"sheetId": emp_id, "startRowIndex": 1, "endRowIndex": 1000, "startColumnIndex": 1, "endColumnIndex": 2},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "active"}, {"userEnteredValue": "inactive"}]}, "showCustomUi": True, "strict": True}
        }},
        # Employees!E — Min Hours: number >= 0 (non-strict so blank is allowed)
        {"setDataValidation": {
            "range": {"sheetId": emp_id, "startRowIndex": 1, "endRowIndex": 1000, "startColumnIndex": 4, "endColumnIndex": 5},
            "rule": {"condition": {"type": "NUMBER_GREATER_EQ", "values": [{"userEnteredValue": "0"}]}, "showCustomUi": True, "strict": False}
        }},
        # Employees!F — Max Hours: number >= 0 (non-strict so blank is allowed)
        {"setDataValidation": {
            "range": {"sheetId": emp_id, "startRowIndex": 1, "endRowIndex": 1000, "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {"condition": {"type": "NUMBER_GREATER_EQ", "values": [{"userEnteredValue": "0"}]}, "showCustomUi": True, "strict": False}
        }},
        # Employees!G — Start Date: must be a valid date (non-strict so blank is allowed)
        {"setDataValidation": {
            "range": {"sheetId": emp_id, "startRowIndex": 1, "endRowIndex": 1000, "startColumnIndex": 6, "endColumnIndex": 7},
            "rule": {"condition": {"type": "DATE_IS_VALID"}, "showCustomUi": True, "strict": False}
        }},
        # Assignments!F — Assigned Employee: dropdown from Employees list (non-strict so
        # empty cells are allowed for unassigned / GAP slots)
        {"setDataValidation": {
            "range": {"sheetId": asgn_id, "startRowIndex": 1, "endRowIndex": 50000, "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"=Employees!$A$2:$A${emp_rows}"}]}, "showCustomUi": True, "strict": False}
        }},
    ]
    workbook.batch_update({"requests": requests})

def run_initialize_blanks(target_year, target_month):
    print(f"Initializing blank matrix for {target_year}-{target_month:02d}...")
    _, templates_df, holidays_df = load_tab_data()
    slots = build_empty_schedule_matrix(target_year, target_month, templates_df, holidays_df)
    if not slots: return
    final_target_rows = [[s['Date'], s['Day of Week'], s['Day Type'], s['Start Time'], s['End Time'], "", "GAP"] for s in slots]
    write_to_spreadsheet(target_year, target_month, final_target_rows, method="Initialize Blanks")

def run_auto_assignment(target_year, target_month, overwrite=False):
    print(f"Running assignment engine for {target_year}-{target_month:02d} (Overwrite={overwrite})...")
    employees_df, templates_df, holidays_df = load_tab_data()
    assignments_sheet = workbook.worksheet("Assignments")
    existing_records = assignments_sheet.get_all_records()
    
    target_prefix = f"{target_year}-{target_month:02d}"
    existing_target_rows = []
    
    if existing_records:
        existing_df = pd.DataFrame(existing_records)
        existing_df.columns = existing_df.columns.str.strip()
        for col in existing_df.select_dtypes(include=['object', 'string']):
            existing_df[col] = existing_df[col].astype(str).str.strip()
        if not overwrite:
            existing_target_rows = existing_df[existing_df['Date'].astype(str).str.startswith(target_prefix)].to_dict('records')

    active_employees = employees_df[employees_df['Status'].str.lower() == 'active']
    
    if len(active_employees) > 35:
        raise Exception(f"Cost Safety Limit Triggered: Active employees count ({len(active_employees)}) exceeds budget protection limit (35). Process terminated.")

    # Execute batched LLM profile optimization call
    employee_rules_map = batch_normalize_rules_with_claude(active_employees, target_year, target_month)

    tracking_hours = {row['Employee Name']: 0.0 for _, row in active_employees.iterrows()}
    tracking_shifts = {row['Employee Name']: 0 for _, row in active_employees.iterrows()}
    # Case-insensitive lookup from normalized name → canonical name (from Employees sheet).
    # Resolves case drift when Assignments has "jane smith" but Employees has "Jane Smith".
    _name_lookup = {normalize_name(n): n for n in tracking_hours}
    daily_assignments = {}
    
    # Pre-cache max/min hours for faster lookup
    max_hours_map = {}
    min_hours_map = {}
    for _, emp in active_employees.iterrows():
        try:
            max_hours_map[emp['Employee Name']] = float(emp['Max Hours']) if emp['Max Hours'] != "" else 999.0
        except ValueError:
            max_hours_map[emp['Employee Name']] = 999.0
        try:
            min_hours_map[emp['Employee Name']] = float(emp['Min Hours']) if emp['Min Hours'] != "" else 0.0
        except ValueError:
            min_hours_map[emp['Employee Name']] = 0.0

    # Pre-cache each employee's earliest eligible date: max(their Start Date, today). Today is
    # an implicit floor for everyone, not just new hires with a Start Date set — otherwise a
    # re-run of Auto-Assign could "fix" a past gap by retroactively staffing a day that already
    # happened, which isn't a real fill, just a fabricated record.
    today_str = datetime.now().strftime("%Y-%m-%d")
    earliest_eligible_map = {}
    for _, emp in active_employees.iterrows():
        start_date = normalize_date_string(emp['Start Date'])
        earliest_eligible_map[emp['Employee Name']] = max(start_date, today_str) if start_date else today_str

    slots_to_process = []
    if overwrite or not existing_target_rows:
        slots_to_process = build_empty_schedule_matrix(target_year, target_month, templates_df, holidays_df)
    else:
        slots_to_process = existing_target_rows

    # Pre-compute shift durations once per (Start Time, End Time) pair instead of
    # re-parsing the same handful of recurring shift templates on every slot.
    shift_duration_cache = {}
    for slot in slots_to_process:
        key = (slot['Start Time'], slot['End Time'])
        if key not in shift_duration_cache:
            shift_duration_cache[key] = calculate_hours(slot['Start Time'], slot['End Time'])

    for slot in slots_to_process:
        raw_name = slot.get('Assigned Employee', '')
        if raw_name and raw_name != "GAP":
            canonical = _name_lookup.get(normalize_name(raw_name), raw_name)
            if canonical != raw_name:
                slot['Assigned Employee'] = canonical
            emp_name = canonical
        else:
            emp_name = raw_name
        if emp_name and emp_name != "GAP" and emp_name in tracking_hours:
            tracking_hours[emp_name] += shift_duration_cache[(slot['Start Time'], slot['End Time'])]
            tracking_shifts[emp_name] += 1
            dt = slot['Date']
            if (dt, emp_name) not in daily_assignments:
                daily_assignments[(dt, emp_name)] = []
            daily_assignments[(dt, emp_name)].append(slot['Start Time'])

    # Track strictly empty slots using references
    unassigned_slots = [s for s in slots_to_process if s.get('Assigned Employee', '') in ["", "GAP"]]

    # --- PASS 1: Mandatory Minimum Guarantee (Strict-First Processing) ---
    print("\n -> [Pass 1] Executing baseline participation guarantee thresholds...")

    # Shuffle before stable-sort so ties in Max Hours are broken randomly,
    # preventing the same employees from always winning the first pick.
    active_employees_pass1 = active_employees.sample(frac=1, random_state=random.randint(0, 9999)).copy()
    def _safe_float_max(x):
        try:
            return float(x) if str(x).strip() not in ("", "nan") else 999.0
        except (ValueError, TypeError):
            return 999.0
    active_employees_pass1['Max Sort'] = active_employees_pass1['Max Hours'].apply(_safe_float_max)
    # Sort restricted (low max-hour) employees FIRST so they claim their narrow pool of
    # compatible slots before flexible, high-max-hour employees consume them. Flexible
    # employees have the most room to absorb whatever is left over in Pass 2.
    sorted_pass1_employees = active_employees_pass1.sort_values(by='Max Sort', ascending=True)

    # Shifts gained in this run (separate from pre-existing tracking_shifts)
    run_shifts = {row['Employee Name']: 0 for _, row in active_employees.iterrows()}

    for _, employee in sorted_pass1_employees.iterrows():
        name = employee['Employee Name']
        target_min = min_hours_map[name]

        # Keep claiming compatible slots until this employee's stated Min Hours is met.
        # Employees with no Min Hours set still get one guaranteed baseline shift
        # (run_shifts[name] == 0) so everyone gets at least some participation.
        while tracking_hours[name] < target_min or run_shifts[name] == 0:
            assigned_this_round = False

            for slot in list(unassigned_slots):
                duration = shift_duration_cache[(slot['Start Time'], slot['End Time'])]
                if tracking_hours[name] + duration > max_hours_map[name]: continue
                if slot['Date'] < earliest_eligible_map[name]: continue
                if not is_available(employee_rules_map[name], slot): continue

                emp_day_shifts = daily_assignments.get((slot['Date'], name), [])
                if slot['Start Time'] in emp_day_shifts: continue
                if len(emp_day_shifts) >= 2: continue

                is_current_am = "am" in slot['Start Time'].lower()
                has_am = any("am" in t.lower() for t in emp_day_shifts)
                has_pm = any("pm" in t.lower() for t in emp_day_shifts)
                if (is_current_am and has_pm) or (not is_current_am and has_am): continue

                slot['Assigned Employee'] = name
                slot['Issues'] = ""
                tracking_hours[name] += duration
                tracking_shifts[name] += 1
                run_shifts[name] += 1

                if (slot['Date'], name) not in daily_assignments:
                    daily_assignments[(slot['Date'], name)] = []
                daily_assignments[(slot['Date'], name)].append(slot['Start Time'])

                print(f"    Fixed baseline shift for {name} on {slot['Date']} at {slot['Start Time']} ({tracking_hours[name]:.1f}/{target_min:.1f} min hrs)")
                unassigned_slots.remove(slot)
                assigned_this_round = True
                break

            # No compatible slot was found this round — further looping won't help,
            # so stop trying for this employee and let the diagnostic flag the shortfall.
            if not assigned_this_round:
                break

    # --- PASS 2: Workload Balancing Optimization ---
    print("\n -> [Pass 2] Optimizing workload balance across remaining unallocated empty slots...")
    for slot in unassigned_slots:
        duration = shift_duration_cache[(slot['Start Time'], slot['End Time'])]
        assigned_name, issue_flag = "", "GAP"
        
        # FIX 3: Break hour ties by shift count, then add jitter so the same
        # zero-hour employees don't always win when multiple are equally underloaded.
        names_shuffled = list(tracking_hours.keys())
        random.shuffle(names_shuffled)
        sorted_employee_names = sorted(
            names_shuffled,
            key=lambda x: (tracking_hours[x], tracking_shifts[x])
        )

        for name in sorted_employee_names:
            if tracking_hours[name] + duration > max_hours_map[name]: continue
            if slot['Date'] < earliest_eligible_map[name]: continue
            if not is_available(employee_rules_map[name], slot): continue

            emp_day_shifts = daily_assignments.get((slot['Date'], name), [])
            if slot['Start Time'] in emp_day_shifts: continue
            if len(emp_day_shifts) >= 2: continue
            
            is_current_am = "am" in slot['Start Time'].lower()
            has_am = any("am" in t.lower() for t in emp_day_shifts)
            has_pm = any("pm" in t.lower() for t in emp_day_shifts)
            if (is_current_am and has_pm) or (not is_current_am and has_am): continue

            assigned_name = name
            tracking_hours[name] += duration
            issue_flag = ""
            
            if (slot['Date'], name) not in daily_assignments:
                daily_assignments[(slot['Date'], name)] = []
            daily_assignments[(slot['Date'], name)].append(slot['Start Time'])
            break 

        slot['Assigned Employee'] = assigned_name
        slot['Issues'] = issue_flag

    # Diagnostic: flag employees who received 0 shifts, and separately, employees
    # who finished below their stated Min Hours requirement.
    unassigned_names = [name for name, count in run_shifts.items() if count == 0]
    unmet_min_names = [
        name for name, target in min_hours_map.items()
        if target > 0 and tracking_hours[name] < target
    ]
    if unassigned_names or unmet_min_names:
        print("\n" + "="*80)
        if unassigned_names:
            print(f"⚠️ DIAGNOSTIC ALERT: Could not find any valid shifts for: {', '.join(unassigned_names)}")
            print("   Reason: Their specific Default Rules, Blocked Dates, or Max Hours limit do not overlap")
            print("           with any of the available unassigned slots in this calendar period.")
        if unmet_min_names:
            print("⚠️ DIAGNOSTIC ALERT: The following employees did not reach their Min Hours requirement:")
            for name in unmet_min_names:
                print(f"   {name}: {tracking_hours[name]:.1f} / {min_hours_map[name]:.1f} hours")
            print("   Reason: Not enough compatible, unfilled slots remained for these employees given")
            print("           their availability rules and Max Hours ceiling.")
        print("="*80 + "\n")

    final_target_rows = []
    for slot in slots_to_process:
        emp = slot.get('Assigned Employee', '')
        if emp == "GAP": emp = ""
        issue = "GAP" if emp == "" else ""
        final_target_rows.append([slot['Date'], slot['Day of Week'], slot['Day Type'], slot['Start Time'], slot['End Time'], emp, issue])

    write_to_spreadsheet(target_year, target_month, final_target_rows, method="Auto-Assign (Force Overwrite)" if overwrite else "Auto-Assign (Smart Fill)")

CHAT_DELTA_SCHEMA = {
    "type": "object",
    "properties": {
        "deltas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM AM/PM"},
                    "original_employee": {"type": "string", "description": "Name currently in the slot, or '' / 'GAP' if unassigned"},
                    "new_employee": {"type": "string", "description": "New name to put in the slot, or '' / 'GAP' to clear it"}
                },
                "required": ["date", "start_time", "original_employee", "new_employee"],
                "additionalProperties": False
            }
        }
    },
    "required": ["deltas"],
    "additionalProperties": False
}

def plan_chat_modification(target_year, target_month, instruction):
    """Uses Claude NLP to find specific shift-change deltas WITHOUT writing anything to the sheet.
    Returns a list of resolved change previews for the UI to show the user before they confirm —
    each one flagged with whether it actually matched a real shift, so a mismatch (e.g. Claude
    guessing a time that doesn't exist) is visible before commit rather than silently skipped."""
    if not ai_client:
        raise Exception("Claude client is uninitialized. Verify your API key configuration.")

    print("\n[Chat AI] Fetching current schedule matrix from Google Sheets...")
    assignments_sheet = workbook.worksheet("Assignments")
    records = assignments_sheet.get_all_records()
    if not records:
        raise Exception("No active schedule slots available to modify.")
        
    df = pd.DataFrame(records)
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes(include=['object', 'string']):
        df[col] = df[col].astype(str).str.strip()
        
    df['Spreadsheet_Row'] = range(2, len(df) + 2)
    
    target_prefix = f"{target_year}-{target_month:02d}"
    target_month_df = df[df['Date'].astype(str).str.startswith(target_prefix)].copy()
    if target_month_df.empty:
        raise Exception(f"No shifts found matching {target_prefix}. Run initialization first.")
        
    visible_cols = ["Date", "Day of Week", "Start Time", "End Time", "Assigned Employee"]
    current_json = target_month_df[visible_cols].to_json(orient="records")
    
    prompt = f"""
    You are a precision scheduling modification engine. Analyze the instruction and output ONLY the specific shifts that need to change. Do not return unmodified shifts.

    CRITICAL CONSTRAINT: Multiple employees can be assigned to the exact same Date and Start Time. You must explicitly identify the name of the person currently in the slot ('original_employee') that is being modified or replaced, so the system targets the correct row.

    Context Timeline: Year {target_year}, Month {target_month}.
    User Command: "{instruction}"

    Current Month Shifts:
    {current_json}

    Return one delta per shift that actually needs to change. Return zero deltas if no changes are required.
    """

    max_retries = 3
    base_delay = 2
    deltas = None

    for attempt in range(max_retries):
        try:
            print(f"[Chat AI] Sending token-optimized delta request to Claude (Attempt {attempt+1}/{max_retries})...")
            response = ai_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                output_config={"format": {"type": "json_schema", "schema": CHAT_DELTA_SCHEMA}},
                messages=[{"role": "user", "content": prompt}]
            )
            text = next(b.text for b in response.content if b.type == "text")
            deltas = json.loads(text)["deltas"]
            break
        except NON_RETRYABLE_CLAUDE_ERRORS as e:
            # Fail fast — a 400/401/403/404 will not resolve itself on retry, and there's no
            # local fallback for a freeform NL instruction, so surface the real cause immediately
            # instead of a generic "quota/network" message that would be actively misleading here.
            raise Exception(f"Claude rejected the request ({type(e).__name__}): {e}. This will not succeed on retry — check your API key, request, or model configuration.")
        except Exception as e:
            print(f"[Chat AI] Warning: Request attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                sleep_time = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
            else:
                raise Exception(f"Fatal Quota/Network Error: Claude could not complete the conversational processing request after {max_retries} retries.")

    print(f"[Chat AI] Claude identified {len(deltas)} modification targets.")

    previews = []
    for delta in deltas:
        dt = delta.get("date")
        t_start = delta.get("start_time")
        orig_emp = delta.get("original_employee", "").strip()
        new_emp = delta.get("new_employee", "").strip()

        if orig_emp == "GAP": orig_emp = ""
        if new_emp == "GAP": new_emp = ""

        match = df[(df['Date'] == dt) & (df['Start Time'] == t_start) & (df['Assigned Employee'] == orig_emp)]
        previews.append({
            "date": dt,
            "day_of_week": match.iloc[0]['Day of Week'] if not match.empty else "",
            "start_time": t_start,
            "original_employee": orig_emp,
            "new_employee": new_emp,
            "matched": not match.empty,
        })
    return previews

def find_new_hire_candidate_shifts(target_year, target_month, employee_name, rules_map):
    """For an employee (typically a new hire who started after this month's schedule was
    already built), finds shifts currently assigned to OTHER active employees that could
    reasonably be handed to them instead — without creating a new violation in the process.

    Scoped to today-forward only (the same eligibility floor run_auto_assignment() uses — never
    suggest rewriting a day that's already happened), and only pulls from a 'donor' whose total
    hours would still meet their own Min Hours after losing the shift. Each candidate is also
    checked against the target employee's own availability rules, Max Hours, and same-day
    AM/PM-conflict rules, the same way run_auto_assignment() validates any other assignment.

    Returns a list of candidates sorted by date, for the UI to let the user pick which ones to
    actually reassign one at a time — this never writes anything itself."""
    employees_df, _, _ = load_tab_data()
    active_employees = employees_df[employees_df['Status'].str.lower() == 'active']

    target_rows = active_employees[active_employees['Employee Name'].apply(normalize_name) == normalize_name(employee_name)]
    if target_rows.empty:
        raise Exception(f"'{employee_name}' is not an active employee.")
    target_employee = target_rows.iloc[0]
    employee_name = target_employee['Employee Name']  # use canonical casing from Employees sheet
    target_rules = rules_map.get(employee_name, {})

    today_str = datetime.now().strftime("%Y-%m-%d")
    start_date = normalize_date_string(target_employee['Start Date'])
    eligible_from = max(start_date, today_str) if start_date else today_str

    try:
        target_max_hours = float(target_employee['Max Hours']) if str(target_employee['Max Hours']).strip() != "" else 999.0
    except ValueError:
        target_max_hours = 999.0

    assignments_sheet = workbook.worksheet("Assignments")
    records = assignments_sheet.get_all_records()
    df = pd.DataFrame(records)
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes(include=['object', 'string']):
        df[col] = df[col].astype(str).str.strip()

    target_prefix = f"{target_year}-{target_month:02d}"
    month_df = df[df['Date'].astype(str).str.startswith(target_prefix)]

    hours_by_employee = {}
    min_hours_by_employee = {}
    for _, emp in active_employees.iterrows():
        name = emp['Employee Name']
        emp_rows = month_df[month_df['Assigned Employee'].apply(normalize_name) == normalize_name(name)]
        hours_by_employee[name] = sum(calculate_hours(r['Start Time'], r['End Time']) for _, r in emp_rows.iterrows())
        try:
            min_hours_by_employee[name] = float(emp['Min Hours']) if str(emp['Min Hours']).strip() != "" else 0.0
        except ValueError:
            min_hours_by_employee[name] = 0.0

    target_hours = hours_by_employee.get(employee_name, 0.0)
    target_day_shifts = {}
    for _, row in month_df[month_df['Assigned Employee'].apply(normalize_name) == normalize_name(employee_name)].iterrows():
        target_day_shifts.setdefault(row['Date'], []).append(row['Start Time'])

    candidates = []
    for _, row in month_df.iterrows():
        donor = row['Assigned Employee']
        if not donor or normalize_name(donor) in ("gap", normalize_name(employee_name)):
            continue
        if row['Date'] < eligible_from:
            continue

        duration = calculate_hours(row['Start Time'], row['End Time'])

        # Donor must still meet their own Min Hours after giving up this shift — never fix one
        # person's shortfall by creating a new one for somebody else.
        if hours_by_employee.get(donor, 0.0) - duration < min_hours_by_employee.get(donor, 0.0):
            continue

        if target_hours + duration > target_max_hours:
            continue
        if not is_available(target_rules, {'Day of Week': row['Day of Week'], 'Date': row['Date'], 'Start Time': row['Start Time']}):
            continue

        day_shifts = target_day_shifts.get(row['Date'], [])
        if row['Start Time'] in day_shifts or len(day_shifts) >= 2:
            continue
        is_current_am = "am" in row['Start Time'].lower()
        has_am = any("am" in t.lower() for t in day_shifts)
        has_pm = any("pm" in t.lower() for t in day_shifts)
        if (is_current_am and has_pm) or (not is_current_am and has_am):
            continue

        candidates.append({
            "date": row['Date'], "day_of_week": row['Day of Week'], "start_time": row['Start Time'],
            "end_time": row['End Time'], "current_employee": donor,
            "donor_hours_after": round(hours_by_employee.get(donor, 0.0) - duration, 2),
            "donor_min_hours": min_hours_by_employee.get(donor, 0.0),
            "new_hire_hours_after": round(target_hours + duration, 2),
        })

    candidates.sort(key=lambda c: (c['date'], c['start_time']))
    return candidates

def apply_chat_deltas(deltas, instruction="", method="Natural Language Command"):
    """Applies a list of deltas (each a dict with date/start_time/original_employee/new_employee)
    to the live Assignments sheet. Re-fetches and re-matches against the sheet as it is right now,
    rather than trusting row numbers captured earlier, in case the sheet changed in between (e.g.
    a manual edit, or another Auto-Assign run) while the deltas were being reviewed.
    Logs each applied change to the History sheet under the given method, with 'instruction' as
    the Details column. Originally built for plan_chat_modification()'s NL-command deltas, but
    the matching/update/logging logic is generic enough to reuse for any other single-shift
    reassignment flow (e.g. onboarding a new hire) — just pass a different method label."""
    if not deltas:
        return

    assignments_sheet = workbook.worksheet("Assignments")
    records = assignments_sheet.get_all_records()
    if not records:
        raise Exception("No active schedule slots available to modify.")

    df = pd.DataFrame(records)
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes(include=['object', 'string']):
        df[col] = df[col].astype(str).str.strip()
    df['Spreadsheet_Row'] = range(2, len(df) + 2)

    cells_to_update = []
    history_entries = []
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    for delta in deltas:
        dt = delta.get("date")
        t_start = delta.get("start_time")
        orig_emp = delta.get("original_employee", "")
        new_emp = delta.get("new_employee", "")
        new_issue = "GAP" if new_emp == "" else ""

        match = df[(df['Date'] == dt) & (df['Start Time'] == t_start) & (df['Assigned Employee'] == orig_emp)]
        if not match.empty:
            row = match.iloc[0]
            row_num = int(row['Spreadsheet_Row'])
            cells_to_update.append(gspread.Cell(row=row_num, col=6, value=new_emp))
            cells_to_update.append(gspread.Cell(row=row_num, col=7, value=new_issue))

            df.loc[df['Spreadsheet_Row'] == row_num, 'Assigned Employee'] = new_emp
            history_entries.append({
                "timestamp": timestamp, "method": method, "date": dt,
                "day_of_week": row['Day of Week'], "start_time": t_start, "old_employee": orig_emp,
                "new_employee": new_emp, "edited_by": APP_EDITED_BY, "details": instruction,
            })
        else:
            print(f" -> Warning: Target mismatch for shift on {dt} at {t_start} with Original Employee '{orig_emp}'")

    if cells_to_update:
        print(f"[{method}] Executing targeted cell patch update for {len(cells_to_update)} elements...")
        assignments_sheet.update_cells(cells_to_update, value_input_option="USER_ENTERED")
        print(f"[{method}] Patch committed successfully.\n")
        # The cell update above already succeeded — a failure logging it to History must not be
        # reported as the calling action itself having failed.
        try:
            log_history_entries(history_entries)
        except Exception as e:
            print(f"\n[WARN] The change was applied, but logging it to the History sheet failed: {e}\n")

def write_to_spreadsheet(target_year, target_month, target_rows, method="Unknown", edited_by=APP_EDITED_BY, details=""):
    """Assembles history, validates data schema, and stages mutations transactionally to avoid data loss.
    Also diffs the target month's old vs new Assigned Employee values and logs every changed slot to the
    History sheet under the given method/edited_by/details, so every write through this function — which
    is every UI-driven write in the app — leaves an audit trail of exactly what changed."""
    try:
        assignments_sheet = workbook.worksheet("Assignments")
        existing_records = assignments_sheet.get_all_records()
        historical_rows = []
        old_target_rows = []

        canonical_headers = ["Date", "Day of Week", "Day Type", "Start Time", "End Time", "Assigned Employee", "Issues"]

        if existing_records:
            existing_df = pd.DataFrame(existing_records)
            existing_df.columns = existing_df.columns.str.strip()
            # diff_assignment_rows() below reads these rows by fixed position (row[0], row[3],
            # row[5], ...), so the columns must be in this exact order regardless of whatever
            # order they happen to be in the sheet right now (e.g. if someone manually dragged a
            # column directly in Google Sheets) — otherwise it would silently read the wrong
            # field into a history entry instead of raising an error.
            existing_df = existing_df[canonical_headers]
            target_mask = existing_df['Date'].astype(str).str.startswith(f"{target_year}-{target_month:02d}")
            historical_rows = existing_df[~target_mask].values.tolist()
            old_target_rows = existing_df[target_mask].values.tolist()

        raw_combined = historical_rows + target_rows
        
        sanitized_rows = []
        for row in raw_combined:
            sanitized_row = []
            for item in row:
                if pd.isna(item) or item is None:
                    sanitized_row.append("")
                else:
                    sanitized_row.append(str(item).strip())
            sanitized_rows.append(sanitized_row)
            
        sanitized_rows.sort(key=lambda x: x[0])
        payload = [canonical_headers] + sanitized_rows

        # Validate payload is fully serializable BEFORE clearing the sheet.
        # This is the last safety gate — if anything non-serializable slipped through
        # sanitization above, we bail here rather than wiping the sheet with bad data.
        json.dumps(payload)

        assignments_sheet.clear()
        assignments_sheet.update(range_name='A1', values=payload, value_input_option="USER_ENTERED")
        print(f"Sheet updated successfully. Total rows: {len(sanitized_rows)}")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Transaction aborted to prevent data loss. Error during payload serialization: {e}\n")
        raise e

    # History logging is a best-effort audit trail, not part of the write transaction above —
    # the actual schedule write already succeeded by this point. A logging failure here (e.g. a
    # transient Sheets API error appending to the History tab) must not be reported as the whole
    # operation having failed, or the user gets a false "this didn't work" when it actually did.
    try:
        def sanitize_row(row):
            return ["" if (pd.isna(item) or item is None) else str(item).strip() for item in row]

        changes = diff_assignment_rows(
            [sanitize_row(r) for r in old_target_rows],
            [sanitize_row(r) for r in target_rows],
        )
        timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        log_history_entries([
            {
                "timestamp": timestamp, "method": method, "date": date, "day_of_week": day_of_week,
                "start_time": start_time, "old_employee": old_emp, "new_employee": new_emp,
                "edited_by": edited_by, "details": details,
            }
            for date, day_of_week, start_time, old_emp, new_emp in changes
        ])
    except Exception as e:
        print(f"\n[WARN] The schedule write succeeded, but logging it to the History sheet failed: {e}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Child Watch Scheduler Engine Terminal Interface")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-blanks")
    init_parser.add_argument("--year", type=int, required=True)
    init_parser.add_argument("--month", type=int, required=True)

    assign_parser = subparsers.add_parser("auto-assign")
    assign_parser.add_argument("--year", type=int, required=True)
    assign_parser.add_argument("--month", type=int, required=True)
    assign_parser.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()
    if args.command == "init-blanks": run_initialize_blanks(args.year, args.month)
    elif args.command == "auto-assign": run_auto_assignment(args.year, args.month, overwrite=args.overwrite)