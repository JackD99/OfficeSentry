# Microsoft Azure

On Azure, run Office Sentry on a small Linux virtual machine. One paste at creation installs Docker,
downloads the newest release and starts it with automatic HTTPS; what runs on the VM is then exactly the
Docker setup in [Running it on your own server](../../DEPLOY.md), so everything there applies.

> **Tested:** the start-up file the VM runs ([`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml)):
> its install script, run against the published release, brings up a healthy portal, worker and Caddy.
> The Azure portal steps: not yet. If a screen differs, please
> [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

Already run AKS? Use the [Kubernetes](kubernetes.md) guide with the `managed-csi` (Azure Disk) storage class.
Azure Container Apps and App Service aren't supported: their only lasting storage is Azure Files, a network
share, and Office Sentry's database needs a disk ([why](README.md#not-supported-and-why)).

## 1. Get the start-up file ready

Copy [`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml) into a text editor and change
the two lines marked `yours`: the domain people will open the portal on (such as `sentry.yourmsp.com`) and
your timezone. Nothing else in it needs changing.

## 2. Create the virtual machine

In the [Azure portal](https://portal.azure.com): **Virtual machines → Create → Azure virtual machine**.

**Basics**

- **Resource group:** a new one, `officesentry`. **Virtual machine name:** `officesentry`.
- **Region:** where your clients' data should stay (such as UK South). **Availability options:** no
  infrastructure redundancy required.
- **Image:** Ubuntu Server 24.04 LTS (x64).
- **Size:** 2 vCPUs and 4 GiB, such as **Standard_B2als_v2**, for a few dozen clients. For hundreds, a size
  that doesn't burst, such as **Standard_D2als_v6**, keeps the nightly collection steady. (Arm64 sizes such as
  Standard_D2pls_v5 work too, with the Ubuntu Server 24.04 LTS Arm64 image.)
- **Authentication:** SSH public key, user name `azureuser`, and let Azure make a key pair (download it).
- **Public inbound ports:** None. You open exactly what's needed in step 3.

**Disks:** OS disk type Standard SSD or Premium SSD, and in **OS disk size** pick at least the size in
[How much disk](../../DEPLOY.md#how-much-disk) (64 GiB is enough for about 200 clients).

**Management:** turn **Auto-shutdown** off if it's on, and tick **Enable backup** (a daily policy in a new
Recovery Services vault): Azure then keeps copies of the whole VM.

**Advanced:** paste the file from step 1 into **Custom data**.

**Review + create → Create.** Installing Docker and Office Sentry takes a few minutes after the VM is
running.

## 3. Open the firewall

On the VM: **Networking → Network settings → Create port rule → Inbound port rule**, twice:

| Source | Port | Name | Why |
|---|---|---|---|
| IP Addresses: your office and home addresses (comma separated, such as `203.0.113.10`) | 443, TCP | `https-staff` | the portal |
| Any | 80, TCP | `http-certificate` | Let's Encrypt checks here before issuing the HTTPS certificate; Caddy only redirects it to 443 |

Port 443 is the allow-list: client users who sign in to the portal need their addresses there too. Leave
SSH (22) closed; step 5 uses **Run command** instead.

## 4. Point your domain at it

On the VM's **Overview**, note the **Public IP address**, then at your DNS provider add an **A** record for
your domain with it. (On Cloudflare, keep the proxy off: [Cloudflare](cloudflare.md).) Caddy gets the
certificate by itself within a minute of the record working.

No domain yet? The public IP's **Configuration** page can give it a DNS name label,
`<label>.<region>.cloudapp.azure.com`, which works as the domain (set it in the start-up file before
creating the VM).

## 5. Create the first admin

On the VM: **Operations → Run command → RunShellScript**, run:

```bash
cd /opt/officesentry && docker compose logs web | grep "No admin account yet"
```

Open the link it shows, choose a username and password, scan the QR code with an authenticator app and save
the recovery codes. Nothing shown? The install may still be running: `cloud-init status` says, and
`tail -50 /var/log/cloud-init-output.log` shows how far it got. Then carry on from step 6 of
[First start](../../DEPLOY.md#first-start).

## 6. Keep the key and the backups

- **The key.** Run `docker exec officesentry-web-1 cat /keys/secrets.json` with Run command and save the
  line in your password manager, or as a secret in an Azure Key Vault that only you can read
  ([Where the keys are](../../DEPLOY.md#where-the-keys-are)).
- **Nightly backups** run at 03:45 UTC, into `/data/backups` in the data volume, and *Settings → System
  status* alerts when one fails or is overdue. Azure Backup copies the whole VM daily, keys included, so keep
  access to the vault as tight as access to the VM. For copies with another provider, add
  [Off-site backups](../../DEPLOY.md#off-site-backups).

## Everyday

Run commands with **Run command** (or open port 22 to your own address and SSH in as `azureuser`), in
`/opt/officesentry`. Everything in [Running it on your own server](../../DEPLOY.md) works as written there:

- **Updating:** read the [release notes](https://github.com/JackD99/OfficeSentry/releases), set
  `OFFICESENTRY_VERSION` in `.env` to the new release, then `docker compose pull && docker compose up -d`. If
  the release changes `docker-compose.yml` or `Caddyfile`, run `sh /opt/officesentry/install.sh` first: it
  downloads the newest files and keeps your `.env`.
- **Settings:** in `/opt/officesentry/.env` ([When things run](../../DEPLOY.md#when-things-run)), then
  `docker compose up -d`.
- **Is it working:** `docker compose ps` shows `web` and `worker` as healthy; *Settings → System status*
  shows the rest.
