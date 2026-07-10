import streamlit as st
import scheduler
import pandas as pd
from datetime import datetime, timedelta
import calendar
import sys
import io
import contextlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import streamlit.components.v1 as components

st.set_page_config(page_title="Child Watch Scheduler", layout="wide")

@contextlib.contextmanager
def st_capture(st_element):
    """Intercepts stdout prints and streams them into a Streamlit element."""
    buffer = io.StringIO()
    old_write = sys.stdout.write

    def new_write(string):
        old_write(string)
        buffer.write(string)
        st_element.code(buffer.getvalue())

    sys.stdout.write = new_write
    try:
        yield
    finally:
        sys.stdout.write = old_write

@st.cache_data(ttl=600)
def cached_load_tab_data():
    """Loads structural background tabs with a 10 minute performance cache runtime window."""
    return scheduler.load_tab_data()

@st.cache_data(ttl=600)
def cached_normalize_rules(year, month, _active_employees):
    """Parses availability rules with the same 10 minute cache window, so re-opening the audit
    tab doesn't re-trigger Claude calls on every rerun."""
    return scheduler.batch_normalize_rules_with_claude(_active_employees, year, month)

@st.cache_data(ttl=60)
def cached_fetch_history():
    """Short-lived cache so the History tab and direct-edit badge don't hit Google Sheets
    on every page rerun (toggle clicks, button presses, etc.)."""
    return scheduler.fetch_history()

@st.cache_data(ttl=30)
def cached_get_app_state(key, default=""):
    """Short-lived cache for the App State sheet so notification markers don't require a
    live read on every rerun."""
    return scheduler.get_app_state(key, default)

@st.cache_data(ttl=600)
def cached_fetch_all_assignments():
    """Loads all months from Assignments. Explicitly cleared after every write, so a longer
    TTL is safe and reduces redundant API calls during passive reruns."""
    return scheduler.fetch_clean_dataframe("Assignments", fallback_columns=[
        "Date", "Day of Week", "Day Type", "Start Time", "End Time", "Assigned Employee", "Issues"
    ])

# Pre-warm all Google Sheets caches before the first UI element so the page renders
# all at once. The four reads are independent, so run them in parallel to cut cold-start
# time from ~4–5s (sequential) to ~1–2s.
with ThreadPoolExecutor(max_workers=4) as _executor:
    _f_asgn  = _executor.submit(cached_fetch_all_assignments)
    _f_tabs  = _executor.submit(cached_load_tab_data)
    _f_hist  = _executor.submit(cached_fetch_history)
    _f_state = _executor.submit(cached_get_app_state, "last_seen_direct_edit_timestamp", "")

_f_asgn.result()
_f_tabs.result()
history_df_preview        = _f_hist.result()
last_seen_direct_edit_ts  = _f_state.result()


unseen_direct_edits, latest_direct_edit_ts = scheduler.count_unseen_direct_edits(
    history_df_preview, last_seen_direct_edit_ts
)
history_tab_label = "📜 History"
if unseen_direct_edits > 0:
    history_tab_label += f" 🔔 {unseen_direct_edits}"

st.title("🗓️ Child Watch Scheduler Dashboard")
st.write("Manage your Google Sheet schedule matrix and run the optimization engine locally.")
st.caption("🟢 Connected to: **Child Watch Schedule**")

# 1. Date Selection Toolbar (kept on the main page — the sidebar is reserved exclusively
# for the Natural Language Command Assistant)
month_names = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

# Session State Initialization
if "schedule_df" not in st.session_state:
    st.session_state["schedule_df"] = None
if "current_view_key" not in st.session_state:
    st.session_state["current_view_key"] = ""
if "last_error" not in st.session_state:
    st.session_state["last_error"] = None

# Undo Snapshot State Initialization
if "history_df" not in st.session_state:
    st.session_state["history_df"] = None
if "last_action" not in st.session_state:
    st.session_state["last_action"] = ""

# Pending Chat Change Preview State Initialization
if "pending_chat_changes" not in st.session_state:
    st.session_state["pending_chat_changes"] = None

# New Hire Candidate Shifts State Initialization
if "new_hire_candidates" not in st.session_state:
    st.session_state["new_hire_candidates"] = None

# Employee List Change Detection State Initialization
if "employee_data_hash" not in st.session_state:
    st.session_state["employee_data_hash"] = None
if "employee_last_synced" not in st.session_state:
    st.session_state["employee_last_synced"] = None
if "employee_external_change" not in st.session_state:
    st.session_state["employee_external_change"] = False
if "employee_snapshot_csv" not in st.session_state:
    st.session_state["employee_snapshot_csv"] = None
if "confirm_initialize" not in st.session_state:
    st.session_state["confirm_initialize"] = False
if "_prev_ctrl_year" not in st.session_state:
    st.session_state["_prev_ctrl_year"] = None
if "_prev_ctrl_month" not in st.session_state:
    st.session_state["_prev_ctrl_month"] = None
if "employee_undo_snapshot" not in st.session_state:
    st.session_state["employee_undo_snapshot"] = None
if "holiday_undo_snapshot" not in st.session_state:
    st.session_state["holiday_undo_snapshot"] = None
if "template_undo_snapshot" not in st.session_state:
    st.session_state["template_undo_snapshot"] = None
if "pending_history_delete" not in st.session_state:
    st.session_state["pending_history_delete"] = None

fallback_cols = ["Date", "Day of Week", "Day Type", "Start Time", "End Time", "Assigned Employee", "Issues"]

# Helper: Snapshot current state before mutation
def capture_undo_snapshot(action_name):
    if st.session_state["schedule_df"] is not None:
        st.session_state["history_df"] = st.session_state["schedule_df"].copy()
    else:
        st.session_state["history_df"] = pd.DataFrame(columns=fallback_cols)
    st.session_state["last_action"] = action_name

def render_inline_undo(action_names):
    """Renders a compact undo prompt right next to the action that produced the current snapshot,
    instead of a single global banner at the top of the page."""
    if st.session_state["history_df"] is None:
        return
    last_action = st.session_state["last_action"]
    matched_names = [action_names] if isinstance(action_names, str) else action_names
    if last_action not in matched_names:
        return

    st.info(f"**State Changed:** You recently executed *'{last_action}'*.")
    if st.button("⏪ Undo Last Action", type="secondary", use_container_width=True, key=f"undo_{last_action}"):
        with st.spinner("Restoring Google Sheet to previous state snapshot..."):
            try:
                restore_df = st.session_state["history_df"].fillna("").copy()
                restore_df['Date'] = restore_df['Date'].astype(str)
                rows_to_write = restore_df[fallback_cols].values.tolist()

                scheduler.write_to_spreadsheet(selected_year, selected_month, rows_to_write, method="Undo", details=f"Reverted '{last_action}'")

                st.success("Successfully rolled back to the previous state!")
                st.session_state["schedule_df"] = None
                st.session_state["history_df"] = None
                st.session_state["last_action"] = ""
                cached_fetch_all_assignments.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Failed to restore state: {e}")

# 2. Main Dashboard Interface Layout

# Fatal Error Alerts
if st.session_state["last_error"]:
    st.error(f"🛑 **Fatal Execution Interruption Catch:** {st.session_state['last_error']}")
    if st.button("Dismiss Error Notification"):
        st.session_state["last_error"] = None
        st.rerun()

# Streamlit sizes st.container(border=True) to its own content, so the Step 1/2 boxes below
# would otherwise be different heights (Step 2 has an extra checkbox). Flexbox stretch alone
# wasn't reliably equalizing them, so just hard-code a fixed minimum height for both instead.
# This generic selector also reaches the Onboard New Hire container further down — that's fine,
# since min-height is only a floor; it just stops that box from ever looking too short when its
# candidate-shift list is empty, and grows normally once results appear.
st.markdown("""
    <style>
        div[data-testid="stVerticalBlockBorderWrapper"] { min-height: 230px; }
        /* Employee Validation Audit expander labels are padded to fixed character widths so
           their "|" separators line up — that only works in a monospace font, since the
           default theme font is proportional (equal character count != equal pixel width).
           Scope this to the label's text container specifically (stMarkdownContainer), NOT
           every descendant of <summary> — the expand/collapse chevron is a sibling rendered
           via an icon-ligature font, and forcing it to monospace breaks the glyph mapping and
           shows the raw ligature name ("arrow...") as literal text instead of the icon. */
        div[data-testid="stTabs"] div[data-testid="stExpander"] summary [data-testid="stMarkdownContainer"],
        div[data-testid="stTabs"] div[data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] * {
            font-family: "SF Mono", "Monaco", "Consolas", "Liberation Mono", "Courier New", monospace !important;
        }
        /* Hides the built-in "Press Enter to submit form" hint that appears under a text input
           inside a form once it has unsaved changes — on the Natural Language Command Assistant
           field it overlaps the typed text instead of sitting cleanly below it. */
        div[data-testid="InputInstructions"] {
            display: none;
        }
        /* Hide Streamlit's connection-lost status banner so shutting down the app shows
           the current page frozen cleanly rather than an alarming red error widget. */
        [data-testid="stStatusWidget"] {
            display: none !important;
        }
    </style>
""", unsafe_allow_html=True)

