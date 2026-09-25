"""Read-only Google Sheets source."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
COLUMN_LETTER = re.compile(r"^[A-Za-z]{1,2}$")


def _letter_to_index(letter: str) -> int:
    idx = 0
    for char in letter.upper():
        idx = idx * 26 + (ord(char) - ord("A") + 1)
    return idx - 1


class SheetError(RuntimeError):
    pass


class SheetSource:
    """Pulls rows out of one worksheet and maps them onto logical field names."""

    def __init__(self, cfg):
        self.cfg = cfg.sheets
        self._client = None

    def _client_or_connect(self):
        if self._client is None:
            creds = Credentials.from_service_account_file(
                self.cfg["service_account_file"], scopes=SCOPES
            )
            self._client = gspread.authorize(creds)
        return self._client

    def _resolve(self, spec: Any, headers: List[str]) -> int:
        if isinstance(spec, int):
            return spec - 1
        text = str(spec).strip()
        lowered = [h.strip().lower() for h in headers]
        if text.lower() in lowered:
            return lowered.index(text.lower())
        if COLUMN_LETTER.match(text):
            return _letter_to_index(text)
        raise SheetError(
            f"Column {spec!r} not found. Headers seen: {headers}. "
            "Use the exact header text, a column letter like 'C', or set sheets.header_row: 0."
        )

    @staticmethod
    def _explain(exc: "gspread.exceptions.APIError") -> str:
        """Turn the raw API error into something actionable in the log."""
        text = str(exc)
        if "has not been used in project" in text or "it is disabled" in text:
            return (
                "The Google Sheets API is not enabled for the service account's "
                "project. Enable it at "
                "console.cloud.google.com/apis/library/sheets.googleapis.com, "
                "select the project named in the error, then wait a minute. "
                f"Original error: {text}"
            )
        if "PERMISSION_DENIED" in text or "403" in text:
            return (
                "Google denied access to the sheet. Share it with the service "
                f"account's client_email (Viewer is enough). Original error: {text}"
            )
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            return f"Google Sheets rate limit hit; skipping this cycle. {text}"
        return f"Google Sheets API error: {text}"

    def fetch(self) -> List[Dict[str, str]]:
        """Blocking call — run it in a thread. Returns one dict per data row."""
        client = self._client_or_connect()
        # Everything below must raise SheetError, never something else: the caller
        # treats a SheetError as "skip this cycle, change nothing", which is the
        # rail that stops a broken share from looking like an emptied sheet.
        try:
            sheet = client.open_by_key(self.cfg["spreadsheet_id"])
            worksheet = sheet.worksheet(self.cfg["worksheet"])
            values = worksheet.get_all_values()
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetError(
                f"Worksheet {self.cfg['worksheet']!r} not found. Check sheets.worksheet "
                "against the tab name at the bottom of the spreadsheet."
            ) from exc
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetError(
                f"No spreadsheet with id {self.cfg['spreadsheet_id']!r}. Check "
                "sheets.spreadsheet_id, and that the sheet is shared with the "
                "service account."
            ) from exc
        except gspread.exceptions.APIError as exc:
            raise SheetError(self._explain(exc)) from exc
        except PermissionError as exc:
            # gspread turns some 403s into a bare PermissionError with no detail.
            raise SheetError(
                "Google refused the request (403). Either the Google Sheets API is "
                "not enabled for the service account's project, or the sheet is not "
                "shared with it. Enable the API at "
                "console.cloud.google.com/apis/library/sheets.googleapis.com and "
                "share the sheet with the client_email in the service account file."
            ) from exc
        except OSError as exc:  # DNS/TLS/connection trouble
            raise SheetError(f"Could not reach Google Sheets: {exc}") from exc

        if not values:
            return []

        header_row = int(self.cfg["header_row"] or 0)
        if header_row > 0:
            if len(values) < header_row:
                raise SheetError(f"Sheet has fewer than {header_row} rows; no header found.")
            headers = values[header_row - 1]
            data_rows = values[header_row:]
        else:
            headers = []
            data_rows = values

        columns = dict(self.cfg["columns"])
        key_column = self.cfg.get("key_column")
        if key_column:
            columns["_key"] = key_column

        index = {logical: self._resolve(spec, headers) for logical, spec in columns.items()}

        rows: List[Dict[str, str]] = []
        for offset, raw in enumerate(data_rows):
            row = {
                logical: (raw[i].strip() if i < len(raw) else "")
                for logical, i in index.items()
            }
            if not row.get("name"):
                continue
            row["_row_number"] = str(offset + header_row + 1)
            rows.append(row)

        log.debug("Fetched %d usable rows from the sheet", len(rows))
        return rows
