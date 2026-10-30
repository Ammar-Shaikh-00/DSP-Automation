# DU DSP Automation

This project reads pending DU account identifiers from a Google Sheet and fills in the related DSP details automatically.

It is built for operations work where many Fiber, Wireless, and FIX IDs need to be checked in DU and recorded in one place. The sheet is the source of truth: the tool looks up each pending Input ID, resolves the correct account, extracts the required fields, and writes the result back to the same row.

## What it does

- Processes Fiber, Wireless, and FIX ID values from the sheet
- Reuses a single DU session and does not run concurrent DU logins
- Skips rows that already have SUCCESS or ERROR in Status
- Writes results using this column layout:

| Column | Field |
| --- | --- |
| A | Activation Date |
| B | Account Name |
| C | Input ID |
| D | Resolved Account ID |
| E | Contact / Mobile Number |
| F | Phone Nr |
| G | Promotion / Rate Plan |
| H | Location |
| I | Nationality |
| J | Account Type |
| K | Status |

Pending work is any row with a blank Status. Completed or failed rows stay as they are on later runs.

The expected outcome is a sheet that is updated in batches, with account details filled in and a clear SUCCESS or ERROR status for each processed ID.
