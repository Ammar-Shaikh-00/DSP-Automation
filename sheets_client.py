from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_INPUT_IDS = {
    "account id",
    "accountid",
    "account_id",
    "input id",
    "inputid",
    "input_id",
}

SHEET_COLUMNS = 11
HEADER_ROW = 1
INPUT_COL = 2
CONTACT_COL = 4
STATUS_COL = 10
MAX_SHEET_BATCH_SIZE = 10
SHEET_FLUSH_SECONDS = 30.0


@dataclass(frozen=True)
class PendingRow:
    row_number: int
    account_id: str


@dataclass
class AccountWrite:
    row_number: int
    account_id: str
    result: dict[str, Any] | None = None
    error_message: str | None = None
    du_time: float = 0.0
    extra_rows: int = 0
    write_error: bool = False
    sheet_time: float = 0.0
    cached: bool = False
    classification: str = ""
    qbe_time: float = 0.0
    home_time: float = 0.0
    wireless_time: float = 0.0


class SheetsError(Exception):
    """Google Sheets access or update failed."""


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def _with_retry(func, *args, **kwargs):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return func(*args, **kwargs)
        except APIError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SheetsError("Google Sheets API request failed.") from exc
    raise SheetsError("Google Sheets API request failed.") from last_error


class GoogleSheetsClient:
    def __init__(
        self,
        credentials_path: str | Path,
        sheet_id: str,
        worksheet_name: str,
        sheet_batch_size: int = 6,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.sheet_id = sheet_id
        self.worksheet_name = worksheet_name
        self.worksheet: gspread.Worksheet | None = None
        self.total_sheets_time = 0.0
        self.last_read_time = 0.0
        self.last_write_time = 0.0
        self.sheet_cache: list[list[str]] = []
        self.sheet_batch_size = max(1, min(int(sheet_batch_size), MAX_SHEET_BATCH_SIZE))
        self.batch_requests = 0
        self.rows_written = 0
        self._pending: list[tuple[AccountWrite, list[dict[str, Any]]]] = []
        self._flushing = False
        self.last_flush_time = time.perf_counter()

    def connect(self) -> None:
        started = time.perf_counter()
        try:
            credentials = Credentials.from_service_account_file(
                str(self.credentials_path),
                scopes=SCOPES,
            )
            client = gspread.authorize(credentials)
            spreadsheet = client.open_by_key(self.sheet_id)
            self.worksheet = spreadsheet.worksheet(self.worksheet_name)
        except Exception as exc:
            raise SheetsError(
                "Unable to open the Google Sheet. "
                "Confirm the sheet ID, worksheet name, and that the "
                "service account has Editor access."
            ) from exc
        finally:
            self.total_sheets_time += time.perf_counter() - started

    def _ws(self) -> gspread.Worksheet:
        if self.worksheet is None:
            raise SheetsError("Google Sheets client is not connected.")
        return self.worksheet

    def _pad_row(self, row: list[str]) -> list[str]:
        padded = list(row) + [""] * (SHEET_COLUMNS - len(row))
        return padded[:SHEET_COLUMNS]

    def _set_cache(self, values: list[list[str]]) -> None:
        self.sheet_cache = [self._pad_row(row) for row in values]

    def test_access(self) -> dict[str, Any]:
        ws = self._ws()
        started = time.perf_counter()
        values = _with_retry(ws.get_all_values)
        self.last_read_time = time.perf_counter() - started
        self.total_sheets_time += self.last_read_time
        pending = self.read_pending_accounts(values=values)
        return {
            "title": ws.spreadsheet.title,
            "worksheet": ws.title,
            "rows": len(values),
            "pending": len(pending),
        }

    def read_pending_accounts(
        self,
        values: list[list[str]] | None = None,
        retry_errors: bool = False,
        allow_account_id: str | None = None,
    ) -> list[PendingRow]:
        ws = self._ws()
        if values is None:
            started = time.perf_counter()
            values = _with_retry(ws.get_all_values)
            self.last_read_time = time.perf_counter() - started
            self.total_sheets_time += self.last_read_time
            logger.info("Google Sheet read: %.2fs", self.last_read_time)

        self._set_cache(values)
        pending: list[PendingRow] = []
        allowed_id = _norm_id(allow_account_id) if allow_account_id else ""

        for index, row in enumerate(self.sheet_cache, start=1):
            if index == HEADER_ROW:
                continue

            account_id = _norm_id(row[INPUT_COL])
            status = _norm_id(row[STATUS_COL])

            if not account_id:
                continue

            if account_id.lower() in HEADER_INPUT_IDS:
                continue

            if not self._is_pending_status(
                status,
                account_id=account_id,
                retry_errors=retry_errors,
                allow_account_id=allowed_id,
            ):
                continue

            pending.append(
                PendingRow(
                    row_number=index,
                    account_id=account_id,
                )
            )

        return pending

    def _is_pending_status(
        self,
        status: str,
        account_id: str,
        retry_errors: bool,
        allow_account_id: str,
    ) -> bool:
        if not status:
            return True

        if status.upper() == "SUCCESS":
            return False

        is_error = status.upper().startswith("ERROR:")
        if not is_error:
            return False

        if retry_errors:
            return True

        if allow_account_id and account_id == allow_account_id:
            return True

        return False

    def unique_pending_accounts(self, pending: list[PendingRow]) -> list[PendingRow]:
        seen: set[str] = set()
        unique: list[PendingRow] = []
        for row in pending:
            if row.account_id in seen:
                logger.info(
                    "Skipping duplicate pending Input ID %s (row %s).",
                    row.account_id,
                    row.row_number,
                )
                continue
            seen.add(row.account_id)
            unique.append(row)

        unique.sort(
            key=lambda item: (
                self.max_row_for_account(item.account_id),
                item.row_number,
            ),
            reverse=True,
        )
        return unique

    def max_row_for_account(self, account_id: str) -> int:
        rows = self._find_account_rows(account_id)
        return max(rows) if rows else 0

    def _sheet_row(self, record: dict[str, Any]) -> list[str]:
        return [
            _cell(record.get("activation_date")),
            _cell(record.get("account_name")),
            _cell(record.get("input_id") or record.get("account_id")),
            _cell(record.get("resolved_account_id")),
            _cell(record.get("contact")),
            _cell(record.get("phone_nr")),
            _cell(record.get("promotion")),
            _cell(record.get("location")),
            _cell(record.get("nationality")),
            _cell(record.get("account_type")),
            _cell(record.get("status")),
        ]

    def _find_account_rows(self, account_id: str) -> list[int]:
        target = _norm_id(account_id)
        found: list[int] = []
        for index, row in enumerate(self.sheet_cache, start=1):
            if index == HEADER_ROW:
                continue
            value = _norm_id(row[INPUT_COL])
            if value.lower() in HEADER_INPUT_IDS:
                continue
            if value == target:
                found.append(index)
        return found

    def _contact_at(self, row_number: int) -> str:
        row = self.sheet_cache[row_number - 1]
        return _norm_id(row[CONTACT_COL])

    def _update_cache_row(self, row_number: int, record: dict[str, Any]) -> None:
        values = self._sheet_row(record)
        if 1 <= row_number <= len(self.sheet_cache):
            self.sheet_cache[row_number - 1] = values
        elif row_number == len(self.sheet_cache) + 1:
            self.sheet_cache.append(values)

    def _delete_rows(self, row_numbers: list[int]) -> None:
        unique_sorted = sorted({row for row in row_numbers if row >= 1}, reverse=True)
        if not unique_sorted:
            return
        self.flush_writes()

        ws = self._ws()
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "ROWS",
                        "startIndex": row - 1,
                        "endIndex": row,
                    }
                }
            }
            for row in unique_sorted
        ]
        self._spreadsheet_batch({"requests": requests})
        for row in unique_sorted:
            if 1 <= row <= len(self.sheet_cache):
                del self.sheet_cache[row - 1]

    def _insert_records_below(self, last_row: int, records: list[dict[str, Any]]) -> None:
        if not records:
            return

        self.flush_writes()
        ws = self._ws()
        count = len(records)
        all_rows = [self._sheet_row(record) for record in records]
        body = {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": last_row,
                            "endIndex": last_row + count,
                        },
                        "inheritFromBefore": True,
                    }
                },
                {
                    "updateCells": {
                        "start": {
                            "sheetId": ws.id,
                            "rowIndex": last_row,
                            "columnIndex": 0,
                        },
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": value}}
                                    for value in row
                                ]
                            }
                            for row in all_rows
                        ],
                        "fields": "userEnteredValue",
                    }
                },
            ]
        }
        self._spreadsheet_batch(body)
        self.rows_written += count
        for offset, row_values in enumerate(all_rows):
            self.sheet_cache.insert(last_row + offset, row_values)

    def _batch_values(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return

        ws = self._ws()

        def _do() -> Any:
            payload = [
                {"range": item["range"], "values": item["values"]}
                for item in updates
            ]
            return ws.batch_update(payload, value_input_option="RAW")

        started = time.perf_counter()
        try:
            _with_retry(_do)
            self.batch_requests += 1
            self.rows_written += len(updates)
            self.last_flush_time = time.perf_counter()
        finally:
            elapsed = time.perf_counter() - started
            self.last_write_time = elapsed
            self.total_sheets_time += elapsed

    def _spreadsheet_batch(self, body: dict[str, Any]) -> None:
        ws = self._ws()
        started = time.perf_counter()
        try:
            _with_retry(ws.spreadsheet.batch_update, body)
            self.batch_requests += 1
            self.last_flush_time = time.perf_counter()
        finally:
            elapsed = time.perf_counter() - started
            self.last_write_time = elapsed
            self.total_sheets_time += elapsed

    def _log_job_write(self, job: AccountWrite) -> None:
        if not job.error_message and not job.write_error:
            account_type = job.classification
            if not account_type and job.result:
                account_type = str(job.result.get("type") or "")
            if account_type == "FIBER":
                logger.info("Updated row %s successfully.", job.row_number)
            elif account_type == "WIRELESS":
                if job.extra_rows:
                    logger.info(
                        "Inserted %s additional row%s.",
                        job.extra_rows,
                        "" if job.extra_rows == 1 else "s",
                    )
                logger.info("Updated successfully.")
        logger.info("Sheet write: %.2fs", job.sheet_time)
        logger.info(
            "Total: %.2fs",
            job.qbe_time + job.home_time + job.wireless_time + job.sheet_time,
        )

    def has_pending(self, job: AccountWrite) -> bool:
        return any(item is job for item, _ in self._pending)

    def apply_write(self, job: AccountWrite) -> None:
        """Queue one account result and flush in small batches."""
        if not self.sheet_cache:
            values = _with_retry(self._ws().get_all_values)
            self._set_cache(values)

        job.write_error = False
        try:
            self._enqueue_job(job)
        except Exception:
            logger.warning(
                "Sheet write failed for row %s; retrying immediately.",
                job.row_number,
            )
            self._write_job_now(job)

    def apply_writes(self, jobs: list[AccountWrite]) -> None:
        for job in jobs:
            self.apply_write(job)
        self.flush_writes()

    def _enqueue_job(self, job: AccountWrite) -> None:
        if job.error_message:
            self._queue_error(job)
            return

        result = job.result or {}
        rows = result.get("rows") or []
        account_type = result.get("type")

        if account_type == "FIBER":
            if not rows:
                job.error_message = "ERROR: No services found"
                self._queue_error(job)
                return
            self._enqueue_fiber_job(job, rows[0])
            return

        if account_type == "WIRELESS":
            if not rows:
                job.error_message = "ERROR: No services found"
                self._queue_error(job)
                return
            self._enqueue_wireless_job(job, rows)
            return

        job.error_message = "ERROR: DU API failed"
        self._queue_error(job)

    def _queue_error(self, job: AccountWrite) -> None:
        if job.row_number == HEADER_ROW:
            raise SheetsError("Refusing to overwrite the header row.")
        if 1 <= job.row_number <= len(self.sheet_cache):
            row = self._pad_row(self.sheet_cache[job.row_number - 1])
            row[STATUS_COL] = job.error_message or ""
            self.sheet_cache[job.row_number - 1] = row
        self._queue_job_updates(job, [{
            "range": f"K{job.row_number}",
            "values": [[job.error_message or ""]],
        }])

    def _queue_job_updates(
        self,
        job: AccountWrite,
        updates: list[dict[str, Any]],
    ) -> None:
        if not updates:
            return
        self._pending.append((job, updates))
        self._flush_if_needed()

    def _flush_if_needed(self) -> None:
        if not self._pending:
            return
        if len(self._pending) >= self.sheet_batch_size:
            self.flush_writes()
            return
        if time.perf_counter() - self.last_flush_time >= SHEET_FLUSH_SECONDS:
            self.flush_writes()

    def flush_writes(self) -> None:
        if self._flushing or not self._pending:
            return

        pending = self._pending
        self._pending = []
        self._flushing = True
        all_updates = [update for _, updates in pending for update in updates]
        started = time.perf_counter()
        try:
            try:
                self._batch_values(all_updates)
                share = self.last_write_time / len(pending) if pending else 0.0
                for job, _ in pending:
                    job.sheet_time += share
                    self._log_job_write(job)
            except Exception:
                for job, updates in pending:
                    self._write_pending_job(job, updates)
        finally:
            self._flushing = False
            self.last_flush_time = time.perf_counter()
            elapsed = time.perf_counter() - started
            if elapsed:
                logger.info(
                    "Sheet batch: %s request, %s row%s.",
                    1,
                    len(all_updates),
                    "" if len(all_updates) == 1 else "s",
                )

    def _write_pending_job(
        self,
        job: AccountWrite,
        updates: list[dict[str, Any]],
    ) -> None:
        started = time.perf_counter()
        try:
            self._batch_values(updates)
            job.sheet_time += time.perf_counter() - started
            self._log_job_write(job)
        except Exception:
            job.sheet_time += time.perf_counter() - started
            self._write_job_now(job)

    def _write_job_now(self, job: AccountWrite) -> None:
        started = time.perf_counter()
        try:
            self._apply_write_once(job)
            job.sheet_time += time.perf_counter() - started
            self._log_job_write(job)
        except Exception:
            job.sheet_time += time.perf_counter() - started
            job.write_error = True
            logger.error("Could not write status for row %s.", job.row_number)
            self._log_job_write(job)

    def _enqueue_fiber_job(self, job: AccountWrite, record: dict[str, Any]) -> None:
        existing = self._find_account_rows(job.account_id)
        if not existing:
            existing = [job.row_number]
        existing = [row for row in existing if row != HEADER_ROW]
        if not existing:
            existing = [job.row_number]
        extras = [row for row in existing[1:] if row != HEADER_ROW]
        if extras:
            self.flush_writes()
            elapsed = self._apply_fiber_job(job, record)
            job.sheet_time += elapsed
            self._log_job_write(job)
            return
        keep_row = existing[0]
        if keep_row == HEADER_ROW:
            raise SheetsError("Refusing to overwrite the header row.")
        self._update_cache_row(keep_row, record)
        job.row_number = keep_row
        job.extra_rows = 0
        self._queue_job_updates(job, [{
            "range": f"A{keep_row}:K{keep_row}",
            "values": [self._sheet_row(record)],
        }])

    def _build_wireless_plan(
        self,
        job: AccountWrite,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = self._find_account_rows(job.account_id)
        if not existing:
            existing = [job.row_number]
        existing = [row for row in existing if row != HEADER_ROW]
        if not existing:
            existing = [job.row_number]

        seen_mobile: dict[str, int] = {}
        duplicate_rows: list[int] = []
        for row_number in existing:
            mobile = self._contact_at(row_number)
            if not mobile:
                continue
            if mobile in seen_mobile:
                duplicate_rows.append(row_number)
            else:
                seen_mobile[mobile] = row_number

        planned_existing = [
            row_number
            for row_number in existing
            if row_number not in duplicate_rows
        ]
        if not planned_existing:
            planned_existing = [job.row_number]

        if duplicate_rows:
            seen_mobile = {}
            for row_number in planned_existing:
                mobile = self._contact_at(row_number)
                if mobile and mobile not in seen_mobile:
                    seen_mobile[mobile] = row_number

        mobile_to_row = dict(seen_mobile)
        placeholders = [
            row_number
            for row_number in planned_existing
            if not self._contact_at(row_number)
        ]
        assigned: set[int] = set()
        updates: list[tuple[int, dict[str, Any]]] = []
        inserts: list[dict[str, Any]] = []

        for record in records:
            mobile = _norm_id(record.get("contact"))
            target = mobile_to_row.get(mobile)
            if target and target not in assigned:
                updates.append((target, record))
                assigned.add(target)
                continue
            if placeholders:
                target = placeholders.pop(0)
                updates.append((target, record))
                assigned.add(target)
                if mobile:
                    mobile_to_row[mobile] = target
                continue
            inserts.append(record)

        leftover_placeholders = [
            row_number
            for row_number in placeholders
            if row_number not in assigned
        ]
        return {
            "duplicate_rows": duplicate_rows,
            "leftover_placeholders": leftover_placeholders,
            "updates": updates,
            "inserts": inserts,
        }

    def _enqueue_wireless_job(
        self,
        job: AccountWrite,
        records: list[dict[str, Any]],
    ) -> None:
        plan = self._build_wireless_plan(job, records)
        if plan["duplicate_rows"] or plan["leftover_placeholders"] or plan["inserts"]:
            self.flush_writes()
            elapsed = self._apply_wireless_job(job, records)
            job.sheet_time += elapsed
            self._log_job_write(job)
            return
        updates = plan["updates"]
        for row_number, record in updates:
            self._update_cache_row(row_number, record)
        job.extra_rows = 0
        if updates:
            job.row_number = updates[0][0]
        self._queue_job_updates(job, [
            {
                "range": f"A{row_number}:K{row_number}",
                "values": [self._sheet_row(record)],
            }
            for row_number, record in updates
        ])

    def _apply_write_once(self, job: AccountWrite) -> None:
        if job.error_message:
            self.write_error(job.row_number, job.error_message)
            return

        result = job.result or {}
        rows = result.get("rows") or []
        account_type = result.get("type")

        if account_type == "FIBER":
            if not rows:
                job.error_message = "ERROR: No services found"
                self.write_error(job.row_number, job.error_message)
                return
            self._apply_fiber_job(job, rows[0])
            return

        if account_type == "WIRELESS":
            if not rows:
                job.error_message = "ERROR: No services found"
                self.write_error(job.row_number, job.error_message)
                return
            self._apply_wireless_job(job, rows)
            return

        job.error_message = "ERROR: DU API failed"
        self.write_error(job.row_number, job.error_message)

    def _shift_after_deletes(self, row_number: int, deleted: list[int]) -> int:
        return row_number - sum(1 for item in deleted if item < row_number)

    def _apply_fiber_job(self, job: AccountWrite, record: dict[str, Any]) -> float:
        started = time.perf_counter()
        existing = self._find_account_rows(job.account_id)
        if not existing:
            existing = [job.row_number]

        existing = [row for row in existing if row != HEADER_ROW]
        if not existing:
            existing = [job.row_number]
        keep_row = existing[0]
        extras = [row for row in existing[1:] if row != HEADER_ROW]
        if extras:
            self._delete_rows(extras)
            logger.info(
                "Removed %s duplicate Fiber row%s for %s.",
                len(extras),
                "" if len(extras) == 1 else "s",
                job.account_id,
            )
            keep_row = self._shift_after_deletes(keep_row, extras)

        if keep_row == HEADER_ROW:
            raise SheetsError("Refusing to overwrite the header row.")

        self._batch_values([{
            "range": f"A{keep_row}:K{keep_row}",
            "values": [self._sheet_row(record)],
        }])
        self._update_cache_row(keep_row, record)
        job.row_number = keep_row
        job.extra_rows = 0
        return time.perf_counter() - started

    def _apply_wireless_job(
        self,
        job: AccountWrite,
        records: list[dict[str, Any]],
    ) -> float:
        started = time.perf_counter()
        existing = self._find_account_rows(job.account_id)
        if not existing:
            existing = [job.row_number]
        existing = [row for row in existing if row != HEADER_ROW]
        if not existing:
            existing = [job.row_number]

        seen_mobile: dict[str, int] = {}
        duplicate_rows: list[int] = []
        for row_number in existing:
            mobile = self._contact_at(row_number)
            if not mobile:
                continue
            if mobile in seen_mobile:
                duplicate_rows.append(row_number)
            else:
                seen_mobile[mobile] = row_number

        if duplicate_rows:
            self._delete_rows(duplicate_rows)
            logger.info(
                "Removed %s duplicate Wireless row%s for %s.",
                len(duplicate_rows),
                "" if len(duplicate_rows) == 1 else "s",
                job.account_id,
            )
            existing = [
                self._shift_after_deletes(row_number, duplicate_rows)
                for row_number in existing
                if row_number not in duplicate_rows
            ]
            if not existing:
                existing = [self._shift_after_deletes(job.row_number, duplicate_rows)]
            seen_mobile = {}
            for row_number in existing:
                mobile = self._contact_at(row_number)
                if mobile and mobile not in seen_mobile:
                    seen_mobile[mobile] = row_number

        mobile_to_row = dict(seen_mobile)
        placeholders = [
            row_number
            for row_number in existing
            if not self._contact_at(row_number)
        ]
        assigned: set[int] = set()
        updates: list[tuple[int, dict[str, Any]]] = []
        inserts: list[dict[str, Any]] = []

        for record in records:
            mobile = _norm_id(record.get("contact"))
            target = mobile_to_row.get(mobile)
            if target and target not in assigned:
                updates.append((target, record))
                assigned.add(target)
                continue
            if placeholders:
                target = placeholders.pop(0)
                updates.append((target, record))
                assigned.add(target)
                if mobile:
                    mobile_to_row[mobile] = target
                continue
            inserts.append(record)

        leftover_placeholders = [
            row_number
            for row_number in placeholders
            if row_number not in assigned
        ]
        if leftover_placeholders:
            self._delete_rows(leftover_placeholders)
            logger.info(
                "Removed %s unused duplicate row%s for %s.",
                len(leftover_placeholders),
                "" if len(leftover_placeholders) == 1 else "s",
                job.account_id,
            )
            updates = [
                (self._shift_after_deletes(row_number, leftover_placeholders), record)
                for row_number, record in updates
                if row_number not in leftover_placeholders
            ]

        if updates:
            self._batch_values([
                {
                    "range": f"A{row_number}:K{row_number}",
                    "values": [self._sheet_row(record)],
                }
                for row_number, record in updates
            ])
            for row_number, record in updates:
                self._update_cache_row(row_number, record)

        if inserts:
            current_rows = self._find_account_rows(job.account_id)
            last_row = max(current_rows) if current_rows else job.row_number
            if updates:
                last_row = max(last_row, max(row_number for row_number, _ in updates))
            self._insert_records_below(last_row, inserts)

        job.extra_rows = len(inserts)
        if updates:
            job.row_number = updates[0][0]
        elif inserts:
            current_rows = self._find_account_rows(job.account_id)
            if current_rows:
                job.row_number = current_rows[0]
        return time.perf_counter() - started

    def write_fiber_row(self, row_number: int, record: dict[str, Any]) -> None:
        self._update_row(row_number, record)
        logger.info("Updated row %s successfully.", row_number)

    def write_wireless_rows(
        self,
        row_number: int,
        records: list[dict[str, Any]],
    ) -> int:
        job = AccountWrite(
            row_number=row_number,
            account_id=_norm_id(records[0].get("input_id") or records[0].get("account_id")),
        )
        self._apply_wireless_job(job, records)
        logger.info("Updated successfully.")
        return job.extra_rows

    def _update_row(self, row_number: int, record: dict[str, Any]) -> None:
        if row_number == HEADER_ROW:
            raise SheetsError("Refusing to overwrite the header row.")
        self._batch_values([{
            "range": f"A{row_number}:K{row_number}",
            "values": [self._sheet_row(record)],
        }])
        self._update_cache_row(row_number, record)

    def write_error(self, row_number: int, message: str) -> None:
        if row_number == HEADER_ROW:
            raise SheetsError("Refusing to overwrite the header row.")
        ws = self._ws()
        started = time.perf_counter()
        try:
            _with_retry(
                ws.update_acell,
                f"K{row_number}",
                message,
            )
            if 1 <= row_number <= len(self.sheet_cache):
                row = self._pad_row(self.sheet_cache[row_number - 1])
                row[STATUS_COL] = message
                self.sheet_cache[row_number - 1] = row
        finally:
            elapsed = time.perf_counter() - started
            self.last_write_time = elapsed
            self.total_sheets_time += elapsed
