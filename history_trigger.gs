/**
 * Logs direct edits to the "Assigned Employee" column of the Assignments sheet into the
 * History sheet, in real time, with the actual editor's email and timestamp.
 *
 * Why this exists: the Python app can only see changes when it's running and someone
 * interacts with it. Edits made directly in this Google Sheet (typing over a cell, pasting,
 * deleting a name) happen completely outside the app's control. Apps Script's onEdit trigger
 * is the only way to catch those the instant they happen, with the real editor identity.
 *
 * Important: onEdit (simple trigger) only fires for edits made by a human directly in the
 * Sheets UI — it does NOT fire for edits made via the Sheets API (which is how the Python
 * app writes). So this script will never see — and never duplicate-log — anything the app
 * itself does; it only ever sees genuine direct edits.
 *
 * SETUP (one-time):
 *   1. Open the "Child Watch Schedule" Google Sheet.
 *   2. Go to Extensions > Apps Script.
 *   3. Delete any starter code in the editor, then paste in this entire file.
 *   4. Save (the floppy disk icon, or Cmd/Ctrl+S). Name the project anything you like.
 *   5. Click "Run" once on the onEdit function to trigger Google's permission prompt, and
 *      authorize it (this script only reads/writes within this one spreadsheet).
 *   6. That's it — no manual trigger setup needed. onEdit is a "simple trigger" that Google
 *      wires up automatically just by the function being named onEdit in this file.
 *
 * To verify it's working: go back to the spreadsheet, manually type a different name into
 * any "Assigned Employee" cell on the Assignments sheet, then check the History tab — a new
 * row should appear within a few seconds.
 */

const ASSIGNMENTS_SHEET_NAME = "Assignments";
const HISTORY_SHEET_NAME = "History";
const ASSIGNED_EMPLOYEE_COLUMN = 6; // F
const ISSUES_COLUMN = 7;            // G
const HISTORY_HEADERS = [
  "Timestamp", "Method", "Date", "Day of Week", "Start Time",
  "Old Employee", "New Employee", "Edited By", "Details"
];

function onEdit(e) {
  try {
    const range = e.range;
    const sheet = range.getSheet();

    if (sheet.getName() !== ASSIGNMENTS_SHEET_NAME) return;
    if (range.getRow() === 1) return; // header row
    if (range.getColumn() !== ASSIGNED_EMPLOYEE_COLUMN) return;
    // Ignore multi-cell pastes/fills that aren't a single-cell edit — oldValue/value aren't
    // meaningful for a range, and a bulk paste directly in the sheet is rare enough that it's
    // not worth guessing at per-cell diffs here.
    if (range.getNumRows() !== 1 || range.getNumColumns() !== 1) return;

    const row = range.getRow();
    const oldValue = (e.oldValue || "").toString().trim();
    const newValue = (e.value || "").toString().trim();
    if (oldValue === newValue) return;

    const rowValues = sheet.getRange(row, 1, 1, 7).getValues()[0];
    const date = rowValues[0];
    const dayOfWeek = rowValues[1];
    const startTime = rowValues[3];

    // Keep the Issues column consistent with the convention the app itself uses:
    // blank Assigned Employee always means "GAP".
    sheet.getRange(row, ISSUES_COLUMN).setValue(newValue === "" ? "GAP" : "");

    const editorEmail = Session.getActiveUser().getEmail() || "Unknown";
    const historySheet = getOrCreateHistorySheet(e.source);
    historySheet.appendRow([
      formatTimestamp(new Date()),
      "Direct Sheet Edit",
      date,
      dayOfWeek,
      startTime,
      oldValue,
      newValue,
      editorEmail,
      ""
    ]);
  } catch (err) {
    // Never let a logging failure block the user's actual edit.
    console.error("History logging failed: " + err);
  }
}

function getOrCreateHistorySheet(spreadsheet) {
  let sheet = spreadsheet.getSheetByName(HISTORY_SHEET_NAME);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(HISTORY_SHEET_NAME);
    sheet.appendRow(HISTORY_HEADERS);
  }
  // Force the Timestamp column to plain-text formatting. Without this, Sheets auto-detects
  // the "yyyy-MM-dd hh:mm:ss a" string as an actual date/time value and re-displays it using
  // the column's own number format (typically 24-hour), silently overriding the AM/PM string
  // we deliberately constructed — same issue the Python side hit with USER_ENTERED writes.
  sheet.getRange("A:A").setNumberFormat("@");
  return sheet;
}

function formatTimestamp(date) {
  const tz = Session.getScriptTimeZone();
  return Utilities.formatDate(date, tz, "yyyy-MM-dd hh:mm:ss a");
}
