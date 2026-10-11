# Other clouds and VPS hosts

Any host that rents Ubuntu servers runs Office Sentry the same way: create a server with 2 vCPUs and 4 GB of
memory, paste one start-up file, open two ports, point your domain at it. What runs on the server is then
exactly the Docker setup in [Running it on your own server](../../DEPLOY.md).

> **Tested:** the start-up file ([`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml)):
> its install script, run against the published release, brings up a healthy portal, worker and Caddy, and
> its nightly backup command works. Each host's own screens: not yet. If a step doesn't match what you see,
> please [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

## The recipe

1. **The start-up file.** Copy [`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml) and
   change the two lines marked `yours`: your domain (such as `sentry.yourmsp.com`) and your timezone.
2. **The server.** Ubuntu 24.04, at least 2 vCPUs and 4 GB of memory, a disk from
   [How much disk](../../DEPLOY.md#how-much-disk), in the region where your clients' data should stay. Paste the
   file where the host takes *user data* or *cloud-init* (the table below says where), and add your SSH key.
3. **The firewall.** Inbound TCP 443 from your office and home addresses only (client users who sign in need
   theirs there too), TCP 80 from anywhere (Let's Encrypt checks there before issuing the certificate; Caddy
   only redirects it to 443), and SSH from your own address, or none if the host has a web console.
4. **Your domain.** An **A** record for it with the server's IPv4 address. (On Cloudflare, keep the proxy
   off: [Cloudflare](cloudflare.md).)
5. **The first admin.** A few minutes after the server starts, on its console or over SSH:

   ```bash
   cd /opt/officesentry && sudo docker compose logs web | grep "No admin account yet"
   ```

   Nothing shown? `cloud-init status` says whether the install is still running. Open the link, create the
   admin with two-step sign-in, and carry on from step 6 of [First start](../../DEPLOY.md#first-start).
6. **The key and backups.** Save the line `sudo docker exec officesentry-web-1 cat /keys/secrets.json` prints
   in your password manager ([Where the keys are](../../DEPLOY.md#where-the-keys-are)). A database backup runs
   every night at 03:45 UTC; turn on the host's own backups or snapshots too, and add
   [Off-site backups](../../DEPLOY.md#off-site-backups) with another provider.

Updating and every other command are as in [Running it on your own server](../../DEPLOY.md), in
`/opt/officesentry`. If a release changes `docker-compose.yml` or `Caddyfile`, run
`sudo sh /opt/officesentry/install.sh`: it downloads the newest files and keeps your `.env`.

## Where each host keeps these

| Host | Server | 2 vCPUs, 4 GB | Where the start-up file goes | Firewall | Backups |
|---|---|---|---|---|---|
| DigitalOcean | Droplet | Basic, 2 vCPUs / 4 GB | **Advanced options → Add initialization scripts** | **Networking → Firewalls**, applied to the Droplet | **Backups** (daily or weekly) on the Droplet |
| Google Cloud | Compute Engine VM | `e2-medium` | **Advanced options → Management → Metadata**, key `user-data` | **VPC network → Firewall**, a rule for the VM's network tag | **Snapshot schedule** on the boot disk |
| Linode (Akamai) | Linode | Shared CPU, Linode 4 GB | **Add User Data** (regions with the Metadata service) | **Cloud Firewalls** | **Backups** on the Linode |
| Vultr | Cloud Compute | 2 vCPUs / 4 GB | **Additional Features → Cloud-Init User-Data** | **Network → Firewall**, a group linked to the instance | **Automatic Backups** |
| Oracle Cloud | Compute instance | `VM.Standard.A1.Flex` (Ampere, arm64), 2 OCPUs / 12 GB | **Show advanced options → Management → Cloud-init script** | the subnet's **Security List** or a **Network Security Group** | **Boot volume backup policy** |
| Hetzner | Cloud server | shared vCPU, 2 vCPUs / 4 GB | **Cloud config** | **Firewalls** | **Backups** |

### Google Cloud

On Compute Engine, the VM's external address changes on restart unless you reserve it: **VPC network → IP
addresses → Reserve external static address**, attached to the VM. Firewall rules apply to VMs through
network tags: give the VM a tag such as `officesentry` and create the two inbound rules (443 from your
addresses, 80 from anywhere) for that tag. Connect with **SSH** on the VM's page. GKE: use the
[Kubernetes](kubernetes.md) guide with the `standard-rwo` storage class. Cloud Run isn't supported
([why](README.md#not-supported-and-why)).

### Oracle Cloud

Oracle's Always Free allowance includes Ampere (arm64) capacity, which runs Office Sentry from 2.1.0. Oracle's
Ubuntu images also come with a firewall on the server itself, blocking every port but SSH; if the portal
doesn't answer once the Security List allows 80 and 443, open them in the server's own firewall as Oracle's
documentation describes.

### Hetzner

Hetzner has its own guide, [Hetzner, from a phone](../../DEPLOY.md#hetzner-from-a-phone), which deploys and
updates from GitHub Actions and is how the project's own server runs. The start-up file here works on Hetzner
too, for a server you look after yourself.

### A host without cloud-init

Some VPS hosts (and on-premises virtual machines) don't take a start-up file. Install Docker
([docs.docker.com](https://docs.docker.com/engine/install/)), then follow
[First start](../../DEPLOY.md#first-start) on the server; it's the same result.