with st.container(border=True):
    ctrl_year, ctrl_month, ctrl_refresh = st.columns([3, 3, 2])
    with ctrl_year:
        selected_year = st.number_input("Target Year", min_value=2025, max_value=datetime.today().year + 10, value=datetime.today().year)
    with ctrl_month:
        selected_month_name = st.selectbox("Target Month", month_names, index=datetime.today().month - 1)
        selected_month = month_names.index(selected_month_name) + 1
    with ctrl_refresh:
        st.write("")
        force_refresh_clicked = st.button("🔄 Force Refresh From Cloud", use_container_width=True)

    # Reset the Step 1 confirmation state when the user switches to a different month/year
    if (st.session_state["_prev_ctrl_year"] != selected_year or
            st.session_state["_prev_ctrl_month"] != selected_month):
        st.session_state["confirm_initialize"] = False
    st.session_state["_prev_ctrl_year"] = selected_year
    st.session_state["_prev_ctrl_month"] = selected_month

    # Find the most recent generation event for the currently-selected month
    _gen_methods = {"Auto-Assign (Smart Fill)", "Auto-Assign (Force Overwrite)", "Initialize Blanks"}
    _target_pfx = f"{selected_year}-{selected_month:02d}"
    _last_gen_row = None
    if not history_df_preview.empty and "Method" in history_df_preview.columns and "Date" in history_df_preview.columns:
        _gen_hist = history_df_preview[
            history_df_preview["Method"].isin(_gen_methods) &
            history_df_preview["Date"].astype(str).str.startswith(_target_pfx)
        ]
        if not _gen_hist.empty:
            _last_gen_row = _gen_hist.iloc[0]

    st.divider()

    col1, col2 = st.columns(2)

    with col1:
        with st.container(border=True):
            st.markdown("#### 🧱 Step 1 — Initialization")
            _init_today = datetime.today()
            _is_past_month = (selected_year, selected_month) < (_init_today.year, _init_today.month)
            _is_current_month = (selected_year, selected_month) == (_init_today.year, _init_today.month)
            _preserve_date = _init_today.strftime('%Y-%m-%d') if _is_current_month else None

            if _is_past_month:
                st.caption("Generate empty template shifts for the month. This clears out previous iterations for this month.")
                st.button("Initialize Month", type="secondary", use_container_width=True, disabled=True)
                st.caption("⛔ Past months cannot be re-initialized — doing so would permanently wipe assignment history.")
            else:
                if _is_current_month:
                    st.caption(
                        f"Generate empty template shifts from today onward. "
                        f"Assignments before {selected_month_name} {_init_today.day} will be preserved."
                    )
                else:
                    st.caption("Generate empty template shifts for the month. This clears out previous iterations for this month.")

                if not st.session_state["confirm_initialize"]:
                    if st.button("Initialize Month", type="secondary", use_container_width=True):
                        st.session_state["confirm_initialize"] = True
                        st.rerun()
                else:
                    if _is_current_month:
                        st.warning(
                            f"⚠️ This will reset all unassigned slots from today onward for "
                            f"{selected_month_name} {selected_year}. "
                            f"Existing assignments before today will be preserved. Continue?"
                        )
                    else:
                        st.warning(f"⚠️ This will **clear all existing shifts** for {selected_month_name} {selected_year}. This cannot be undone automatically. Continue?")
                    confirm_init_col, cancel_init_col = st.columns(2)
                    if confirm_init_col.button("✅ Yes, Initialize", type="primary", use_container_width=True):
                        st.session_state["confirm_initialize"] = False
                        capture_undo_snapshot("Initialize Month")
                        with st.spinner("Connecting to Google Sheets and building matrix..."):
                            try:
                                scheduler.run_initialize_blanks(selected_year, selected_month, preserve_before_date=_preserve_date)
                                st.success(f"Successfully initialized blank slots for {selected_month_name} {selected_year}!")
                                st.session_state["schedule_df"] = None
                                st.session_state["last_error"] = None
                                cached_fetch_all_assignments.clear()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Initialization failed: {e}")
                    if cancel_init_col.button("Cancel", type="secondary", use_container_width=True):
                        st.session_state["confirm_initialize"] = False
                        st.rerun()
                render_inline_undo("Initialize Month")

    with col2:
        with st.container(border=True):
            st.markdown("#### 🤖 Step 2 — Roster Allocation")
            st.caption("Run the workload-balanced allocation rules over unassigned slots.")

            overwrite_mode = st.checkbox("Force Overwrite", help="Wipe all manual edits in the sheet and reassign from scratch.")

            button_label = "Force Overwrite & Reassign" if overwrite_mode else "Auto-Assign Roster (Smart Fill)"
            button_type = "primary" if overwrite_mode else "secondary"

            if st.button(button_label, type=button_type, use_container_width=True):
                capture_undo_snapshot(button_label)
                with st.status("📡 Contacting Claude and building the roster — this can take up to a minute...", expanded=True) as status:
                    log_placeholder = st.empty()
                    try:
                        with st_capture(log_placeholder):
                            scheduler.run_auto_assignment(selected_year, selected_month, overwrite=overwrite_mode)
                        status.update(label="✅ Roster generation complete!", state="complete", expanded=False)
                        st.success(f"Roster generation complete for {selected_month_name} {selected_year}!")
                        st.session_state["schedule_df"] = None
                        st.session_state["last_error"] = None
                        cached_fetch_all_assignments.clear()
                        st.rerun()
                    except Exception as e:
                        status.update(label="🛑 Assignment execution failed", state="error", expanded=True)
                        st.session_state["last_error"] = str(e)
                        st.error(f"Assignment execution failed: {e}")
            render_inline_undo(["Force Overwrite & Reassign", "Auto-Assign Roster (Smart Fill)"])
            if _last_gen_row is not None:
                st.caption(f"Last run: **{_last_gen_row['Method']}** · {_last_gen_row['Timestamp']}")
            else:
                st.caption("Not yet generated for this month.")

active_key = f"{selected_year}-{selected_month}"

# Manual Sync (Clears data caches globally)
if force_refresh_clicked:
    st.session_state["schedule_df"] = None
    st.session_state["last_error"] = None
    st.session_state["history_df"] = None
    st.session_state["employee_data_hash"] = None
    st.session_state["employee_last_synced"] = None
    st.session_state["employee_external_change"] = False
    st.cache_data.clear()
    st.rerun()

# 3. Onboard New Hire — for an employee who started after this month's schedule was already
# built. Neither Auto-Assign mode fits this: Smart Fill only ever touches empty gaps (useless
# if the month is already fully staffed), and Force Overwrite would erase the historical record
# of who actually worked the days before today. This instead surfaces individual shifts
# currently held by employees who've already met their own Min Hours, and lets the user hand
# them over one at a time, reviewing each before it's written.
st.divider()
with st.expander("🆕 Onboard New Hire — Reassign Existing Shifts", expanded=False):
    st.caption(
        "For an employee who started after this month's schedule was already built. Surfaces shifts "
        "(from today onward) currently held by employees who've already met their Min Hours, so you can "
        "hand individual ones to the new hire without rebuilding — or erasing the history of — the rest of the month."
    )

    onboard_employees_df, _, _ = cached_load_tab_data()
    onboard_active_employees = onboard_employees_df[onboard_employees_df['Status'].str.lower() == 'active']
    hire_options = sorted(onboard_active_employees['Employee Name'].tolist())

    if not hire_options:
        st.info("No active employees configured.")
    else:
        select_col, find_col = st.columns([3, 1])
        with select_col:
            new_hire_name = st.selectbox("Employee", hire_options, key="new_hire_select", label_visibility="collapsed")
        with find_col:
            find_clicked = st.button("🔍 Find Candidate Shifts", use_container_width=True)

        # Same format as the Employee Validation Audit tab, so the preferences driving which
        # shifts show up as candidates below are visible right where you're deciding among them.
        new_hire_row = onboard_active_employees[onboard_active_employees['Employee Name'] == new_hire_name].iloc[0]
        pref_col_rules, pref_col_blocked, pref_col_start = st.columns(3)
        with pref_col_rules:
            st.markdown(f"**Baseline Availability Rules:**\n`{new_hire_row['Default Rules'] if new_hire_row['Default Rules'] else 'None Listed'}`")
        with pref_col_blocked:
            st.markdown(f"**Blocked Dates:**\n`{new_hire_row['Blocked Dates'] if new_hire_row['Blocked Dates'] else 'None Listed'}`")
        with pref_col_start:
            st.markdown(f"**Start Date:**\n`{new_hire_row['Start Date'] if new_hire_row['Start Date'] else 'Already eligible'}`")

        if find_clicked:
            with st.spinner(f"Finding shifts that could be reassigned to {new_hire_name}..."):
                try:
                    onboard_rules_map = cached_normalize_rules(selected_year, selected_month, onboard_active_employees)
                    candidates = scheduler.find_new_hire_candidate_shifts(selected_year, selected_month, new_hire_name, onboard_rules_map, _assignments_df=cached_fetch_all_assignments())
                    st.session_state["new_hire_candidates"] = {"name": new_hire_name, "shifts": candidates}
                except Exception as e:
                    st.session_state["new_hire_candidates"] = None
                    st.error(f"Failed to find candidate shifts: {e}")

        pending_hire = st.session_state["new_hire_candidates"]
        if pending_hire and pending_hire["name"] == new_hire_name:
            shifts = pending_hire["shifts"]
            if not shifts:
                st.info(f"No eligible shifts found to reassign to {new_hire_name} for the rest of this month.")
            else:
                st.write(f"**{len(shifts)} candidate shift(s)** for {new_hire_name} — each is currently held by someone who would still meet their own Min Hours if it were reassigned:")

                # Group by week then by day so 100+ shifts don't become one giant scroll.
                from collections import defaultdict
                _shifts_by_week = defaultdict(lambda: defaultdict(list))
                for _i, _s in enumerate(shifts):
                    _d = datetime.strptime(_s['date'], '%Y-%m-%d')
                    _days_to_sun = (_d.weekday() + 1) % 7
                    _week_start = (_d - timedelta(days=_days_to_sun)).strftime('%Y-%m-%d')
                    _shifts_by_week[_week_start][_s['date']].append((_i, _s))

                for _week_start in sorted(_shifts_by_week.keys()):
                    _days = _shifts_by_week[_week_start]
                    _week_total = sum(len(v) for v in _days.values())
                    _ws_dt = datetime.strptime(_week_start, '%Y-%m-%d')
                    _we_dt = _ws_dt + timedelta(days=6)
                    _week_label = f"{_ws_dt.strftime('%b %-d')} – {_we_dt.strftime('%b %-d')}  ({_week_total} shift{'s' if _week_total != 1 else ''})"
                    with st.expander(_week_label, expanded=False):
                        for _day_str in sorted(_days.keys()):
                            _day_dt = datetime.strptime(_day_str, '%Y-%m-%d')
                            st.markdown(f"**{_day_dt.strftime('%A, %b %-d')}**")
                            for i, shift in _days[_day_str]:
                                shift_col, assign_col = st.columns([3, 1])
                                with shift_col:
                                    st.write(
                                        f"{shift['start_time']}–{shift['end_time']} "
                                        f"— currently **{shift['current_employee']}** "
                                        f"(would have {shift['donor_hours_after']:.1f} hrs left, min {shift['donor_min_hours']:.1f})"
                                    )
                                with assign_col:
                                    if st.button("➕ Assign", key=f"assign_hire_{i}", use_container_width=True):
                                        capture_undo_snapshot("New Hire Shift Assignment")
                                        with st.spinner("Reassigning shift..."):
                                            try:
                                                scheduler.apply_chat_deltas(
                                                    [{
                                                        "date": shift["date"], "start_time": shift["start_time"],
                                                        "original_employee": shift["current_employee"], "new_employee": new_hire_name,
                                                    }],
                                                    instruction=f"Onboarding new hire: {new_hire_name}",
                                                    method="New Hire Shift Assignment",
                                                )
                                                st.session_state["schedule_df"] = None
                                                cached_fetch_all_assignments.clear()
                                                refreshed_rules_map = cached_normalize_rules(selected_year, selected_month, onboard_active_employees)
                                                st.session_state["new_hire_candidates"] = {
                                                    "name": new_hire_name,
                                                    "shifts": scheduler.find_new_hire_candidate_shifts(selected_year, selected_month, new_hire_name, refreshed_rules_map, _assignments_df=cached_fetch_all_assignments()),
                                                }
                                                st.success(f"Assigned the {shift['date']} {shift['start_time']} shift to {new_hire_name}!")
                                                st.rerun()
                                            except Exception as e:
                                                st.error(f"Failed to reassign shift: {e}")
                if st.button("✖ Clear candidate list"):
                    st.session_state["new_hire_candidates"] = None
                    st.rerun()
    render_inline_undo("New Hire Shift Assignment")

# 4. Chat Assistant Form (lives in the sidebar so it's available alongside the date
# picker without taking up width in the main content area)
st.sidebar.divider()
st.sidebar.subheader("💬 Natural Language Command Assistant")
st.sidebar.write("Type modifications conversationally (e.g., *'Swap Alice and Bob's shifts on July 1 and 2'*).")

with st.sidebar.form("chat_assistant_form", clear_on_submit=True):
    chat_instruction = st.text_area(
        "Enter scheduling instructions:",
        placeholder="Alice is out sick on July 10, remove her from all shifts...",
        height=120
    )
    submit_chat = st.form_submit_button("✨ Apply Conversational Command", type="secondary", use_container_width=True)

if submit_chat:
    if chat_instruction.strip() == "":
        st.sidebar.warning("Please provide a valid text instruction command first.")
    else:
        with st.sidebar.status("📡 Sending your instruction to Claude...", expanded=True) as status:
            log_placeholder = st.empty()
            try:
                with st_capture(log_placeholder):
                    previews = scheduler.plan_chat_modification(selected_year, selected_month, chat_instruction, _assignments_df=cached_fetch_all_assignments())
                st.session_state["last_error"] = None
                if previews:
                    status.update(label=f"✅ Proposed {len(previews)} change(s)", state="complete", expanded=False)
                    st.session_state["pending_chat_changes"] = {"changes": previews, "instruction": chat_instruction}
                    # Rerun immediately so the preview/confirm UI below renders as part of a
                    # fresh script pass, rather than appearing mid-run underneath the status
                    # widget (which can otherwise flash or look like duplicated buttons).
                    st.rerun()
                else:
                    status.update(label="✅ No changes needed", state="complete", expanded=False)
                    st.sidebar.info("Claude found no shifts that need to change for that instruction.")
            except Exception as e:
                status.update(label="🛑 Conversational command failed", state="error", expanded=True)
                st.session_state["last_error"] = str(e)
                st.sidebar.error(f"Failed to execute conversational modification: {e}")

