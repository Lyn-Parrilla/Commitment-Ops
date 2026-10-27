"""Keep raw submission tokens out of application logs."""

from __future__ import annotations

import logging

from app.domain.submissions import redact_submission_token

_INSTALLED = False


class SubmissionTokenLogFilter(logging.Filter):
    """Redact submission credentials before a log record is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


def redact_log_record(record: logging.LogRecord) -> None:
    """Redact a record at creation time so child loggers cannot bypass it."""

    if isinstance(record.msg, str):
        record.msg = redact_submission_token(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(
            redact_submission_token(item) if isinstance(item, str) else item
            for item in record.args
        )
    elif isinstance(record.args, dict):
        record.args = {
            key: redact_submission_token(value) if isinstance(value, str) else value
            for key, value in record.args.items()
        }


def install_submission_log_redaction() -> None:
    """Install redaction for every logger, not only records emitted on root."""

    global _INSTALLED
    if _INSTALLED:
        return
    previous_factory = logging.getLogRecordFactory()

    def redacting_factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        redact_log_record(record)
        return record

    logging.setLogRecordFactory(redacting_factory)
    root = logging.getLogger()
    if not any(isinstance(item, SubmissionTokenLogFilter) for item in root.filters):
        root.addFilter(SubmissionTokenLogFilter())
    _INSTALLED = True
