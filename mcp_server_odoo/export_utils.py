"""Export utilities for controlled CSV export of Odoo records.

This module provides the core export pipeline: pre-flight count check,
batched search_read, streaming CSV write with atomic rename, and audit logging.
"""

import csv
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import OdooConfig
from .schemas import (
    ExportAccessDeniedError,
    ExportBlockedExceedsLimitError,
    ExportFileError,
    ExportSuccessResult,
)

logger = logging.getLogger("mcp_server_odoo.export")

# Module-level constants
MAX_PREVIEW_LINES = 10  # 1 header + 9 data rows
MAX_PATH_LENGTH = 250  # Windows safety margin
DEFAULT_FIELDS = ["id", "display_name"]  # smart default when user passes fields=None


class _ExportFileWriteError(Exception):
    """Internal exception for file write failures (not a schema)."""

    pass


def generate_export_filename(model: str) -> str:
    """Generate a unique filename: odoo_export_{model}_{timestamp}_{uuid_short}.csv.

    Args:
        model: Odoo model name (e.g. 'res.partner')

    Returns:
        str: Filename like 'odoo_export_res_partner_20260614T153022_a1b2c3d4.csv'
    """
    safe_model = model.replace(".", "_").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    uuid_short = uuid.uuid4().hex[:8]
    return f"odoo_export_{safe_model}_{timestamp}_{uuid_short}.csv"