# Preview of proposed changes — nothing is written to the sheet until the user explicitly
# confirms, since Claude's interpretation of an ambiguous instruction can be wrong and this
# is the one place in the app where an LLM call can directly rewrite schedule data.
if st.session_state["pending_chat_changes"]:
    with st.sidebar:
        st.info("📋 Review the proposed changes below, then confirm or discard.")
        any_unmatched = False
        for change in st.session_state["pending_chat_changes"]["changes"]:
            from_label = change["original_employee"] or "GAP"
            to_label = change["new_employee"] or "GAP"
            day_label = f" ({change['day_of_week']})" if change["day_of_week"] else ""
            if change["matched"]:
                st.markdown(f"- {change['date']}{day_label} @ {change['start_time']}: **{from_label} → {to_label}**")
            else:
                any_unmatched = True
                st.markdown(f"- ⚠️ {change['date']}{day_label} @ {change['start_time']}: **{from_label} → {to_label}**  *(no matching shift found — will be skipped)*")
        if any_unmatched:
            st.caption("Shifts marked ⚠️ didn't match anything currently on the sheet and will be silently skipped if you confirm.")

        confirm_col, discard_col = st.columns(2)
        if confirm_col.button("✅ Confirm", type="secondary", use_container_width=True, key="confirm_chat_changes"):
            capture_undo_snapshot("Conversational Chat Command")
            with st.spinner("Writing changes to Google Sheets..."):
                try:
                    pending = st.session_state["pending_chat_changes"]
                    scheduler.apply_chat_deltas(pending["changes"], instruction=pending["instruction"])
                    st.session_state["pending_chat_changes"] = None
                    st.session_state["schedule_df"] = None
                    st.session_state["last_error"] = None
                    cached_fetch_all_assignments.clear()
                    st.success("Conversational changes committed and updated in Google Sheets!")
                    st.rerun()
                except Exception as e:
                    st.session_state["last_error"] = str(e)
                    st.error(f"Failed to apply changes: {e}")
        if discard_col.button("❌ Discard", type="secondary", use_container_width=True, key="discard_chat_changes"):
            st.session_state["pending_chat_changes"] = None
            st.rerun()

st.sidebar.divider()
with st.sidebar:
    render_inline_undo("Conversational Chat Command")

@st.dialog("Shut Down App")
def _confirm_shutdown():
    st.warning("This will stop the app. Are you sure?")
    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button("Yes, shut down", type="primary", use_container_width=True):
            st.session_state["shutting_down"] = True
            st.rerun()
    with col_no:
        if st.button("Cancel", use_container_width=True):
            st.rerun()

st.sidebar.divider()
with st.sidebar:
    st.caption("⚙️ App Controls")
    if st.session_state.get("shutting_down"):
        st.info("Shutting down...")
        # Navigate the browser tab away so the user sees a clean close.
        # height=1 (not 0) ensures the iframe is actually mounted and the
        # script executes. The stStatusWidget CSS above hides the connection
        # banner as a belt-and-suspenders fallback.
        components.html("""
            <script>
                setTimeout(function() {
                    try { window.top.close(); } catch(e) {}
                    try { window.top.location.href = 'about:blank'; } catch(e) {}
                }, 800);
            </script>
        """, height=1)
        if not st.session_state.get("shutdown_thread_started"):
            st.session_state["shutdown_thread_started"] = True
            threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()
    else:
        if st.button("🔴 Shut Down App", use_container_width=True):
            _confirm_shutdown()

st.divider()

# 5. Automated Data Sync Engine
if st.session_state["schedule_df"] is None or st.session_state["current_view_key"] != active_key:
    try:
        df = cached_fetch_all_assignments()
        if not df.empty:
            target_prefix = f"{selected_year}-{selected_month:02d}"
            filtered_df = df[df['Date'].astype(str).str.startswith(target_prefix)].copy()
            st.session_state["schedule_df"] = filtered_df
            st.session_state["current_view_key"] = active_key
        else:
            st.session_state["schedule_df"] = pd.DataFrame(columns=fallback_cols)
    except Exception as e:
        st.error(f"Failed to fetch live view updates: {e}")

working_df = st.session_state["schedule_df"]

# --- HTML Calendar Grid Generation Engine ---
def render_html_calendar_grid(year, month, data_frame, holidays_df=None):
    shifts_by_date = {}
    for _, row in data_frame.iterrows():
        d_str = str(row['Date'])
        if d_str not in shifts_by_date:
            shifts_by_date[d_str] = []
        shifts_by_date[d_str].append(row)

    holiday_lookup = {}
    if holidays_df is not None and not holidays_df.empty:
        for _, h in holidays_df.iterrows():
            h_date = scheduler.normalize_date_string(str(h.get('Date', '')))
            if h_date:
                holiday_lookup[h_date] = {
                    "name": str(h.get('Name', '')),
                    "override": str(h.get('Override Type', '')).strip().lower()
                }

    cal = calendar.Calendar(firstweekday=6)
    month_weeks = cal.monthdayscalendar(year, month)

    html = """
    <style>
        .cal-table { width: 100%; border-collapse: collapse; table-layout: fixed; margin-top: 15px; font-family: inherit; }
        .cal-th { background-color: #f0f2f6; text-align: center; padding: 8px; border: 1px solid #dcdcdc; font-weight: bold; color: #31333F; }
        .cal-td { vertical-align: top; height: 115px; border: 1px solid #dcdcdc; padding: 6px; position: relative; background-color: #ffffff; }
        .cal-td-holiday { background-color: #fffbea; }
        .cal-td-closed { background-color: #f5f5f5; }
        .cal-day-num { font-weight: bold; margin-bottom: 4px; color: #555555; font-size: 14px; }
        .cal-holiday-label { font-size: 10px; color: #b45309; font-style: italic; margin-bottom: 4px; }
        .cal-closed-label { font-size: 10px; color: #9ca3af; font-style: italic; margin-bottom: 4px; }
        .cal-empty { background-color: #fafafa; }
        .cal-shift-box { font-size: 11px; padding: 3px 6px; margin-bottom: 4px; border-radius: 4px; background-color: #e8f0fe; border-left: 4px solid #1a73e8; color: #185abc; font-weight: 500; }
        .cal-shift-gap { background-color: #fce8e6; border-left: 4px solid #d93025; color: #d93025; }
        .cal-shift-gap-past { background-color: #f3f4f6; border-left: 4px solid #9ca3af; color: #9ca3af; }
        @media (prefers-color-scheme: dark) {
            .cal-th { background-color: #262730 !important; border-color: rgba(255,255,255,0.12) !important; color: #fafafa !important; }
            .cal-td { background-color: #0e1117 !important; border-color: rgba(255,255,255,0.12) !important; }
            .cal-td-holiday { background-color: #2a2200 !important; }
            .cal-td-closed { background-color: #1a1a1a !important; }
            .cal-day-num { color: #a0a0a0 !important; }
            .cal-empty { background-color: #080b0f !important; border-color: rgba(255,255,255,0.06) !important; }
            .cal-shift-box { background-color: #1a2e4a !important; border-color: #4d8fcc !important; color: #7cb9f5 !important; }
            .cal-shift-gap { background-color: #3d1515 !important; border-color: #cc4444 !important; color: #ff7070 !important; }
            .cal-shift-gap-past { background-color: #1f1f1f !important; border-color: #555 !important; color: #666 !important; }
            .cal-holiday-label { color: #d4a017 !important; }
            .cal-closed-label { color: #555 !important; }
        }
    </style>
    <table class="cal-table">
        <tr>
            <th class="cal-th">Sun</th><th class="cal-th">Mon</th><th class="cal-th">Tue</th>
            <th class="cal-th">Wed</th><th class="cal-th">Thu</th><th class="cal-th">Fri</th>
            <th class="cal-th">Sat</th>
        </tr>
    """
    for week in month_weeks:
        html += "<tr>"
        for day in week:
            if day == 0:
                html += '<td class="cal-td cal-empty"></td>'
            else:
                date_str = f"{year}-{month:02d}-{day:02d}"
                h_info = holiday_lookup.get(date_str)
                is_closed = h_info and h_info["override"] == "closed"

                if h_info:
                    td_class = "cal-td cal-td-closed" if is_closed else "cal-td cal-td-holiday"
                else:
                    td_class = "cal-td"
                html += f'<td class="{td_class}">'
                html += f'<div class="cal-day-num">{day}</div>'

                if h_info:
                    if is_closed:
                        html += f'<div class="cal-closed-label">🚫 {h_info["name"]}</div>'
                    else:
                        html += f'<div class="cal-holiday-label">🏖 {h_info["name"]}</div>'

                if date_str in shifts_by_date:
                    today_str = datetime.today().strftime('%Y-%m-%d')
                    for shift in shifts_by_date[date_str]:
                        emp = shift['Assigned Employee']
                        t_start = shift['Start Time']
                        if emp == "" or emp == "GAP":
                            if date_str >= today_str:
                                html += f'<div class="cal-shift-box cal-shift-gap">🚨 {t_start}: GAP</div>'
                            else:
                                html += f'<div class="cal-shift-box cal-shift-gap-past">⬜ {t_start}: GAP</div>'
                        else:
                            html += f'<div class="cal-shift-box">🕒 {t_start}: {emp}</div>'
                html += '</td>'
        html += "</tr>"
    html += "</table>"
    return html

def format_hours_target(value):
    """Renders a Min/Max Hours cell for display, falling back to an em dash for any
    flavor of 'not set' (empty string, None, or NaN), and otherwise always showing exactly one
    decimal place so every value in the column has the same 'XX.X' shape to align against."""
    if pd.isna(value) or str(value).strip() == "":
        return "—"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value).strip()

def round_to_half(hours):
    """Rounds a precise hour total (often a quarter-hour value from 45-minute shifts, e.g. 3.75)
    to the nearest 0.5 for display, so the UI only ever shows a 'X.0' or 'X.5' figure."""
    return round(hours * 2) / 2

def is_gap_mask(data_frame):
    return data_frame['Assigned Employee'].apply(lambda x: str(x).strip() in ["", "GAP"])

def filter_gaps_only(data_frame):
    """Returns all unfilled shifts — past gaps are already rendered grey, future gaps red."""
    return data_frame[is_gap_mask(data_frame)]

def describe_conflict(name, parsed_rules, row, start_date=""):
    """Explains why a specific shift conflicts with an employee's parsed availability rules.
    Checks reasons in the same order as scheduler.is_available() so the first matching reason
    here is guaranteed to be the actual reason is_available() returned False. The Start Date
    check runs first since it's a more fundamental issue ('not hired yet') than a preference
    rule, and isn't part of parsed_rules at all — it's a separate employee field."""
    day_of_week = row['Day of Week']
    date_str = row['Date']
    start_time = row['Start Time'].lower()

    if start_date and date_str < start_date:
        return f"{name}'s start date is {start_date}, but is assigned to a shift on {date_str} — before they were hired."

    if parsed_rules.get("use_local_fallback", False):
        return f"{name}'s rules text ('{parsed_rules.get('raw_rules', '')}') flags this shift as unavailable (degraded local parsing — verify manually)."

    if date_str in parsed_rules.get("vacation_dates", []):
        return f"{name} has {date_str} marked as a blocked date, but is assigned to a shift that day."

    forbidden = [d.lower() for d in parsed_rules.get("forbidden_days", [])]
    if day_of_week.lower() in forbidden:
        return f"{name} cannot work {day_of_week}s, but is assigned to a {day_of_week} shift."

    allowed = [d.lower() for d in parsed_rules.get("allowed_days", [])]
    if allowed and day_of_week.lower() not in allowed:
        allowed_display = " or ".join(parsed_rules.get("allowed_days", []))
        return f"{name} can only work {allowed_display}, but is assigned to a {day_of_week} shift."

    restriction = parsed_rules.get("time_restriction", "any")
    if restriction == "morning" and "pm" in start_time:
        return f"{name} is morning-only, but is assigned to a {row['Start Time']} shift."
    if restriction == "afternoon" and "am" in start_time:
        return f"{name} is afternoon-only, but is assigned to a {row['Start Time']} shift."

    return f"{name} is unavailable for this shift per their stated rules."

