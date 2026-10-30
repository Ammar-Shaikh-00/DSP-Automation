from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from config import ConfigError, load_config
from du_client import (
    DUAPIError,
    DUAccountNotFoundError,
    DUAuthenticationError,
    DUClient,
    DUError,
    DUInvalidInputError,
    DUNoServicesError,
)
from sheets_client import AccountWrite, GoogleSheetsClient, PendingRow, SheetsError

logger = logging.getLogger("du_dsp")


def configure_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DU DSP Automation — read Input IDs from Google Sheets and update DU account data.",
    )
    parser.add_argument(
        "--test-sheets",
        action="store_true",
        help="Connect to Google Sheets and print basic access details, then exit.",
    )
    parser.add_argument(
        "--account",
        metavar="INPUT_ID",
        help="Process only this Input ID (Account ID or FIX ID) from the worksheet.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Also process rows whose Status starts with ERROR:. SUCCESS rows are still skipped.",
    )
    return parser.parse_args()


def sheet_error_message(exc: Exception) -> str:
    if isinstance(exc, DUInvalidInputError):
        return "ERROR: Invalid input"
    if isinstance(exc, DUAccountNotFoundError):
        return "ERROR: Account not found"
    if isinstance(exc, DUNoServicesError):
        return "ERROR: No services found"
    if isinstance(exc, DUAuthenticationError):
        return "ERROR: Authentication failed"
    return "ERROR: DU API failed"


def classify_result(result: dict[str, Any] | None, exc: Exception | None = None) -> str:
    if isinstance(exc, DUAccountNotFoundError):
        return "ACCOUNT_NOT_FOUND"
    if isinstance(exc, DUNoServicesError):
        return "NO_SERVICES"
    if result and result.get("type") == "FIBER":
        return "FIBER"
    if result and result.get("type") == "WIRELESS":
        return "WIRELESS"
    return ""