def _hash_domain(domain: Any) -> str:
    """Return 16-char sha256 prefix of canonicalized domain.

    Never log the full domain — domains frequently contain PII.

    Args:
        domain: Odoo domain expression (list)

    Returns:
        str: 16-character hex prefix of the SHA-256 hash
    """
    try:
        canonical = json.dumps(domain, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(domain)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _log_export_audit(
    *,
    model: str,
    domain_hash: str,
    row_count: int,
    file_path: Path,
    duration_ms: int,
) -> None:
    """Write audit log entry via stdlib logging.

    Never includes full domain.

    Args:
        model: Odoo model name
        domain_hash: 16-char hash of the domain
        row_count: Number of rows exported
        file_path: Path to the export file
        duration_ms: Export duration in milliseconds
    """
    logger.info(
        "export | model=%s | rows=%d | file=%s | duration_ms=%d | domain_hash=%s",
        model,
        row_count,
        file_path,
        duration_ms,
        domain_hash,
    )


def _write_csv_atomic(
    target_path: Path,
    headers: list[str],
    record_iterator: Iterator[list[Any]],
) -> tuple[int, list[str]]:
    """Stream records to a temp file, then atomically rename to target_path.

    Returns (row_count, preview_lines).

    Args:
        target_path: Final destination path for the CSV file
        headers: Column headers for the CSV
        record_iterator: Iterator yielding rows as lists of values

    Returns:
        tuple[int, list[str]]: (row_count, preview_lines)

    Raises:
        ExportFileError: If file write fails
    """
    target_path = Path(target_path)

    # Path length validation (Windows safety)
    resolved = str(target_path.resolve())
    if len(resolved) > MAX_PATH_LENGTH:
        raise _ExportFileWriteError(
            f"Export path too long ({len(resolved)} chars, "
            f"max {MAX_PATH_LENGTH}): {target_path}. "
            f"Shorten ODOO_MCP_EXPORT_DIR or the model name."
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")

    row_count = 0
    preview_lines: list[str] = []

    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
            # Always write header
            writer.writerow(headers)
            if len(preview_lines) < MAX_PREVIEW_LINES:
                preview_lines.append(",".join(headers))

            for row in record_iterator:
                writer.writerow(row)
                row_count += 1
                if len(preview_lines) < MAX_PREVIEW_LINES:
                    preview_lines.append(",".join(str(v) if v is not None else "" for v in row))

        # Atomic rename
        os.replace(temp_path, target_path)
    except (OSError, IOError) as e:
        # Cleanup partial temp file
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise _ExportFileWriteError(f"Failed to write export file {target_path}: {e}") from e
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    return row_count, preview_lines


def execute_export(
    *,
    model: str,
    domain: Optional[list] = None,
    fields: Optional[list[str]] = None,
    config: OdooConfig,
    odoo_connection: Any,  # duck-typed OdooConnection
    access_controller: Any,  # duck-typed AccessController
) -> (
    ExportSuccessResult | ExportBlockedExceedsLimitError | ExportAccessDeniedError | ExportFileError
):
    """Execute a controlled CSV export.

    Pipeline:
    1. Access control check
    2. Pre-flight: search_count vs max_rows
    3. Generate filename
    4. Batch loop: search_read(offset, limit)
    5. Stream write to file (atomic)
    6. Audit log
    7. Return success result

    Args:
        model: Odoo model name
        domain: Odoo domain expression (default empty list)
        fields: List of field names to export (default ["id", "display_name"])
        config: OdooConfig with export settings
        odoo_connection: Duck-typed OdooConnection with execute_kw method
        access_controller: Duck-typed access controller with validate_model_access

    Returns:
        ExportSuccessResult on success, or an error variant
    """
    start_time = time.monotonic()
    domain = domain or []
    fields = fields or DEFAULT_FIELDS

    # 1. Access control
    try:
        access_controller.validate_model_access(model, "read")
    except Exception as e:
        logger.warning("export_access_denied | model=%s | reason=%s", model, type(e).__name__)
        return ExportAccessDeniedError(
            message=f"Model '{model}' is not accessible in current access control mode."
        )

    # 2. Pre-flight: search_count
    try:
        matched_count = odoo_connection.execute_kw(model, "search_count", [domain], {})
    except Exception as e:
        logger.warning("export_search_count_failed | model=%s | reason=%s", model, type(e).__name__)
        return ExportAccessDeniedError(
            message=f"Model '{model}' is not accessible or does not exist."
        )

    if matched_count > config.export_max_rows:
        return ExportBlockedExceedsLimitError(
            message=(
                f"Domain matches {matched_count} records, exceeds max_rows={config.export_max_rows}. "
                f"Refine the domain or use aggregate_records for server-side aggregation."
            ),
            matched_count=matched_count,
            max_rows_limit=config.export_max_rows,
            suggestion=(
                "Add filters to your domain or use aggregate_records() for summary statistics."
            ),
        )

    # 3. Generate filename
    filename = generate_export_filename(model)
    target_path = config.export_dir / filename

    # 4. Batch loop with streaming
    def record_iterator() -> Iterator[list]:
        offset = 0
        while offset < matched_count:
            batch = odoo_connection.execute_kw(
                model,
                "search_read",
                [domain],
                {
                    "fields": fields,
                    "offset": offset,
                    "limit": config.export_batch_size,
                },
            )
            if not batch:
                break
            for record in batch:
                # Extract field values in field order
                yield [record.get(f) for f in fields]
            offset += config.export_batch_size

    # 5. Write file
    try:
        row_count, preview_lines = _write_csv_atomic(
            target_path=target_path,
            headers=fields,
            record_iterator=record_iterator(),
        )
    except _ExportFileWriteError as e:
        return ExportFileError(message=str(e))

    # 6. Audit log
    duration_ms = int((time.monotonic() - start_time) * 1000)
    domain_hash = _hash_domain(domain)
    _log_export_audit(
        model=model,
        domain_hash=domain_hash,
        row_count=row_count,
        file_path=target_path,
        duration_ms=duration_ms,
    )

    # 7. Success result
    return ExportSuccessResult(
        file_path=str(target_path),
        file_size_bytes=target_path.stat().st_size,
        row_count=row_count,
        truncated=False,
        max_rows_limit=config.export_max_rows,
        preview=preview_lines[:MAX_PREVIEW_LINES],
        duration_ms=duration_ms,
        exported_at=datetime.now(timezone.utc).isoformat(),
    )