def compute_employee_conflicts(data_frame, active_employees, rules_map):
    """Returns {employee_name: {'shifts': df_with_Conflict_column, 'hours_issue': str|None,
    'total_hours': float}} and the total conflict count (per-shift rule violations + min/max hour
    cap violations) across all
    employees, so the rollup can be shown before any expander is opened. Hour cap violations matter
    here because manual edits (table editor, day editor, or direct sheet edits) don't go through
    run_auto_assignment()'s Max/Min Hours checks at all, so they can silently push someone over/under.
    """
    conflict_data = {}
    total_conflicts = 0
    for _, employee in active_employees.iterrows():
        name = employee['Employee Name']
        emp_shifts = data_frame[data_frame['Assigned Employee'] == name].copy()
        parsed_rules = rules_map.get(name, {})

        start_date = scheduler.normalize_date_string(employee['Start Date'])

        if not emp_shifts.empty:
            emp_shifts['Conflict'] = emp_shifts.apply(
                lambda row: (start_date and row['Date'] < start_date) or not scheduler.is_available(parsed_rules, {
                    'Day of Week': row['Day of Week'],
                    'Date': row['Date'],
                    'Start Time': row['Start Time']
                }),
                axis=1
            )
            total_conflicts += int(emp_shifts['Conflict'].sum())

        total_hours = sum(scheduler.calculate_hours(s['Start Time'], s['End Time']) for _, s in emp_shifts.iterrows())

        max_hours = None
        try:
            if str(employee['Max Hours']).strip() != "":
                max_hours = float(employee['Max Hours'])
        except ValueError:
            max_hours = None
        min_hours = None
        try:
            if str(employee['Min Hours']).strip() != "":
                min_hours = float(employee['Min Hours'])
        except ValueError:
            min_hours = None

        hours_issue = None
        if max_hours is not None and total_hours > max_hours:
            displayed_total, displayed_max = round_to_half(total_hours), round_to_half(max_hours)
            hours_issue = f"{name} is scheduled for {displayed_total:.1f} hrs, exceeding their {displayed_max:.1f} hr maximum by {displayed_total - displayed_max:.1f} hrs."
        elif min_hours is not None and total_hours < min_hours:
            displayed_total, displayed_min = round_to_half(total_hours), round_to_half(min_hours)
            hours_issue = f"{name} is scheduled for only {displayed_total:.1f} hrs, below their {displayed_min:.1f} hr minimum by {displayed_min - displayed_total:.1f} hrs."

        if hours_issue:
            total_conflicts += 1

        conflict_data[name] = {"shifts": emp_shifts, "hours_issue": hours_issue, "total_hours": total_hours}
    return conflict_data, total_conflicts

