"""Device fixtures, with dates relative to now so ages stay stable."""

from datetime import UTC, datetime, timedelta


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


ENTRA_DEVICES = [
    {"id": "ed1", "deviceId": "d-ann", "displayName": "LAPTOP-ANN", "operatingSystem": "Windows",
     "operatingSystemVersion": "10.0.26200.6584", "trustType": "AzureAd", "isManaged": True, "isCompliant": True,
     "accountEnabled": True, "approximateLastSignInDateTime": days_ago(1), "registrationDateTime": days_ago(300)},
    {"id": "ed2", "deviceId": "d-bob", "displayName": "LAPTOP-BOB", "operatingSystem": "Windows",
     "operatingSystemVersion": "10.0.19045.5011", "trustType": "AzureAd", "isManaged": True, "isCompliant": False,
     "accountEnabled": True, "approximateLastSignInDateTime": days_ago(45), "registrationDateTime": days_ago(900)},
    {"id": "ed3", "deviceId": "d-old", "displayName": "OLD-PC", "operatingSystem": "Windows",
     "operatingSystemVersion": "10.0.19045.3000", "trustType": "AzureAd", "isManaged": False,
     "accountEnabled": True, "approximateLastSignInDateTime": days_ago(200), "registrationDateTime": days_ago(1500)},
    {"id": "ed4", "deviceId": "d-desk", "displayName": "DESK-RECEPTION", "operatingSystem": "Windows",
     "operatingSystemVersion": "10.0.26200.100", "trustType": "ServerAd", "isManaged": False,
     "accountEnabled": True, "approximateLastSignInDateTime": days_ago(2), "registrationDateTime": days_ago(400)},
    {"id": "ed5", "deviceId": "d-phone", "displayName": "Ann's iPhone", "operatingSystem": "iOS",
     "operatingSystemVersion": "18.6", "trustType": "Workplace", "isManaged": True,
     "accountEnabled": True, "approximateLastSignInDateTime": days_ago(0), "registrationDateTime": days_ago(100)},
]

MANAGED_DEVICES = [
    {"id": "md1", "deviceName": "LAPTOP-ANN", "operatingSystem": "Windows", "osVersion": "10.0.26200.6584",
     "complianceState": "compliant", "isEncrypted": True, "lastSyncDateTime": days_ago(1),
     "enrolledDateTime": days_ago(300), "userPrincipalName": "ann@x.com", "managedDeviceOwnerType": "company",
     "manufacturer": "Dell", "model": "Latitude 7450", "jailBroken": "Unknown", "azureADDeviceId": "d-ann"},
    {"id": "md2", "deviceName": "LAPTOP-BOB", "operatingSystem": "Windows", "osVersion": "10.0.19045.5011",
     "complianceState": "noncompliant", "isEncrypted": False, "lastSyncDateTime": days_ago(45),
     "enrolledDateTime": days_ago(900), "userPrincipalName": "bob@x.com", "managedDeviceOwnerType": "company",
     "manufacturer": "HP", "model": "EliteBook 840 G5", "jailBroken": "Unknown", "azureADDeviceId": "d-bob"},
    {"id": "md3", "deviceName": "Ann's iPhone", "operatingSystem": "iOS", "osVersion": "18.6",
     "complianceState": "compliant", "isEncrypted": True, "lastSyncDateTime": days_ago(0),
     "enrolledDateTime": days_ago(100), "userPrincipalName": "ann@x.com", "managedDeviceOwnerType": "personal",
     "manufacturer": "Apple", "model": "iPhone 15", "jailBroken": "False", "azureADDeviceId": "d-phone"},
]
