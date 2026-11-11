"""Normalize database timestamps; legacy SQLite timestamps are UTC."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
SQL_NOW = "(strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00')"


def timestamp(value):
    if value is None or value == '':
        return value
    text = str(value)
    if len(text) == 10 and text[4] == '-' and text[7] == '-':
        return text  # A date has no known time; do not invent one.
    if text.isdigit():
        number = int(text)
        return datetime.fromtimestamp(number / 1000 if number > 10**11 else number, timezone.utc).astimezone(BEIJING).isoformat(timespec='seconds')
    parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING).isoformat(timespec='seconds')
