# Deploy guides

Office Sentry runs anywhere that can run two containers from one image and give them one disk. These
guides start from what you already run: a container manager, a NAS, a Kubernetes cluster or a cloud. Each
uses the published image, [`ghcr.io/jackd99/officesentry`](https://github.com/JackD99/OfficeSentry/pkgs/container/officesentry),
and keeps keys and passwords in the platform's own settings or secret store, never in a file you commit.

New to it and just want it running? A small Linux server with Docker is the simplest and the best tested:
[Running it on your own server](../../DEPLOY.md#first-start).

## Pick a guide

| You run | Guide | Tested |
|---|---|---|
| A Linux server with Docker | [Running it on your own server](../../DEPLOY.md#first-start) | Yes: the project's own server runs it |
| Portainer | [Portainer](portainer.md) | The stack file, with Docker Compose; Portainer's screens not yet |
| Dockge, Komodo, Coolify or Dokploy | [Other container managers](container-managers.md) | The stack file, with Docker Compose; the managers not yet |
| Unraid, Synology or TrueNAS | [Unraid, Synology and TrueNAS](nas.md) | The stack file, with Docker Compose; the NAS systems not yet |
| Kubernetes (AKS, EKS, GKE, k3s and others) | [Kubernetes](kubernetes.md) | Checked against a Kubernetes 1.32 API server and run as a pod in Docker; not yet on a full cluster |
| Microsoft Azure | [Microsoft Azure](azure.md) | Not yet; the server it builds runs the tested Docker setup |
| Amazon Web Services | [Amazon Web Services](aws.md) | Not yet; the server it builds runs the tested Docker setup |
| DigitalOcean, Google Cloud, Linode, Vultr, Oracle Cloud or another VPS | [Other clouds and VPS hosts](other-clouds.md) | Not yet; the server it builds runs the tested Docker setup |
| A domain on Cloudflare | [Cloudflare](cloudflare.md) | DNS only: yes. Proxied: Caddy's side tested. Tunnel: not yet |
| Windows Server without Docker | [Running on Windows without Docker](../../DEPLOY.md#running-on-windows-without-docker) | Yes |

"Not yet" means the steps follow each platform's documentation but haven't been run end to end. If one
doesn't work as written, please [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose)
saying where it went wrong, so the guide can be fixed.

## What every platform needs

Whichever guide you follow, these hold. They explain most of the choices in the guides.

- **One image, two containers.** The **portal** (`web`) is the image's default command and listens on port
  8000. The **worker** runs `python -m officesentry worker` from the same image and does all collection,
  scheduled emails and housekeeping. Run exactly one worker, and one portal (it runs several threads itself).
- **One disk, on the same machine as both.** Everything is stored in a SQLite database in `/data`, which
  both containers open at once. That needs a local disk or block storage (a server's own disk, Azure Disk,
  Amazon EBS, Google Persistent Disk, a NAS's own volume). Never put `/data` on a network file share (NFS,
  SMB, Azure Files, Amazon EFS): SQLite's documentation says the mode Office Sentry uses
  [doesn't work over a network filesystem](https://www.sqlite.org/wal.html), and the database can be
  damaged. This is why the container services listed at the end aren't supported.
- **The keys kept apart from the data.** The key that decrypts the stored certificates and passwords is
  either generated into `secrets.json` in a second volume, `/keys` (the default), or given to both containers as
  two environment variables from a secret store (below). Either way, save a copy somewhere safe, apart from
  the backups ([Where the keys are](../../DEPLOY.md#where-the-keys-are)).
- **HTTPS in front.** Sign-in cookies only work over HTTPS. Put a reverse proxy, ingress or tunnel in front of
  port 8000, and set `OFFICESENTRY_TRUSTED_PROXIES` to the number of proxies in front (usually 1), so sign-in
  limits and the activity log see each person's real address
  ([Securing the portal](../securing-the-portal.md#behind-a-reverse-proxy)).
- **User 10001.** The containers run as user ID 10001 with a read-only root filesystem and a writable
  `/tmp`. A volume Docker or Kubernetes creates is made writable for it; a folder on the host must be owned
  by user ID 10001 (`chown 10001 <folder>`), or the portal stops at start with
  `PermissionError: [Errno 1] Operation not permitted: '/data'`.
- **Size.** At least 2 CPUs and 4 GB of memory for the two together, and disk by the number of clients
  ([How much disk](../../DEPLOY.md#how-much-disk)). The image is published for amd64 (Intel and AMD) and
  arm64 (such as Ampere, AWS Graviton and Raspberry Pi 4 or 5).
- **Network.** The server makes outbound HTTPS calls to Microsoft (sign-in, Graph, Exchange Online and each
  client's SharePoint), to `ghcr.io` for updates, and once a day to `api.github.com` for the list of releases
  (*Settings → Updates* can turn that off).
  The audit log add-on also reads Microsoft's activity feed at `manage.office.com`. <!-- since 2.4.0 -->
  Nothing from Microsoft ever connects in: only the people using the portal need to reach it, plus a
  client's administrator returning from Microsoft's consent page (`/consent/callback`).
- **Updating.** Each guide says how: change the release and redeploy. *Settings → Updates* lists every new
  release and what it asks of you. Its **Update button** needs the updater container that the release's
  `docker-compose.yml` adds, run by Docker Compose from a folder on the server
  ([The Update button](../../DEPLOY.md#the-update-button)), so it works on a plain Docker server and the cloud
  guides built on one (Azure, AWS, other clouds), not under Portainer, another container manager, a NAS or
  Kubernetes.
- **Health checks.** `/health` answers 200 while the portal runs: use it for a platform's liveness and
  readiness checks. `/health/ready` also answers 503 when the worker, the database or the disk has a problem:
  use it for an uptime monitor ([Hear about problems without signing in](../../DEPLOY.md#hear-about-problems-without-signing-in)),
  never as the portal's liveness check, or the platform restarts a healthy portal when only the worker has
  stopped. The worker's check is the command `python -m officesentry healthcheck worker`.
- **The first admin.** On first start the portal's log has a line starting **No admin account yet** with a
  one-time link. `python -m officesentry setup-link`, run in the portal's container, prints it again.

## Keys from a secret store

On a platform with a secret store (Kubernetes Secrets, a container manager's secret variables), you can give
the keys to both containers instead of keeping them in a volume:

| Variable | What it is |
|---|---|
| `OFFICESENTRY_SECRET_KEY` | signs sign-in cookies: any long random text |
| `OFFICESENTRY_ENCRYPTION_KEYS` | encrypts stored certificates and passwords: a Fernet key (32 random bytes, URL-safe Base64). Several, separated by commas, during a key change; the first encrypts |

Make them on any machine with OpenSSL:

```bash
openssl rand -base64 48 | tr -d '\n'; echo         # OFFICESENTRY_SECRET_KEY
openssl rand -base64 32 | tr '+/' '-_'              # OFFICESENTRY_ENCRYPTION_KEYS
```

or with Docker: `docker run --rm ghcr.io/jackd99/officesentry:2 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
for the encryption key.

Set both, on both containers, before the first start, and keep a copy in your password manager: without
the encryption key a backup can't be read. When both are set, no `secrets.json` is written. To change the
encryption key later, put the new key first (`new,old`), restart, run `python -m officesentry rotate-keys`
in the portal's container, then remove the old key and restart again
([Rotating keys](../../DEPLOY.md#rotating-keys)). An install that already has a `secrets.json` keeps using it
until both variables are set; to move, copy the two values out of it.

## Running commands

Everything in [Running it on your own server](../../DEPLOY.md) that starts `docker compose exec web` works
on every platform: run the same `python -m officesentry ...` command in the portal's container (a
container manager's console, `docker exec`, or `kubectl exec`). Backups, the support package and rotating
keys all work this way, and each guide says where its console is.

Restoring is the exception: it refuses while the worker runs, and the portal shouldn't be serving while its
database is replaced. Stop both, run `restore` in a one-off container with the same two volumes, then start
them again. [Portainer](portainer.md#keys-and-backups) shows the `docker run` command, which works on any Docker host,
and [Kubernetes](kubernetes.md) has its own steps.

## Not supported, and why

- **Azure Container Apps, Azure App Service, AWS Fargate (ECS), AWS App Runner, Google Cloud Run** and other
  serverless container services. Their only lasting storage is a network file share (or none), and a
  SQLite database on one can be damaged. Use a small virtual machine on the same cloud
  ([Azure](azure.md), [AWS](aws.md), [Google Cloud](other-clouds.md#google-cloud)) or its Kubernetes service
  with block storage ([Kubernetes](kubernetes.md)).
- **More than one copy.** Two workers would collect everything twice and two portals can't share the database
  across machines. One server copes with hundreds of clients ([How fast](../../DEPLOY.md#how-fast)).
- **Docker Swarm stacks.** Swarm ignores the order the two containers start in (the portal upgrades the
  database before the worker starts) and places them on any node unless pinned. Run the stack on a standalone
  Docker host instead.
