"""Keep tenant-supplied text from becoming live formulas in XLSX exports.

Collected values (display names, rule names, app names) come from client
tenants. openpyxl stores any text starting with ``=`` as a formula, so a user
named ``=HYPERLINK(...)`` would run when the MSP opens the download.
"""

from __future__ import annotations


def no_formulas(wb) -> None:
    """Store every cell openpyxl would treat as a formula as plain text."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
