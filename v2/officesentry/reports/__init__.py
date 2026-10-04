"""Report builders. Detail reports from other modules register into ``data_reports.REPORTS`` here."""

from . import data_reports, sharepoint, sharepoint_storage, sign_in_reports, usage_reports

# SharePoint storage sits next to SharePoint sites in the report tabs.
for _key, _value in sharepoint.REPORTS.items():
    data_reports.REPORTS[_key] = _value
    if _key == "sharepoint":
        data_reports.REPORTS.update(sharepoint_storage.REPORTS)

# Usage and right-sizing sit next to Licence waste.
_items = list(data_reports.REPORTS.items())
_at = [k for k, _ in _items].index("licences") + 1
data_reports.REPORTS.clear()
data_reports.REPORTS.update(_items[:_at] + list(usage_reports.REPORTS.items()) + _items[_at:])

# Sign-in activity sits next to Inactive accounts.
_items = list(data_reports.REPORTS.items())
_at = [k for k, _ in _items].index("inactive") + 1
data_reports.REPORTS.clear()
data_reports.REPORTS.update(_items[:_at] + list(sign_in_reports.REPORTS.items()) + _items[_at:])
