"""Microsoft's published list prices, built in for licences an MSP hasn't priced.

Microsoft has no public price feed: the price list partners use is in Partner Center, behind a partner
sign-in, and the prices on Microsoft's web pages differ by country. So the US list prices are kept here,
dated, and updated in a release when Microsoft changes them. They are only used when the MSP's currency is
US dollars; anywhere else, the MSP enters or imports its own (reseller) prices.

Prices are per user per month on an annual commitment, keyed by the SKU part number Microsoft Graph gives
(``subscribedSkus.skuPartNumber``). Microsoft adds 5% for an annual commitment paid monthly and 20% for a
monthly commitment; the MSP's margin on the Licence prices page covers that and any reseller's.

When Microsoft changes a price: update it here, move ``AS_OF``, and say so in the changelog.
"""

from __future__ import annotations

from datetime import date

CURRENCY = "USD"
AS_OF = date(2026, 7, 1)
# Where the figures come from, for the Licence prices page and the docs.
SOURCE = "Microsoft's US list prices, per user per month on an annual commitment"

PRICES: dict[str, float] = {
    # Microsoft's "Microsoft 365 pricing and packaging updates", effective 1 July 2026.
    "O365_BUSINESS_ESSENTIALS": 7.00,  # Microsoft 365 Business Basic
    "O365_BUSINESS_PREMIUM": 14.00,  # Microsoft 365 Business Standard
    "SPB": 22.00,  # Microsoft 365 Business Premium
    "O365_BUSINESS_PREMIUM_NOTEAMS": 10.79,  # Business Standard (no Teams)
    "Microsoft_365_ Business_ Premium_(no Teams)": 18.79,  # Business Premium (no Teams)
    "SMB_BUSINESS": 10.00,  # Microsoft 365 Apps for business
    "OFFICESUBSCRIPTION": 14.00,  # Microsoft 365 Apps for enterprise
    "STANDARDPACK": 10.00,  # Office 365 E1
    "ENTERPRISEPACK": 26.00,  # Office 365 E3
    "ENTERPRISEPREMIUM": 41.00,  # Office 365 E5
    "SPE_E3": 39.00,  # Microsoft 365 E3
    "SPE_E5": 60.00,  # Microsoft 365 E5
    "SPE_F1": 10.00,  # Microsoft 365 F3
    "M365_F1": 3.00,  # Microsoft 365 F1
    "M365_F1_COMM": 3.00,  # Microsoft 365 F1
    "AAD_PREMIUM": 7.00,  # Microsoft Entra ID P1
    "AAD_PREMIUM_P2": 10.00,  # Microsoft Entra ID P2
    "EMS": 12.00,  # Enterprise Mobility + Security E3
    "EMSPREMIUM": 18.00,  # Enterprise Mobility + Security E5
    # Microsoft's product pages; unchanged on 1 July 2026.
    "EXCHANGESTANDARD": 4.00,  # Exchange Online (Plan 1)
    "EXCHANGEENTERPRISE": 8.00,  # Exchange Online (Plan 2)
    "EXCHANGEARCHIVE_ADDON": 3.00,  # Exchange Online Archiving
    "INTUNE_A": 8.00,  # Microsoft Intune Plan 1
    "ATP_ENTERPRISE": 2.00,  # Microsoft Defender for Office 365 (Plan 1)
    "THREAT_INTELLIGENCE": 5.00,  # Microsoft Defender for Office 365 (Plan 2)
    "VISIOCLIENT": 15.00,  # Visio Plan 2
    "PROJECTPROFESSIONAL": 30.00,  # Project Plan 3
    "POWER_BI_PRO": 14.00,  # Power BI Pro
    "MCOEV": 10.00,  # Teams Phone Standard
    "Microsoft_365_Copilot": 30.00,  # Microsoft 365 Copilot
}


def price(part_number: str | None, currency: str) -> float | None:
    """Microsoft's list price for a licence, when it's built in for this currency."""
    if currency != CURRENCY:
        return None
    return PRICES.get(part_number or "")


def label() -> str:
    """How the list prices are named beside a figure: "Microsoft's list price (1 Jul 2026)"."""
    return f"Microsoft's list price ({AS_OF.day} {AS_OF:%b %Y})"