def print_summary(
    processed: int,
    successful: int,
    failed: int,
    fiber: int,
    wireless: int,
    additional_wireless_rows: int,
    du_api_time: float = 0.0,
    sheets_time: float = 0.0,
    total_time: float = 0.0,
    qbe_total: float = 0.0,
    qbe_count: int = 0,
    home_total: float = 0.0,
    home_count: int = 0,
    wireless_total: float = 0.0,
    wireless_count: int = 0,
    login_time: float = 0.0,
    cache_hits: int = 0,
    api_calls_avoided: int = 0,
    sheet_batch_requests: int = 0,
    sheet_rows_written: int = 0,
) -> None:
    average = (total_time / processed) if processed else 0.0
    qbe_avg = (qbe_total / qbe_count) if qbe_count else 0.0
    home_avg = (home_total / home_count) if home_count else 0.0
    wireless_avg = (wireless_total / wireless_count) if wireless_count else 0.0
    print()
    print("====================================")
    print("DU DSP AUTOMATION COMPLETE")
    print("====================================")
    print(f"Processed: {processed}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    print(f"Fiber: {fiber}")
    print(f"Wireless: {wireless}")
    print(f"Additional wireless rows: {additional_wireless_rows}")
    print()
    print(f"Session login/validation: {login_time:.2f}s")
    print(f"getQBEInfo: {qbe_total:.2f}s total / {qbe_avg:.2f}s avg")
    print(f"Home Services: {home_total:.2f}s total / {home_avg:.2f}s avg")
    print(f"Account Services: {wireless_total:.2f}s total / {wireless_avg:.2f}s avg")
    print(f"Google Sheets batch requests: {sheet_batch_requests}")
    print(f"Rows written: {sheet_rows_written}")
    print(f"Google Sheets total: {sheets_time:.2f}s")
    print(f"Cache hits: {cache_hits}")
    print(f"DU API calls avoided: {api_calls_avoided}")
    print(f"Overall total: {total_time:.2f}s")
    print()
    print(f"DU API time: {du_api_time:.2f}s")
    print(f"Average per account: {average:.2f}s")
    print("====================================")


def account_du_time(du: DUClient) -> float:
    return du.last_qbe_time + du.last_home_time + du.last_wireless_time


def attach_du_timings(job: AccountWrite, du: DUClient) -> None:
    job.qbe_time = du.last_qbe_time
    job.home_time = du.last_home_time
    job.wireless_time = du.last_wireless_time
    job.du_time = account_du_time(du)


def log_account_du_timings(job: AccountWrite) -> None:
    logger.info("Account: %s", job.account_id)
    if job.classification:
        logger.info("%s", job.classification)
    logger.info("getQBEInfo: %.2fs", job.qbe_time)
    logger.info("Home Services: %.2fs", job.home_time)
    logger.info("Account Services: %.2fs", job.wireless_time)


def process_and_write_accounts(
    du: DUClient,
    sheets: GoogleSheetsClient,
    pending_rows: list[PendingRow],
) -> list[AccountWrite]:
    seen: set[str] = set()
    jobs: list[AccountWrite] = []
    total = len(pending_rows)

    for index, pending in enumerate(pending_rows, start=1):
        percent = (index / total * 100) if total else 0.0
        logger.info("[%s/%s] Processing %s", index, total, pending.account_id)

        if pending.account_id in seen:
            logger.info("Skipping duplicate pending Input ID %s.", pending.account_id)
            logger.info("Progress: %s/%s (%.1f%%)", index, total, percent)
            continue
        seen.add(pending.account_id)

        try:
            result = du.process_account(pending.account_id)
            if result.get("type") == "FIBER" and not (result.get("rows") or []):
                raise DUNoServicesError("No services found")
            if result.get("type") not in ("FIBER", "WIRELESS"):
                raise DUAPIError("Unknown account type")

            job = AccountWrite(
                row_number=pending.row_number,
                account_id=pending.account_id,
                result=result,
                extra_rows=0,
                classification=classify_result(result),
            )
            attach_du_timings(job, du)
            log_account_du_timings(job)
        except Exception as exc:
            message = sheet_error_message(exc)
            logger.error("%s — %s", pending.account_id, message)
            job = AccountWrite(
                row_number=pending.row_number,
                account_id=pending.account_id,
                error_message=message,
                classification=classify_result(None, exc),
            )
            attach_du_timings(job, du)
            log_account_du_timings(job)

        sheets.apply_write(job)
        if not sheets.has_pending(job):
            job.result = None

        if job.write_error:
            suffix = "sheet write failed"
        elif sheets.has_pending(job):
            suffix = "sheet queued"
        else:
            suffix = "sheet updated"
        if job.error_message:
            outcome = f"{job.error_message} - {suffix}"
        elif job.classification:
            outcome = f"{job.classification} - {suffix}"
        else:
            outcome = suffix
        logger.info("[%s/%s] %s", index, total, outcome)
        logger.info("Progress: %s/%s (%.1f%%)", index, total, percent)
        jobs.append(job)

    sheets.flush_writes()
    for job in jobs:
        job.result = None
    return jobs


def summarize_jobs(jobs: list[AccountWrite]) -> tuple[int, int, int, int, int, int]:
    processed = 0
    successful = 0
    failed = 0
    fiber = 0
    wireless = 0
    additional_wireless_rows = 0

    for job in jobs:
        processed += 1
        if job.error_message or job.write_error:
            failed += 1
            continue

        successful += 1
        if job.classification == "FIBER":
            fiber += 1
        else:
            wireless += 1
            additional_wireless_rows += job.extra_rows

    return (
        processed,
        successful,
        failed,
        fiber,
        wireless,
        additional_wireless_rows,
    )


def main() -> int:
    configure_logging()
    args = parse_args()
    run_started = time.perf_counter()
    du_api_time = 0.0
    sheets_time = 0.0
    login_time = 0.0
    cache_hits = 0
    api_calls_avoided = 0
    sheet_batch_requests = 0
    sheet_rows_written = 0

    def timing_aggregates(jobs: list[AccountWrite]) -> dict[str, float | int]:
        qbe_total = 0.0
        qbe_count = 0
        home_total = 0.0
        home_count = 0
        wireless_total = 0.0
        wireless_count = 0
        for job in jobs:
            if job.qbe_time:
                qbe_total += job.qbe_time
                qbe_count += 1
            if job.home_time:
                home_total += job.home_time
                home_count += 1
            if job.wireless_time:
                wireless_total += job.wireless_time
                wireless_count += 1
        return {
            "qbe_total": qbe_total,
            "qbe_count": qbe_count,
            "home_total": home_total,
            "home_count": home_count,
            "wireless_total": wireless_total,
            "wireless_count": wireless_count,
        }

    def finish(
        code: int,
        processed: int = 0,
        successful: int = 0,
        failed: int = 0,
        fiber: int = 0,
        wireless: int = 0,
        additional_wireless_rows: int = 0,
        jobs: list[AccountWrite] | None = None,
    ) -> int:
        aggregates = timing_aggregates(jobs or [])
        print_summary(
            processed,
            successful,
            failed,
            fiber,
            wireless,
            additional_wireless_rows,
            du_api_time=du_api_time,
            sheets_time=sheets_time,
            total_time=time.perf_counter() - run_started,
            login_time=login_time,
            cache_hits=cache_hits,
            api_calls_avoided=api_calls_avoided,
            sheet_batch_requests=sheet_batch_requests,
            sheet_rows_written=sheet_rows_written,
            **aggregates,
        )
        return code

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    sheets = GoogleSheetsClient(
        credentials_path=config.google_credentials,
        sheet_id=config.google_sheet_id,
        worksheet_name=config.google_worksheet,
        sheet_batch_size=config.sheet_batch_size,
    )

    try:
        sheets.connect()
    except SheetsError as exc:
        logger.error("%s", exc)
        return 1

    if args.test_sheets:
        try:
            info = sheets.test_access()
        except SheetsError as exc:
            logger.error("%s", exc)
            return 1

        print("Google Sheets access OK.")
        print(f"Spreadsheet: {info['title']}")
        print(f"Worksheet: {info['worksheet']}")
        print(f"Rows: {info['rows']}")
        print(f"Pending accounts: {info['pending']}")
        return 0

    try:
        pending_rows = sheets.read_pending_accounts(
            retry_errors=args.retry_errors,
            allow_account_id=(str(args.account).strip() if args.account else None),
        )
    except SheetsError as exc:
        logger.error("%s", exc)
        sheets_time = sheets.total_sheets_time
        return finish(1)

    sheets_time = sheets.total_sheets_time

    if args.account:
        target = str(args.account).strip()
        pending_rows = [
            row for row in pending_rows if row.account_id == target
        ]
        if not pending_rows:
            logger.error(
                "No pending row found for Input ID %s. "
                "Confirm column C is set and column K is blank "
                "(or ERROR if using --retry-errors / --account).",
                target,
            )
            return finish(1)

    pending_rows = sheets.unique_pending_accounts(pending_rows)

    if not pending_rows:
        logger.info("No pending accounts to process.")
        return finish(0)

    logger.info("Pending accounts: %s", len(pending_rows))
    logger.info("DU workers: %s (single session)", config.du_workers)
    logger.info("Sheet batch size: %s", config.sheet_batch_size)

    du = DUClient(
        username=config.du_username,
        password=config.du_password,
        cookie_file=config.cookie_file,
        work_dir=config.root_dir,
        debug=config.debug_du,
    )

    try:
        du.ensure_session()
    except (DUAuthenticationError, DUError) as exc:
        du_api_time = du.api_time
        logger.error("ERROR: Authentication failed")
        logger.error("%s", exc)
        return finish(1)

    logger.info("Session login/validation: %.2fs", du.last_login_time)
    login_time = du.last_login_time
    du_api_time = du.api_time

    jobs = process_and_write_accounts(du, sheets, pending_rows)
    du_api_time = du.api_time
    sheets_time = sheets.total_sheets_time
    cache_hits = du.cache_hits
    api_calls_avoided = du.api_calls_avoided
    sheet_batch_requests = sheets.batch_requests
    sheet_rows_written = sheets.rows_written

    (
        processed,
        successful,
        failed,
        fiber,
        wireless,
        additional_wireless_rows,
    ) = summarize_jobs(jobs)

    return finish(
        0,
        processed,
        successful,
        failed,
        fiber,
        wireless,
        additional_wireless_rows,
        jobs=jobs,
    )


if __name__ == "__main__":
    raise SystemExit(main())
