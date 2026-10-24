"""Autodesk licences: each client's Autodesk seats and who holds them, beside their Microsoft 365 licences.

* ``api.py``: the client for Autodesk's Business Success Plan Reporting API (formerly the Premium Reporting API),
  the only Autodesk API that reports subscriptions, seat assignments and use.
* ``setup.py``: the app the MSP registered with Autodesk, the Autodesk sign-ins (personal access tokens) and
  which Autodesk team belongs to which client.
* ``exports.py``: reading Autodesk's export files into plain records.

The collector is ``collectors/autodesk.py``, the report ``reports/autodesk.py``, the pages ``web/autodesk.py``.
Nothing here changes anything in a client's Autodesk account or Microsoft 365.
"""
