"""Report builders. Detail reports from other modules register into ``data_reports.REPORTS`` here."""

from . import data_reports, sharepoint, sharepoint_storage, subscriptions, usage_reports

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

# Licences and subscriptions comes first among the licence reports.
_items = list(data_reports.REPORTS.items())
_at = [k for k, _ in _items].index("licences")
data_reports.REPORTS.clear()
data_reports.REPORTS.update(_items[:_at] + list(subscriptions.REPORTS.items()) + _items[_at:])
