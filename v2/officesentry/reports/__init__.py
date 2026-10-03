"""Report builders. Detail reports from other modules register into ``data_reports.REPORTS`` here."""

from . import data_reports, sharepoint, sharepoint_storage

# SharePoint storage sits next to SharePoint sites in the report tabs.
for _key, _value in sharepoint.REPORTS.items():
    data_reports.REPORTS[_key] = _value
    if _key == "sharepoint":
        data_reports.REPORTS.update(sharepoint_storage.REPORTS)
