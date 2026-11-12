"""Expected headers and keys copied from the assignment, independent of the writer."""

CSV_HEADERS = {
    "batch_summary.csv": "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed",
    "daily_overview.csv": "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads",
    "lead_report.csv": "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success",
    "rejections.csv": "workflow_id,batch_id,row_number,reason",
}
QUALITY_KEYS = (
    "lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
    "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped", "orphan_events",
    "unknown_disposition_events",
)
