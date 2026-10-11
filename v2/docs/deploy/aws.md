# Amazon Web Services

On AWS, run Office Sentry on one small Linux server: a **Lightsail** instance (the simplest, with a fixed
monthly price, snapshots and a firewall on one page) or an **EC2** instance. One paste at creation installs
Docker, downloads the newest release and starts it with automatic HTTPS; what runs on the server is then
exactly the Docker setup in [Running it on your own server](../../DEPLOY.md).

> **Tested:** the start-up file the server runs ([`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml)):
> its install script, run against the published release, brings up a healthy portal, worker and Caddy, and
> its nightly backup command works. The AWS console steps: not yet. If a screen differs, please
> [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

Already run EKS? Use the [Kubernetes](kubernetes.md) guide with the EBS CSI driver add-on and a `gp3` storage
class. ECS on Fargate, App Runner and Elastic Beanstalk aren't supported: Fargate's lasting storage is EFS, a
network share, and the others keep none, while Office Sentry's database needs a disk
([why](README.md#not-supported-and-why)).

## Before you start

Copy [`v2/deploy/cloud-init-server.yaml`](../../deploy/cloud-init-server.yaml) into a text editor and change
the two lines marked `yours`: the domain people will open the portal on (such as `sentry.yourmsp.com`) and
your timezone.

## Lightsail

1. In the [Lightsail console](https://lightsail.aws.amazon.com): **Create instance**. Pick the region where
   your clients' data should stay (such as London), **Linux/Unix**, **OS Only → Ubuntu 24.04 LTS**.
2. **Add launch script**, and paste the file from *Before you start*.
3. **Instance plan:** at least 4 GB of memory and 2 vCPUs, with the disk from
   [How much disk](../../DEPLOY.md#how-much-disk). Name it `officesentry`, and **Create instance**.
4. On the instance's **Networking** tab:
   - **Create static IP** and attach it, so the address doesn't change when the instance restarts.
   - Under **IPv4 Firewall**: edit **HTTPS (443)** (add it if missing) and tick **Restrict to IP address**
     with your office and home addresses. Keep **HTTP (80)** open to any address: Let's Encrypt checks there
     before issuing the certificate, and Caddy only redirects it to 443. Restrict **SSH (22)** to
     **Lightsail browser SSH/RDP** (and your own address, if you use your own SSH client).
   - Under **IPv6 Firewall**, do the same, or turn IPv6 off for the instance.
5. At your DNS provider add an **A** record for your domain with the static IP. (On Cloudflare, keep the
   proxy off: [Cloudflare](cloudflare.md).)
6. On the **Snapshots** tab, turn **Automatic snapshots** on: Lightsail keeps a copy of the whole server
   each day.
7. Open **Connect using SSH** (the browser terminal) and run:

   ```bash
   cd /opt/officesentry && docker compose logs web | grep "No admin account yet"
   ```

   (If you get *permission denied*, log out and in again, or use `sudo`. Nothing shown? The install may still
   be running: `cloud-init status` says.) Open the link, create the admin with two-step sign-in, and carry on
   from step 6 of [First start](../../DEPLOY.md#first-start).

## EC2

The same steps, with EC2's names for them:

- **Launch instance**, AMI **Ubuntu Server 24.04 LTS**, instance type **t3.medium** (2 vCPUs, 4 GiB) or the
  Graviton **t4g.medium** (arm64) with the Arm64 AMI. For hundreds of clients, a type that doesn't burst
  (m7i.large, m7g.large).
- **Storage:** a `gp3` root volume of the size in [How much disk](../../DEPLOY.md#how-much-disk).
- **Security group:** HTTPS (443) from your office and home addresses, HTTP (80) from anywhere. No SSH rule
  if you connect with **EC2 Instance Connect** or **Session Manager**.
- **Advanced details → User data:** the file from *Before you start*.
- After launch, allocate an **Elastic IP** and associate it, point your domain at it, and connect with
  **EC2 Instance Connect** to run the command in step 7 above.
- Back up the volume with **AWS Backup** or a Data Lifecycle Manager snapshot policy.

## Keep the key and the backups

- **The key.** Run `docker exec officesentry-web-1 cat /keys/secrets.json` on the server and save the line
  in your password manager, or in AWS Secrets Manager under an account only you can read
  ([Where the keys are](../../DEPLOY.md#where-the-keys-are)).
- **Nightly backups** run at 03:45 UTC, into `/data/backups` in the data volume, and *Settings → System
  status* alerts when one fails or is overdue. Snapshots copy the whole server, keys included, so keep access
  to them as tight as access to the server. For copies with another provider, add
  [Off-site backups](../../DEPLOY.md#off-site-backups).

## Everyday

Connect with the browser terminal (or Instance Connect) and work in `/opt/officesentry`; everything in
[Running it on your own server](../../DEPLOY.md) works as written there.

- **Updating:** read the [release notes](https://github.com/JackD99/OfficeSentry/releases), run
  `sudo sh /opt/officesentry/install.sh` (it downloads the newest release's `docker-compose.yml` and `Caddyfile`,
  which releases change without always saying so, and keeps your `.env`), set `OFFICESENTRY_VERSION` in `.env`
  to the new release, then `docker compose pull && docker compose up -d`.
- **Settings:** in `/opt/officesentry/.env` ([When things run](../../DEPLOY.md#when-things-run)), then
  `docker compose up -d`.
