"""Report builders. Detail reports from other modules register into ``data_reports.REPORTS`` here."""

from . import data_reports, sharepoint

data_reports.REPORTS.update(sharepoint.REPORTS)