def render_schedule_summary(data_frame):
    """Shows a quick rollup of total/assigned/gap shift counts so success rate is visible without scrolling."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    total_shifts = len(data_frame)
    gap_mask = is_gap_mask(data_frame)
    future_mask = data_frame['Date'].astype(str).str.strip() >= today_str
    gap_shifts = int((gap_mask & future_mask).sum())  # only upcoming unfilled slots
    assigned_shifts = total_shifts - int(gap_mask.sum())

    col_total, col_assigned, col_gaps = st.columns(3)
    col_total.metric("Total Shifts", total_shifts)
    col_assigned.metric("Assigned", assigned_shifts)

    if gap_shifts > 0:
        col_gaps.markdown(f"""
            <div style="line-height: 1.2;">
                <div style="font-size: 0.875rem;">Open Gaps</div>
                <div style="font-size: 2.25rem; font-weight: 600; color: #d93025;">{gap_shifts}</div>
            </div>
        """, unsafe_allow_html=True)
    else:
        col_gaps.metric("Open Gaps", gap_shifts)

# 6. Render Views
# --- Pre-compute tab badge labels ---
# history_tab_label, history_df_preview, unseen_direct_edits, and latest_direct_edit_ts
# are already computed above the control panel so Step 2 can reference them.
audit_tab_label = "👤 Employee Validation Audit"
employees_df = pd.DataFrame()
active_employees = pd.DataFrame()
employee_conflicts = {}
total_conflict_count = 0
rules_map = {}

if unseen_direct_edits > 0:
    st.warning(f"🔔 **{unseen_direct_edits} new direct Google Sheet edit{'s' if unseen_direct_edits != 1 else ''} detected** — see the History tab for details.")

if working_df is not None and not working_df.empty:
    # Pre-compute validation conflicts once so the tab label itself can show a rollup count,
    # without requiring the audit tab to be opened first. This runs on every rerun of the
    # whole app (e.g. clicking "Revert" in the Table Editor tab), so when the 10-minute
    # cache below is cold it can trigger a real Claude call — wrap it in a spinner so that
    # wait is visible instead of making unrelated buttons look hung.
    employees_df, _, _ = cached_load_tab_data()
    active_employees = employees_df[employees_df['Status'].str.lower() == 'active']
    if not active_employees.empty:
        with st.spinner("Validating employee schedules against availability rules..."):
            rules_map = cached_normalize_rules(selected_year, selected_month, active_employees)
            employee_conflicts, total_conflict_count = compute_employee_conflicts(working_df, active_employees, rules_map)

    if total_conflict_count > 0:
        audit_tab_label += f" 🚨 {total_conflict_count}"
        # Tab labels in Streamlit can't be styled, so surface a prominent banner above the
        # tabs too — visible immediately regardless of which tab happens to be selected.
        st.error(f"🚨 **{total_conflict_count} validation conflict{'s' if total_conflict_count != 1 else ''} found** — see the Employee Validation Audit tab for details.")

# All employee names (active + inactive) used to constrain the Assigned Employee
# dropdowns in the table and day editors. Includes inactive so existing assignments
# to recently-deactivated employees still display correctly.
emp_names_for_dropdown = (
    [""] + sorted(employees_df['Employee Name'].tolist())
    if working_df is not None and not working_df.empty and not employees_df.empty
    else [""]
)

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    "📊 Table Editor View", "📅 Calendar Grid View",
    audit_tab_label, "👥 Employee List", "📋 Shift Templates", "🗓️ Holidays", history_tab_label,
    "📈 Year-to-Date"
])

with tab1:
    if working_df is not None and not working_df.empty:
        render_schedule_summary(working_df)
        st.write("Double-click **Assigned Employee** cells below to manually override names.")
        filter_row_col1, filter_row_col2, filter_row_col3 = st.columns([2, 2, 1])
        with filter_row_col1:
            show_gaps_only_table = st.toggle("🔍 Show only gaps", key="table_gaps_toggle")
        table_source_df = filter_gaps_only(working_df) if show_gaps_only_table else working_df

        with filter_row_col2:
            _emp_names_tab1 = sorted([n for n in employees_df['Employee Name'].tolist() if str(n).strip()]) if not employees_df.empty else []
            if _emp_names_tab1:
                _emp_filter_tab1 = st.selectbox("Filter by employee", ["All employees"] + _emp_names_tab1, key="emp_filter_tab1", label_visibility="collapsed")
                if _emp_filter_tab1 != "All employees":
                    table_source_df = table_source_df[table_source_df['Assigned Employee'] == _emp_filter_tab1]

        with filter_row_col3:
            _csv_data = table_source_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", data=_csv_data, file_name=f"schedule_{selected_year}_{selected_month:02d}.csv", mime="text/csv", use_container_width=True)

        # Reserve a slot above the table for the save bar now, and fill it in after the data
        # editor renders below — so the save button is visible without scrolling down to find
        # it, instead of trailing the (potentially long) table.
        save_bar = st.container()

        def highlight_gaps(row):
            emp_val = str(row['Assigned Employee']).strip() if pd.notna(row['Assigned Employee']) else ""
            is_gap = emp_val == '' or emp_val == 'GAP'
            is_future = str(row['Date']).strip() >= datetime.today().strftime('%Y-%m-%d')
            return ['background-color: rgba(220,50,50,0.25)' if (is_gap and is_future) else '' for _ in row]

        table_editor_key = "table_editor_data"
        edited_df = st.data_editor(
            table_source_df.style.apply(highlight_gaps, axis=1),
            use_container_width=True,
            hide_index=True,
            disabled=["Date", "Day of Week", "Day Type", "Start Time", "End Time", "Issues"],
            column_config={
                "Assigned Employee": st.column_config.SelectboxColumn(
                    "Assigned Employee",
                    options=emp_names_for_dropdown,
                    required=False,
                    help="Only employees from the Employee List tab are valid.",
                )
            },
            key=table_editor_key
        )

        if not edited_df.equals(table_source_df):
            with save_bar:
                st.warning("🚨 You have unsaved manual overrides in the table below.")
                save_col, revert_col = st.columns(2)
                if save_col.button("💾 Save Inline Changes to Google Sheet", type="primary", use_container_width=True):
                    capture_undo_snapshot("Table Editor Inline Save")
                    with st.spinner("Committing changes to Google Sheets..."):
                        try:
                            cleaned_df = edited_df.fillna("").copy()
                            cleaned_df['Date'] = cleaned_df['Date'].astype(str)
                            cleaned_df['Assigned Employee'] = cleaned_df['Assigned Employee'].astype(str).str.strip()

                            cleaned_df.loc[cleaned_df['Assigned Employee'] != "", 'Issues'] = ""
                            cleaned_df.loc[cleaned_df['Assigned Employee'] == "", 'Issues'] = "GAP"

                            # Merge edits back into the full month's data so saving while
                            # filtered to "gaps only" doesn't drop the unfiltered rows.
                            full_df = working_df.copy()
                            full_df.loc[cleaned_df.index, fallback_cols] = cleaned_df[fallback_cols]
                            full_df['Date'] = full_df['Date'].astype(str)

                            rows_to_write = full_df[fallback_cols].values.tolist()
                            scheduler.write_to_spreadsheet(selected_year, selected_month, rows_to_write, method="Manual Override (Table Editor)")
                            st.success("Changes permanently saved to Google Sheets!")
                            st.session_state["schedule_df"] = None
                            cached_fetch_all_assignments.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to commit changes: {e}")
                if revert_col.button("↩️ Revert Unsaved Changes", type="secondary", use_container_width=True):
                    del st.session_state[table_editor_key]
                    st.rerun()
        render_inline_undo("Table Editor Inline Save")
    else:
        st.info("No shifts found for this month. Use Step 1 above to initialize first.")

with tab2:
    if working_df is not None and not working_df.empty:
        render_schedule_summary(working_df)
        st.write("Overview of all daily assignments and gaps at a glance.")
        cal_filter_col1, cal_filter_col2 = st.columns([2, 2])
        with cal_filter_col1:
            show_gaps_only_calendar = st.toggle("🔍 Show only gaps", key="calendar_gaps_toggle")
        calendar_source_df = filter_gaps_only(working_df) if show_gaps_only_calendar else working_df

        with cal_filter_col2:
            _emp_names_tab2 = sorted([n for n in employees_df['Employee Name'].tolist() if str(n).strip()]) if not employees_df.empty else []
            if _emp_names_tab2:
                _emp_filter_tab2 = st.selectbox("Filter by employee", ["All employees"] + _emp_names_tab2, key="emp_filter_tab2", label_visibility="collapsed")
                if _emp_filter_tab2 != "All employees":
                    calendar_source_df = calendar_source_df[calendar_source_df['Assigned Employee'] == _emp_filter_tab2]

        _, _, holidays_df_for_cal = cached_load_tab_data()
        calendar_html = render_html_calendar_grid(selected_year, selected_month, calendar_source_df, holidays_df_for_cal)
        st.markdown(calendar_html, unsafe_allow_html=True)

        st.write("")
        st.subheader("✏️ Quick Day Editor")
        st.write("Select a specific calendar day to modify its internal shifts directly.")

        min_date = datetime(selected_year, selected_month, 1)
        if selected_month == 12:
            max_date = datetime(selected_year, selected_month, 31)
        else:
            max_date = datetime(selected_year, selected_month + 1, 1) - timedelta(days=1)

        today = datetime.today()
        default_edit_date = today if min_date <= today <= max_date else min_date
        edit_date = st.date_input("Choose date to edit:", value=default_edit_date, min_value=min_date, max_value=max_date)
        edit_date_str = edit_date.strftime("%Y-%m-%d")

        day_shifts = working_df[working_df['Date'] == edit_date_str].copy()

        if not day_shifts.empty:
            edited_day_df = st.data_editor(
                day_shifts,
                use_container_width=True,
                hide_index=True,
                disabled=["Date", "Day of Week", "Day Type", "Start Time", "End Time", "Issues"],
                column_config={
                    "Assigned Employee": st.column_config.SelectboxColumn(
                        "Assigned Employee",
                        options=emp_names_for_dropdown,
                        required=False,
                        help="Only employees from the Employee List tab are valid.",
                    )
                },
                key=f"day_editor_{edit_date_str}"
            )

            if not edited_day_df.equals(day_shifts):
                if st.button("💾 Save Day Overrides to Google Sheet", type="primary", use_container_width=True):
                    capture_undo_snapshot(f"Day Shift Overrides for {edit_date_str}")
                    with st.spinner("Updating spreadsheet matrix..."):
                        try:
                            main_df = working_df.fillna("").copy()
                            main_df['Date'] = main_df['Date'].astype(str)

                            cleaned_day_df = edited_day_df.fillna("").copy()
                            cleaned_day_df['Date'] = cleaned_day_df['Date'].astype(str)
                            cleaned_day_df['Assigned Employee'] = cleaned_day_df['Assigned Employee'].astype(str).str.strip()

                            cleaned_day_df.loc[cleaned_day_df['Assigned Employee'] != "", 'Issues'] = ""
                            cleaned_day_df.loc[cleaned_day_df['Assigned Employee'] == "", 'Issues'] = "GAP"

                            main_df.loc[main_df['Date'] == edit_date_str] = cleaned_day_df
                            rows_to_write = main_df[fallback_cols].values.tolist()
                            scheduler.write_to_spreadsheet(selected_year, selected_month, rows_to_write, method="Manual Override (Day Editor)")
                            st.success(f"Successfully saved overrides for {edit_date_str}!")
                            st.session_state["schedule_df"] = None
                            cached_fetch_all_assignments.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to commit day shifts: {e}")
            render_inline_undo(f"Day Shift Overrides for {edit_date_str}")
        else:
            st.info("No structural shifts exist for the chosen day template layout.")
    else:
        st.info("No shifts found for this month. Use Step 1 above to initialize first.")

with tab3:
    if working_df is not None and not working_df.empty:
        st.write("Cross-reference scheduled workloads against default preference constraints and rule parameters.")
        try:
            if not active_employees.empty:
                # Hours summary table — quick view of every employee's assigned hours vs their
                # min/max targets, so the distribution is visible without expanding each row.
                summary_rows = []
                for _, _emp in active_employees.iterrows():
                    _name = _emp['Employee Name']
                    _entry = employee_conflicts.get(_name, {"shifts": pd.DataFrame(), "hours_issue": None, "total_hours": 0.0})
                    _total = round_to_half(_entry["total_hours"])
                    _conflict_cnt = int(_entry["shifts"]['Conflict'].sum()) if not _entry["shifts"].empty and 'Conflict' in _entry["shifts"].columns else 0
                    _has_issue = bool(_entry["hours_issue"]) or _conflict_cnt > 0
                    summary_rows.append({
                        "": "🚨" if _has_issue else "✅",
                        "Employee": _name,
                        "Shifts": len(_entry["shifts"]),
                        "Hours": f"{_total:.1f}",
                        "Min": format_hours_target(_emp['Min Hours']),
                        "Max": format_hours_target(_emp['Max Hours']),
                    })
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
                st.divider()

                if total_conflict_count > 0:
                    st.error(f"🚨 {total_conflict_count} total validation conflict{'s' if total_conflict_count != 1 else ''} found across all employees — likely manual overrides. Expand an employee below for details.")

                show_conflicts_only = st.toggle("🚨 Show only employees with conflicts", key="audit_conflicts_toggle")

                if show_conflicts_only and total_conflict_count == 0:
                    st.success("✅ No validation conflicts found for this month.")

                # First pass: gather display fields for every employee that will actually be
                # shown, so column widths can be computed before any label is built.
                display_rows = []
                for _, employee in active_employees.iterrows():
                    name = employee['Employee Name']
                    entry = employee_conflicts.get(name, {"shifts": working_df.iloc[0:0].copy(), "hours_issue": None, "total_hours": 0.0})
                    emp_shifts = entry["shifts"]
                    hours_issue = entry["hours_issue"]
                    conflict_count = int(emp_shifts['Conflict'].sum()) if not emp_shifts.empty else 0

                    try:
                        max_h_zero = str(employee.get('Max Hours', '')).strip() not in ('', 'nan') and float(employee['Max Hours']) == 0
                    except (ValueError, TypeError):
                        max_h_zero = False

                    issue_count = conflict_count + (1 if hours_issue else 0) + (1 if max_h_zero else 0)

                    if show_conflicts_only and issue_count == 0:
                        continue

                    display_rows.append({
                        "employee": employee,
                        "name": name,
                        "emp_shifts": emp_shifts,
                        "hours_issue": hours_issue,
                        "max_h_zero": max_h_zero,
                        "conflict_count": conflict_count,
                        "issue_count": issue_count,
                        "shifts_field": f"Shifts: {len(emp_shifts)}",
                        "hours_value": f"{round_to_half(entry['total_hours']):.1f}",
                        "min_value": format_hours_target(employee['Min Hours']),
                        "max_value": format_hours_target(employee['Max Hours']),
                    })

                # Expander labels render in a monospace font, so padding each field to the
                # widest value in its column — with non-breaking spaces so the padding doesn't
                # collapse in HTML — makes every "|" line up vertically. The Hours/Min/Max
                # columns right-justify just the number (not the whole "Label: value" field), so
                # the decimal points line up too, e.g. "Max Hours: 10.5" / "Max Hours:  8.5".
                # The name column uses a fixed baseline (rather than always sizing to whoever's
                # longest right now) so adding a longer-named employee later doesn't reflow
                # every other row's padding — just widen NAME_COL_WIDTH if someone ever exceeds it.
                NBSP = "\xa0"
                NAME_COL_WIDTH = 12
                name_width = max(NAME_COL_WIDTH, max((len(r["name"]) for r in display_rows), default=0))
                shifts_width = max((len(r["shifts_field"]) for r in display_rows), default=0)
                hours_value_width = max((len(r["hours_value"]) for r in display_rows), default=0)
                min_value_width = max((len(r["min_value"]) for r in display_rows), default=0)
                max_value_width = max((len(r["max_value"]) for r in display_rows), default=0)

                for r in display_rows:
                    r["hours_field"] = f"Hours: {r['hours_value'].rjust(hours_value_width, NBSP)}"
                    r["min_field"] = f"Min Hours: {r['min_value'].rjust(min_value_width, NBSP)}"
                    r["max_field"] = f"Max Hours: {r['max_value'].rjust(max_value_width, NBSP)}"

                for row in display_rows:
                    name = row["name"]
                    emp_shifts = row["emp_shifts"]
                    hours_issue = row["hours_issue"]
                    conflict_count = row["conflict_count"]
                    issue_count = row["issue_count"]
                    employee = row["employee"]

                    sep = NBSP + "|" + NBSP
                    warning_tag = f"{sep}🚨 {issue_count} conflict{'s' if issue_count != 1 else ''}" if issue_count > 0 else ""
                    label = (
                        f"👤{NBSP}{name.ljust(name_width, NBSP)}{sep}"
                        f"{row['shifts_field'].ljust(shifts_width, NBSP)}{sep}"
                        f"{row['hours_field']}{sep}"
                        f"{row['min_field']}{sep}"
                        f"{row['max_field']}{warning_tag}"
                    )
                    with st.expander(label):
                        col_rules, col_deviations, col_start = st.columns(3)
                        with col_rules:
                            st.markdown(f"**Baseline Availability Rules:**\n`{employee['Default Rules'] if employee['Default Rules'] else 'None Listed'}`")
                        with col_deviations:
                            st.markdown(f"**Blocked Dates:**\n`{employee['Blocked Dates'] if employee['Blocked Dates'] else 'None Listed'}`")
                        with col_start:
                            st.markdown(f"**Start Date:**\n`{employee['Start Date'] if employee['Start Date'] else 'Already eligible'}`")

                        if hours_issue:
                            st.warning(f"🚨 Hour cap violation — likely from a manual edit: {hours_issue}")

                        if row.get("max_h_zero"):
                            st.warning(f"⚠️ Max Hours is set to 0 — this employee will never be assigned any shifts. Update their Max Hours in the Employee List tab.")

                        if not emp_shifts.empty:
                            if conflict_count > 0:
                                parsed_rules = rules_map.get(name, {})
                                start_date = scheduler.normalize_date_string(employee['Start Date'])
                                st.warning(f"🚨 {conflict_count} shift(s) conflict with {name}'s stated Default Rules, Blocked Dates, or Start Date — likely a manual override:")
                                for _, row in emp_shifts[emp_shifts['Conflict']].iterrows():
                                    st.markdown(f"- **{row['Date']} ({row['Day of Week']})** — {describe_conflict(name, parsed_rules, row, start_date)}")

                            def highlight_conflicts(row):
                                is_conflict = bool(emp_shifts.loc[row.name, 'Conflict'])
                                return ['background-color: rgba(220,50,50,0.2)' if is_conflict else '' for _ in row]

                            st.dataframe(
                                emp_shifts[["Date", "Day of Week", "Start Time", "End Time", "Day Type"]].style.apply(highlight_conflicts, axis=1),
                                use_container_width=True,
                                hide_index=True
                            )
                        else:
                            st.info(f"No active shift components mapped to {name} inside this calendar cross-section.")
            else:
                st.warning("No active employees configured inside the roster system.")

            # --- Data Quality Checks ---
            st.divider()
            st.subheader("Data Quality Checks")

            known_names = set(employees_df['Employee Name'].str.strip().str.lower()) if not employees_df.empty else set()
            assigned_in_schedule = {
                str(n).strip() for n in working_df['Assigned Employee'].dropna().unique()
                if str(n).strip() not in ("", "GAP")
            }
            orphaned_names = {n for n in assigned_in_schedule if n.lower() not in known_names}

            if orphaned_names:
                st.error(f"🔴 {len(orphaned_names)} name(s) in the schedule have no matching entry in the Employee List — they may have been deleted or renamed directly in the sheet:")
                for oname in sorted(orphaned_names):
                    st.markdown(f"- **{oname}**")
            else:
                st.success("✅ All assigned employee names match the Employee List.")

            from collections import Counter
            dup_counts = Counter(
                (str(row['Date']), str(row['Start Time']), str(row['Assigned Employee']))
                for _, row in working_df.iterrows()
                if str(row.get('Assigned Employee', '')).strip() not in ('', 'GAP')
            )
            dup_keys = {k: v for k, v in dup_counts.items() if v > 1}
            if dup_keys:
                st.warning(f"⚠️ {len(dup_keys)} duplicate assignment(s) found — the same employee appears more than once in the same slot:")
                for (d, t, emp), cnt in sorted(dup_keys.items()):
                    st.markdown(f"- **{emp}** on {d} at {t} — appears {cnt}× in the sheet")
            else:
                st.success("✅ No duplicate assignments found.")

        except Exception as e:
            st.error(f"Failed to compile audit summary profile matrix: {e}")
    else:
        st.info("No shifts found for this month. Use Step 1 above to initialize first.")

with tab4:
    st.write("Add, edit, or deactivate employees. Changes save directly to the Employees tab of the Google Sheet.")
    st.caption("Double-click any cell to edit. Use the ➕ button at the bottom to add a new employee. Click the checkbox on a row then the delete icon to remove one.")

    emp_df_for_list, _, _ = cached_load_tab_data()
    employee_list_cols = ['Employee Name', 'Status', 'Default Rules', 'Blocked Dates', 'Min Hours', 'Max Hours', 'Start Date']
    display_df = emp_df_for_list[employee_list_cols].copy()
    # Normalize Status to lowercase so it matches the SelectboxColumn options regardless of
    # how it was capitalized in the Google Sheet (e.g. "Active" → "active").
    display_df['Status'] = display_df['Status'].str.lower().str.strip()
    # Normalize Start Date to YYYY-MM-DD for display; empty/unparseable stays blank.
    display_df['Start Date'] = display_df['Start Date'].apply(
        lambda s: scheduler.normalize_date_string(str(s).strip()) or ""
        if str(s).strip() not in ("", "nan", "None") else ""
    )
    # Convert Min/Max Hours to float so the NumberColumn renders correctly.
    for _col in ('Min Hours', 'Max Hours'):
        display_df[_col] = pd.to_numeric(display_df[_col], errors='coerce')

    # --- External change detection ---
    # Hash the current data from the sheet. If it differs from the last hash we stored in
    # session state, the sheet was edited outside the app (direct edit or another browser
    # session) since we last loaded it.
    current_emp_hash = hash(display_df.to_csv(index=False))
    if st.session_state["employee_data_hash"] is None:
        # First load after session start, save, or force-refresh — initialize quietly.
        st.session_state["employee_data_hash"] = current_emp_hash
        st.session_state["employee_last_synced"] = datetime.now()
        st.session_state["employee_snapshot_csv"] = emp_df_for_list.to_csv(index=False)
    elif current_emp_hash != st.session_state["employee_data_hash"]:
        # Cache was refreshed and data differs — flag an external change and log to History.
        old_snapshot_csv = st.session_state.get("employee_snapshot_csv")
        if old_snapshot_csv:
            try:
                import io
                old_snapshot_df = pd.read_csv(io.StringIO(old_snapshot_csv)).fillna("")
                scheduler.log_employee_changes(old_snapshot_df, emp_df_for_list, edited_by="Direct Sheet Edit")
            except Exception as _e:
                print(f"[WARN] Could not log direct employee sheet edit to History: {_e}")
        st.session_state["employee_data_hash"] = current_emp_hash
        st.session_state["employee_last_synced"] = datetime.now()
        st.session_state["employee_snapshot_csv"] = emp_df_for_list.to_csv(index=False)
        st.session_state["employee_external_change"] = True

    if st.session_state["employee_external_change"]:
        banner_col, dismiss_col = st.columns([5, 1])
        with banner_col:
            st.warning("⚠️ The employee list in Google Sheets was changed outside this app. The table below already reflects the latest data — review it before making further edits.")
        with dismiss_col:
            st.write("")
            if st.button("Dismiss", key="dismiss_emp_change"):
                st.session_state["employee_external_change"] = False
                st.rerun()

    # Last-synced timestamp + targeted refresh button
    sync_ts_col, refresh_btn_col = st.columns([5, 1])
    with sync_ts_col:
        if st.session_state["employee_last_synced"]:
            elapsed_secs = int((datetime.now() - st.session_state["employee_last_synced"]).total_seconds())
            if elapsed_secs < 60:
                sync_label = "just now"
            elif elapsed_secs < 3600:
                mins = elapsed_secs // 60
                sync_label = f"{mins} minute{'s' if mins != 1 else ''} ago"
            else:
                hrs = elapsed_secs // 3600
                sync_label = f"{hrs} hour{'s' if hrs != 1 else ''} ago"
            st.caption(f"Last synced from Google Sheets: {sync_label}")
    with refresh_btn_col:
        if st.button("🔄 Refresh", use_container_width=True, key="refresh_employee_list_btn"):
            st.cache_data.clear()
            st.session_state["employee_data_hash"] = None
            st.session_state["employee_last_synced"] = None
            st.session_state["employee_external_change"] = False
            st.session_state["employee_snapshot_csv"] = None
            st.rerun()

    employee_list_key = "employee_list_editor"
    edited_employees = st.data_editor(
        display_df,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "Employee Name": st.column_config.TextColumn("Employee Name", required=True),
            "Status": st.column_config.SelectboxColumn("Status", options=["active", "inactive"], required=True),
            "Default Rules": st.column_config.TextColumn("Default Rules", help="e.g. 'Mon/Wed/Fri mornings only'"),
            "Blocked Dates": st.column_config.TextColumn("Blocked Dates", help="e.g. '2026-07-04, 2026-07-15'"),
            "Min Hours": st.column_config.NumberColumn("Min Hours", min_value=0.0, step=0.5, format="%.1f"),
            "Max Hours": st.column_config.NumberColumn("Max Hours", min_value=0.0, step=0.5, format="%.1f"),
            "Start Date": st.column_config.TextColumn("Start Date", help="e.g. 2026-07-04 or 7/4/2026. Leave blank if already eligible."),
        },
        key=employee_list_key,
    )

    if not edited_employees.equals(display_df):
        # --- Server-side validation before save ---
        save_errors = []
        save_warnings = []
        seen_names = set()
        for idx, row in edited_employees.iterrows():
            name = str(row.get('Employee Name') or "").strip()
            if not name:
                save_errors.append(f"Row {idx + 1}: Employee Name cannot be blank.")
                continue
            if name.lower() in seen_names:
                save_errors.append(f"Duplicate employee name: **{name}** — each employee must be unique.")
            seen_names.add(name.lower())
            min_h = row.get('Min Hours')
            max_h = row.get('Max Hours')
            try:
                if pd.notna(min_h) and pd.notna(max_h):
                    if float(max_h) < float(min_h):
                        save_errors.append(f"**{name}**: Max Hours ({max_h:.1f}) cannot be less than Min Hours ({min_h:.1f}).")
            except (TypeError, ValueError):
                pass
            start_date_raw = str(row.get('Start Date') or "").strip()
            if start_date_raw and start_date_raw not in ("nan", "None"):
                if not scheduler.normalize_date_string(start_date_raw):
                    save_errors.append(f"**{name}**: Start Date '{start_date_raw}' is not a recognizable date. Use a format like 2026-07-04 or 7/4/2026.")
            rules_text = str(row.get('Default Rules') or "")
            if len(rules_text) > 500:
                save_warnings.append(f"**{name}**: Default Rules is very long ({len(rules_text)} chars). Consider shortening to under 500 characters for reliable AI scheduling.")

        if save_errors:
            for err in save_errors:
                st.error(err)
        if save_warnings:
            for warn in save_warnings:
                st.warning(warn)

        save_emp_col, revert_emp_col = st.columns(2)
        if save_emp_col.button("💾 Save Employee Changes to Google Sheet", type="primary", use_container_width=True, key="save_employees_btn", disabled=bool(save_errors)):
            with st.spinner("Saving employee list to Google Sheets..."):
                try:
                    clean_save_df = edited_employees.dropna(subset=["Employee Name"]).copy()
                    clean_save_df = clean_save_df[clean_save_df["Employee Name"].astype(str).str.strip() != ""]
                    # Normalize Start Date strings to YYYY-MM-DD for Google Sheets.
                    clean_save_df['Start Date'] = clean_save_df['Start Date'].apply(
                        lambda s: scheduler.normalize_date_string(str(s).strip()) or ""
                        if str(s).strip() not in ("", "nan", "None") else ""
                    )
                    # Ensure numeric columns are stored as plain strings (no .0 suffix on integers).
                    for _col in ('Min Hours', 'Max Hours'):
                        clean_save_df[_col] = clean_save_df[_col].apply(
                            lambda v: "" if pd.isna(v) else (str(int(v)) if v == int(v) else str(v))
                        )
                    # Load fresh sheet data: detect concurrent changes, and use as accurate "before" state for history.
                    fresh_emp_df, _, _ = scheduler.load_tab_data()
                    if (st.session_state.get("employee_snapshot_csv") and
                            fresh_emp_df.to_csv(index=False) != st.session_state["employee_snapshot_csv"]):
                        st.error("⚠️ The Employees sheet was modified since you loaded this view. Click 🔄 Refresh to load the latest data, then re-apply your changes.")
                    else:
                        try:
                            scheduler.log_employee_changes(fresh_emp_df, clean_save_df)
                        except Exception as _e:
                            print(f"[WARN] Failed to log employee changes to History: {_e}")
                        st.session_state["employee_undo_snapshot"] = fresh_emp_df[employee_list_cols].to_csv(index=False)
                        scheduler.write_employees_to_sheet(clean_save_df)
                        st.cache_data.clear()
                        st.session_state["employee_data_hash"] = None
                        st.session_state["employee_last_synced"] = None
                        st.session_state["employee_external_change"] = False
                        st.session_state["employee_snapshot_csv"] = None
                        st.success("Employee list saved and Google Sheet validation rules updated!")
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed to save employee list: {e}")
        if revert_emp_col.button("↩️ Revert Changes", type="secondary", use_container_width=True, key="revert_employees_btn"):
            del st.session_state[employee_list_key]
            st.rerun()

    if st.session_state.get("employee_undo_snapshot"):
        st.info("Employee list saved. You can undo this save to restore the previous state.")
        if st.button("⏪ Undo Last Employee Save", type="secondary", use_container_width=True, key="undo_employee_save"):
            with st.spinner("Restoring previous employee list..."):
                try:
                    import io as _io
                    snap_df = pd.read_csv(_io.StringIO(st.session_state["employee_undo_snapshot"])).fillna("")
                    scheduler.write_employees_to_sheet(snap_df)
                    st.session_state["employee_undo_snapshot"] = None
                    st.cache_data.clear()
                    st.session_state["employee_data_hash"] = None
                    st.session_state["employee_last_synced"] = None
                    st.session_state["employee_external_change"] = False
                    st.session_state["employee_snapshot_csv"] = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to undo employee save: {e}")

    st.divider()
    st.caption("💡 Sheet validation rules are updated automatically on every save. To apply them to an existing sheet without changes, use the button below.")
    if st.button("🔧 (Re)apply Google Sheet Validation Rules", type="secondary", key="setup_validation_btn"):
        with st.spinner("Applying validation rules to Google Sheets..."):
            try:
                current_emp_df, _, _ = cached_load_tab_data()
                scheduler.setup_sheet_validation(current_emp_df)
                st.success("Validation rules applied to Employees and Assignments sheets.")
            except Exception as e:
                st.error(f"Failed to apply validation rules: {e}")

with tab5:
    st.write("Add, edit, or remove shift templates. Changes take effect the next time you run Auto-Assign or Initialize Month.")
    st.caption("Each row is one shift slot per day. Set **Staff Required** ≥ 1 to generate multiple open slots for the same shift.")

    _, tmpl_df_raw, _ = cached_load_tab_data()
    template_cols = ['Day Type', 'Start Time', 'End Time', 'Staff Required']
    display_tmpl_df = tmpl_df_raw[template_cols].copy()
    # Coerce Staff Required to int so the NumberColumn renders correctly.
    display_tmpl_df['Staff Required'] = pd.to_numeric(display_tmpl_df['Staff Required'], errors='coerce').astype('Int64')

    if "template_data_hash" not in st.session_state:
        st.session_state["template_data_hash"] = None
    if "template_last_synced" not in st.session_state:
        st.session_state["template_last_synced"] = None
    if "template_external_change" not in st.session_state:
        st.session_state["template_external_change"] = False
    if "template_snapshot_csv" not in st.session_state:
        st.session_state["template_snapshot_csv"] = None

    current_tmpl_hash = hash(display_tmpl_df.to_csv(index=False))
    if st.session_state["template_data_hash"] is None:
        st.session_state["template_data_hash"] = current_tmpl_hash
        st.session_state["template_last_synced"] = datetime.now()
        st.session_state["template_snapshot_csv"] = tmpl_df_raw.to_csv(index=False)
    elif current_tmpl_hash != st.session_state["template_data_hash"]:
        # Cache refreshed and data differs — log the external change then update state.
        old_tmpl_snapshot_csv = st.session_state.get("template_snapshot_csv")
        if old_tmpl_snapshot_csv:
            try:
                import io
                old_tmpl_snapshot_df = pd.read_csv(io.StringIO(old_tmpl_snapshot_csv)).fillna("")
                scheduler.log_template_changes(old_tmpl_snapshot_df, tmpl_df_raw, edited_by="Direct Sheet Edit")
            except Exception as _e:
                print(f"[WARN] Could not log direct template sheet edit to History: {_e}")
        st.session_state["template_data_hash"] = current_tmpl_hash
        st.session_state["template_last_synced"] = datetime.now()
        st.session_state["template_snapshot_csv"] = tmpl_df_raw.to_csv(index=False)
        st.session_state["template_external_change"] = True

    if st.session_state["template_external_change"]:
        tmpl_banner_col, tmpl_dismiss_col = st.columns([5, 1])
        with tmpl_banner_col:
            st.warning("⚠️ Shift Templates were changed outside this app. The table below already reflects the latest data.")
        with tmpl_dismiss_col:
            st.write("")
            if st.button("Dismiss", key="dismiss_tmpl_change"):
                st.session_state["template_external_change"] = False
                st.rerun()

    tmpl_sync_col, tmpl_refresh_col = st.columns([5, 1])
    with tmpl_sync_col:
        if st.session_state["template_last_synced"]:
            elapsed = int((datetime.now() - st.session_state["template_last_synced"]).total_seconds())
            if elapsed < 60:
                tmpl_sync_label = "just now"
            elif elapsed < 3600:
                m = elapsed // 60
                tmpl_sync_label = f"{m} minute{'s' if m != 1 else ''} ago"
            else:
                h = elapsed // 3600
                tmpl_sync_label = f"{h} hour{'s' if h != 1 else ''} ago"
            st.caption(f"Last synced from Google Sheets: {tmpl_sync_label}")
    with tmpl_refresh_col:
        if st.button("🔄 Refresh", use_container_width=True, key="refresh_templates_btn"):
            st.cache_data.clear()
            st.session_state["template_data_hash"] = None
            st.session_state["template_last_synced"] = None
            st.session_state["template_external_change"] = False
            st.session_state["template_snapshot_csv"] = None
            st.rerun()

    template_editor_key = "template_list_editor"
    edited_templates = st.data_editor(
        display_tmpl_df,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "Day Type": st.column_config.TextColumn("Day Type", help="Built-in: 'Weekday' or 'Saturday'. Add any custom name (e.g. 'Short Day') and reference it as a Holiday Override Type.", required=True),
            "Start Time": st.column_config.TextColumn("Start Time", help="e.g. '9:00 AM' or '14:00'", required=True),
            "End Time": st.column_config.TextColumn("End Time", help="e.g. '1:00 PM' or '17:00'", required=True),
            "Staff Required": st.column_config.NumberColumn("Staff Required", min_value=1, step=1, format="%d"),
        },
        key=template_editor_key,
    )

    if not edited_templates.equals(display_tmpl_df):
        tmpl_errors = []
        seen_slots = set()
        for idx, row in edited_templates.iterrows():
            day_type = str(row.get('Day Type') or "").strip()
            start = str(row.get('Start Time') or "").strip()
            end = str(row.get('End Time') or "").strip()
            staff = row.get('Staff Required')

            if not day_type:
                tmpl_errors.append(f"Row {idx + 1}: Day Type cannot be blank.")
            if not start:
                tmpl_errors.append(f"Row {idx + 1}: Start Time cannot be blank.")
            if not end:
                tmpl_errors.append(f"Row {idx + 1}: End Time cannot be blank.")

            if start and end:
                hours = scheduler.calculate_hours(start, end)
                if hours == 0.0:
                    tmpl_errors.append(f"Row {idx + 1} ({day_type}): Start Time '{start}' or End Time '{end}' could not be parsed — use a format like '9:00 AM' or '14:00'.")

            if day_type and start:
                slot_key = (day_type.lower(), start.lower())
                if slot_key in seen_slots:
                    tmpl_errors.append(f"Duplicate slot: **{day_type}** at **{start}** — each Day Type + Start Time combination must be unique.")
                seen_slots.add(slot_key)

            if pd.notna(staff):
                try:
                    if int(staff) < 1:
                        tmpl_errors.append(f"Row {idx + 1} ({day_type} {start}): Staff Required must be at least 1.")
                except (ValueError, TypeError):
                    tmpl_errors.append(f"Row {idx + 1}: Staff Required must be a whole number.")

        if tmpl_errors:
            for err in tmpl_errors:
                st.error(err)

        save_tmpl_col, revert_tmpl_col = st.columns(2)
        if save_tmpl_col.button("💾 Save Template Changes to Google Sheet", type="primary", use_container_width=True, key="save_templates_btn", disabled=bool(tmpl_errors)):
            with st.spinner("Saving shift templates to Google Sheets..."):
                try:
                    clean_tmpl_df = edited_templates.dropna(subset=["Day Type"]).copy()
                    clean_tmpl_df = clean_tmpl_df[clean_tmpl_df["Day Type"].astype(str).str.strip() != ""]
                    # Load fresh sheet data: detect concurrent changes, and use as accurate "before" state for history.
                    _, fresh_tmpl_df, _ = scheduler.load_tab_data()
                    if (st.session_state.get("template_snapshot_csv") and
                            fresh_tmpl_df.to_csv(index=False) != st.session_state["template_snapshot_csv"]):
                        st.error("⚠️ The Shift Templates sheet was modified since you loaded this view. Click 🔄 Refresh to load the latest data, then re-apply your changes.")
                    else:
                        try:
                            scheduler.log_template_changes(fresh_tmpl_df, clean_tmpl_df)
                        except Exception as _e:
                            print(f"[WARN] Failed to log template changes to History: {_e}")
                        st.session_state["template_undo_snapshot"] = fresh_tmpl_df[template_cols].to_csv(index=False)
                        scheduler.write_templates_to_sheet(clean_tmpl_df)
                        st.cache_data.clear()
                        st.session_state["template_data_hash"] = None
                        st.session_state["template_last_synced"] = None
                        st.session_state["template_external_change"] = False
                        st.session_state["template_snapshot_csv"] = None
                        st.success("Shift templates saved to Google Sheets!")
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed to save shift templates: {e}")
        if revert_tmpl_col.button("↩️ Revert Changes", type="secondary", use_container_width=True, key="revert_templates_btn"):
            del st.session_state[template_editor_key]
            st.rerun()

    if st.session_state.get("template_undo_snapshot"):
        st.info("Shift templates saved. You can undo this save to restore the previous state.")
        if st.button("⏪ Undo Last Template Save", type="secondary", use_container_width=True, key="undo_template_save"):
            with st.spinner("Restoring previous shift templates..."):
                try:
                    import io as _io
                    snap_df = pd.read_csv(_io.StringIO(st.session_state["template_undo_snapshot"])).fillna("")
                    scheduler.write_templates_to_sheet(snap_df)
                    st.session_state["template_undo_snapshot"] = None
                    st.cache_data.clear()
                    st.session_state["template_data_hash"] = None
                    st.session_state["template_last_synced"] = None
                    st.session_state["template_external_change"] = False
                    st.session_state["template_snapshot_csv"] = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to undo template save: {e}")

with tab6:
    st.write("Add, edit, or remove holidays. Set an **Override Type** to control which shift template is used on that day.")
    st.caption("Leave Override Type blank for the day-of-week default (Weekday or Saturday). Use **Closed** to skip the day entirely. Or enter any Day Type from the Shift Templates tab to use a custom schedule.")

    _, tmpl_df_for_holidays, holidays_df_raw = cached_load_tab_data()

    # Build Override Type options dynamically from current Shift Template Day Types.
    all_day_types = sorted(tmpl_df_for_holidays['Day Type'].dropna().astype(str).str.strip().unique().tolist())
    holiday_override_options = ["", "Closed"] + all_day_types

    holiday_display_cols = ['Date', 'Name', 'Override Type']
    display_holidays_df = holidays_df_raw[holiday_display_cols].copy()

    # Normalize Date to YYYY-MM-DD for display; empty/unparseable stays blank.
    display_holidays_df['Date'] = display_holidays_df['Date'].apply(
        lambda s: scheduler.normalize_date_string(str(s).strip()) or ""
        if str(s).strip() not in ("", "nan", "None") else ""
    )
    display_holidays_df['Override Type'] = display_holidays_df['Override Type'].apply(
        lambda v: "" if str(v).strip().lower() in ("nan", "none") else str(v).strip()
    )

    if "holiday_data_hash" not in st.session_state:
        st.session_state["holiday_data_hash"] = None
    if "holiday_last_synced" not in st.session_state:
        st.session_state["holiday_last_synced"] = None
    if "holiday_external_change" not in st.session_state:
        st.session_state["holiday_external_change"] = False
    if "holiday_snapshot_csv" not in st.session_state:
        st.session_state["holiday_snapshot_csv"] = None

    current_holiday_hash = hash(display_holidays_df.to_csv(index=False))
    if st.session_state["holiday_data_hash"] is None:
        st.session_state["holiday_data_hash"] = current_holiday_hash
        st.session_state["holiday_last_synced"] = datetime.now()
        st.session_state["holiday_snapshot_csv"] = holidays_df_raw.to_csv(index=False)
    elif current_holiday_hash != st.session_state["holiday_data_hash"]:
        old_holiday_csv = st.session_state.get("holiday_snapshot_csv")
        if old_holiday_csv:
            try:
                import io
                old_holiday_df = pd.read_csv(io.StringIO(old_holiday_csv)).fillna("")
                scheduler.log_holiday_changes(old_holiday_df, holidays_df_raw, edited_by="Direct Sheet Edit")
            except Exception as _e:
                print(f"[WARN] Could not log direct holiday sheet edit to History: {_e}")
        st.session_state["holiday_data_hash"] = current_holiday_hash
        st.session_state["holiday_last_synced"] = datetime.now()
        st.session_state["holiday_snapshot_csv"] = holidays_df_raw.to_csv(index=False)
        st.session_state["holiday_external_change"] = True

    if st.session_state["holiday_external_change"]:
        h_banner_col, h_dismiss_col = st.columns([5, 1])
        with h_banner_col:
            st.warning("⚠️ Holidays were changed outside this app. The table below already reflects the latest data.")
        with h_dismiss_col:
            st.write("")
            if st.button("Dismiss", key="dismiss_holiday_change"):
                st.session_state["holiday_external_change"] = False
                st.rerun()

    h_sync_col, h_refresh_col = st.columns([5, 1])
    with h_sync_col:
        if st.session_state["holiday_last_synced"]:
            elapsed = int((datetime.now() - st.session_state["holiday_last_synced"]).total_seconds())
            if elapsed < 60:
                h_sync_label = "just now"
            elif elapsed < 3600:
                m = elapsed // 60
                h_sync_label = f"{m} minute{'s' if m != 1 else ''} ago"
            else:
                h = elapsed // 3600
                h_sync_label = f"{h} hour{'s' if h != 1 else ''} ago"
            st.caption(f"Last synced from Google Sheets: {h_sync_label}")
    with h_refresh_col:
        if st.button("🔄 Refresh", use_container_width=True, key="refresh_holidays_btn"):
            st.cache_data.clear()
            st.session_state["holiday_data_hash"] = None
            st.session_state["holiday_last_synced"] = None
            st.session_state["holiday_external_change"] = False
            st.session_state["holiday_snapshot_csv"] = None
            st.rerun()

    holiday_editor_key = "holiday_list_editor"
    edited_holidays = st.data_editor(
        display_holidays_df,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "Date": st.column_config.TextColumn("Date", help="e.g. 2026-07-04 or 7/4/2026", required=True),
            "Name": st.column_config.TextColumn("Name", help="e.g. 'Christmas', 'Thanksgiving'", required=True),
            "Override Type": st.column_config.SelectboxColumn("Override Type", options=holiday_override_options,
                help="Blank = day-of-week default. 'Closed' = skip. Any Shift Template Day Type = use that schedule."),
        },
        key=holiday_editor_key,
    )

    if not edited_holidays.equals(display_holidays_df):
        holiday_errors = []
        seen_dates = set()
        for idx, row in edited_holidays.iterrows():
            h_date = row.get('Date')
            h_name = str(row.get('Name') or "").strip()
            h_override_raw = row.get('Override Type')
            h_override = "" if (h_override_raw is None or str(h_override_raw).strip().lower() in ("", "nan", "none")) else str(h_override_raw).strip()

            if not h_date or str(h_date).strip() in ("", "nan", "None"):
                holiday_errors.append(f"Row {idx + 1}: Date cannot be blank.")
                continue
            normalized_h_date = scheduler.normalize_date_string(str(h_date).strip())
            if not normalized_h_date:
                holiday_errors.append(f"Row {idx + 1}: Date '{h_date}' is not a recognizable date. Use a format like 2026-07-04 or 7/4/2026.")
                continue
            if not h_name:
                holiday_errors.append(f"Row {idx + 1}: Name cannot be blank.")

            date_key = normalized_h_date
            if date_key in seen_dates:
                holiday_errors.append(f"Duplicate date: **{date_key}** — each date can only appear once.")
            seen_dates.add(date_key)

            if h_override and h_override not in holiday_override_options:
                holiday_errors.append(f"**{date_key}** ({h_name}): Override Type '{h_override}' is not a known Day Type. Add it to Shift Templates first.")

        if holiday_errors:
            for err in holiday_errors:
                st.error(err)

        save_h_col, revert_h_col = st.columns(2)
        if save_h_col.button("💾 Save Holiday Changes to Google Sheet", type="primary", use_container_width=True, key="save_holidays_btn", disabled=bool(holiday_errors)):
            with st.spinner("Saving holidays to Google Sheets..."):
                try:
                    clean_holidays_df = edited_holidays.copy()
                    clean_holidays_df = clean_holidays_df[
                        clean_holidays_df['Date'].apply(lambda s: bool(str(s).strip()) and str(s).strip() not in ("nan", "None"))
                    ]
                    # Normalize date strings to YYYY-MM-DD for Google Sheets.
                    clean_holidays_df['Date'] = clean_holidays_df['Date'].apply(
                        lambda s: scheduler.normalize_date_string(str(s).strip()) or ""
                    )
                    clean_holidays_df = clean_holidays_df[clean_holidays_df['Date'] != ""]
                    # Load fresh sheet data: detect concurrent changes, and use as accurate "before" state for history.
                    _, _, fresh_holidays_df = scheduler.load_tab_data()
                    if (st.session_state.get("holiday_snapshot_csv") and
                            fresh_holidays_df.to_csv(index=False) != st.session_state["holiday_snapshot_csv"]):
                        st.error("⚠️ The Holidays sheet was modified since you loaded this view. Click 🔄 Refresh to load the latest data, then re-apply your changes.")
                    else:
                        try:
                            scheduler.log_holiday_changes(fresh_holidays_df, clean_holidays_df)
                        except Exception as _e:
                            print(f"[WARN] Failed to log holiday changes to History: {_e}")
                        st.session_state["holiday_undo_snapshot"] = fresh_holidays_df[holiday_display_cols].to_csv(index=False)
                        scheduler.write_holidays_to_sheet(clean_holidays_df)
                        st.cache_data.clear()
                        st.session_state["holiday_data_hash"] = None
                        st.session_state["holiday_last_synced"] = None
                        st.session_state["holiday_external_change"] = False
                        st.session_state["holiday_snapshot_csv"] = None
                        st.success("Holidays saved to Google Sheets!")
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed to save holidays: {e}")
        if revert_h_col.button("↩️ Revert Changes", type="secondary", use_container_width=True, key="revert_holidays_btn"):
            del st.session_state[holiday_editor_key]
            st.rerun()

    if st.session_state.get("holiday_undo_snapshot"):
        st.info("Holidays saved. You can undo this save to restore the previous state.")
        if st.button("⏪ Undo Last Holiday Save", type="secondary", use_container_width=True, key="undo_holiday_save"):
            with st.spinner("Restoring previous holidays..."):
                try:
                    import io as _io
                    snap_df = pd.read_csv(_io.StringIO(st.session_state["holiday_undo_snapshot"])).fillna("")
                    scheduler.write_holidays_to_sheet(snap_df)
                    st.session_state["holiday_undo_snapshot"] = None
                    st.cache_data.clear()
                    st.session_state["holiday_data_hash"] = None
                    st.session_state["holiday_last_synced"] = None
                    st.session_state["holiday_external_change"] = False
                    st.session_state["holiday_snapshot_csv"] = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to undo holiday save: {e}")

with tab7:
    st.write("Every change made through this app or directly in Google Sheets.")
    try:
        history_df = history_df_preview
        if unseen_direct_edits > 0:
            if st.button(f"🔔 Mark {unseen_direct_edits} direct edit notification(s) as read", type="secondary"):
                scheduler.set_app_state("last_seen_direct_edit_timestamp", latest_direct_edit_ts)
                cached_fetch_history.clear()
                cached_get_app_state.clear()
                st.rerun()
        if history_df.empty:
            st.info("No history recorded yet. Changes will start appearing here once you run an action that edits the schedule.")
        else:
            filter_col1, filter_col2, filter_col3 = st.columns(3)
            with filter_col1:
                month_options = sorted(history_df["Timestamp"].str[:7].dropna().unique(), reverse=True)
                selected_hist_months = st.multiselect("Filter by month", month_options, default=[])
            with filter_col2:
                method_options = sorted(history_df["Method"].unique())
                # Empty selection = no filter applied = show everything. Selecting one or
                # more methods narrows the view to just those — a standard "select to
                # include" filter, not an allowlist you have to populate to see anything.
                selected_methods = st.multiselect("Filter by method", method_options, default=[])
            with filter_col3:
                employee_search = st.text_input("Filter by employee name", placeholder="e.g. Alice")

            filtered_df = history_df
            if selected_hist_months:
                filtered_df = filtered_df[filtered_df["Timestamp"].str[:7].isin(selected_hist_months)]
            if selected_methods:
                filtered_df = filtered_df[filtered_df["Method"].isin(selected_methods)]
            if employee_search.strip():
                needle = employee_search.strip().lower()
                name_match = (
                    filtered_df["Old Employee"].str.lower().str.contains(needle, na=False)
                    | filtered_df["New Employee"].str.lower().str.contains(needle, na=False)
                )
                filtered_df = filtered_df[name_match]

            st.caption(f"Showing {len(filtered_df)} of {len(history_df)} total change(s).")

            # Tint the "what happened" columns (Timestamp, Method) a different shade than
            # the "what changed" columns, so the two kinds of metadata read as distinct
            # groups at a glance instead of one undifferentiated row of columns.
            event_cols = ["Timestamp", "Method"]
            def shade_event_columns(col):
                return ["background-color: rgba(100,150,230,0.2)" if col.name in event_cols else "" for _ in col]

            st.caption("Select row(s) below to delete them from the history log.")
            # Key the widget on the filter state so changing a filter swaps in a fresh
            # widget instance instead of reusing one with a stale selection — Streamlit
            # tracks dataframe selection by row *position*, so if a filter change reshuffles
            # what's at each position while a selection is active, the old positions would
            # silently point at different rows, risking deleting the wrong entry.
            filter_signature = f"{'|'.join(sorted(selected_hist_months))}::{'|'.join(sorted(selected_methods))}::{employee_search.strip().lower()}"
            selection_event = st.dataframe(
                filtered_df.drop(columns="_sheet_row").style.apply(shade_event_columns, axis=0),
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key=f"history_table_{hash(filter_signature)}",
                # Force literal text rendering — otherwise Streamlit can auto-detect the
                # Timestamp column as datetime-like and re-format it with its own display
                # logic, silently overriding the 12-hour "...AM/PM" string we stored.
                column_config={"Timestamp": st.column_config.TextColumn("Timestamp")}
            )

            selected_positions = selection_event.selection.rows
            if selected_positions:
                sheet_rows_to_delete = filtered_df.iloc[selected_positions]["_sheet_row"].tolist()
                n_sel = len(selected_positions)
                entry_word = "entry" if n_sel == 1 else "entries"
                if st.session_state.get("pending_history_delete") == sheet_rows_to_delete:
                    st.warning(f"⚠️ Permanently delete {n_sel} history {entry_word}? This cannot be undone.")
                    _confirm_col, _cancel_col = st.columns(2)
                    if _confirm_col.button("✅ Yes, Delete", type="primary", use_container_width=True, key="confirm_hist_delete"):
                        scheduler.delete_history_rows(sheet_rows_to_delete)
                        st.session_state["pending_history_delete"] = None
                        cached_fetch_history.clear()
                        st.rerun()
                    if _cancel_col.button("Cancel", type="secondary", use_container_width=True, key="cancel_hist_delete"):
                        st.session_state["pending_history_delete"] = None
                        st.rerun()
                else:
                    if st.button(f"🗑️ Delete {n_sel} selected {entry_word}", type="secondary"):
                        st.session_state["pending_history_delete"] = sheet_rows_to_delete
                        st.rerun()
    except Exception as e:
        st.error(f"Failed to load change history: {e}")

with tab8:
    st.write("Cumulative hours scheduled per employee across all recorded months.")
    try:
        ytd_raw = cached_fetch_all_assignments()
        if ytd_raw.empty:
            st.info("No assignment data found.")
        else:
            ytd_filled = ytd_raw[~is_gap_mask(ytd_raw)].copy()
            if ytd_filled.empty:
                st.info("No assigned shifts found in the schedule history.")
            else:
                ytd_filled['Year-Month'] = ytd_filled['Date'].astype(str).str[:7]
                ytd_filled['Hours'] = ytd_filled.apply(
                    lambda r: scheduler.calculate_hours(str(r['Start Time']), str(r['End Time'])), axis=1
                )
                pivot = ytd_filled.pivot_table(
                    index='Assigned Employee', columns='Year-Month', values='Hours',
                    aggfunc='sum', fill_value=0
                )
                pivot.columns.name = None
                pivot['Total'] = pivot.sum(axis=1)
                pivot = pivot.sort_values('Total', ascending=False)
                formatted = pivot.map(lambda x: f"{x:.1f}" if x > 0 else "—")
                st.dataframe(formatted, use_container_width=True)
                # CSV export for YTD
                _ytd_csv = pivot.to_csv().encode("utf-8")
                st.download_button("⬇️ Export YTD to CSV", data=_ytd_csv, file_name="ytd_hours.csv", mime="text/csv")
    except Exception as e:
        st.error(f"Failed to load year-to-date data: {e}")

st.divider()
st.caption("Running locally. Data syncs directly with the live 'Child Watch Schedule' Google Sheet.")