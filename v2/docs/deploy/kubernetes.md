# Kubernetes

Office Sentry on any Kubernetes cluster (AKS, EKS, GKE, k3s, RKE2, Talos and others), from the manifests in
[`v2/deploy/kubernetes`](../../deploy/kubernetes/), applied with `kubectl apply -k`. The portal and the worker
run as two containers in one pod with one volume: one copy, on block storage.

> **Tested:** the manifests are checked against a Kubernetes 1.32 API server (server-side dry run), and the
> pod passes the `restricted` Pod Security level. Its two containers were run in Docker the same way the pod
> runs them (shared network and volume, user 10001, read-only root, keys from variables): the worker waits for
> the portal, both health checks pass, and the worker stops cleanly on `SIGTERM`. Not yet run on a full
> cluster or behind a real ingress. If something differs on yours, please
> [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

## What's in the folder

| File | What it is |
|---|---|
| `kustomization.yaml` | the release to run, your settings, and the list of files below |
| `namespace.yaml` | the `officesentry` namespace, enforcing the `restricted` Pod Security level |
| `pvc.yaml` | the data volume: 20 GiB, `ReadWriteOnce`, on your cluster's default storage class |
| `deployment.yaml` | one pod: `web` (the portal, port 8000) and `worker`, sharing the volume, as user 10001 with a read-only root |
| `backup-cronjob.yaml` | the nightly database backup into `/data/backups`, on the same node as the pod |
| `service.yaml` | the portal inside the cluster, port 8000 |
| `ingress.yaml` | HTTPS from outside, through your ingress controller |
| `httproute.yaml` | the same through the Gateway API, instead of `ingress.yaml` |
| `maintenance.yaml` | not applied by default: a pod for restoring a backup |

Why it's shaped this way:

- **One replica, `Recreate`.** The worker must run exactly once, and the old pod stops before a new one
  starts, so the volume is never in two pods.
- **Both containers in one pod.** They share the SQLite database in `/data`, which needs one disk on one
  machine. The worker waits until the portal answers, because the portal upgrades the database first.
- **Block storage only.** The volume must be a disk one node mounts (`ReadWriteOnce`): Azure Disk
  (`managed-csi`) on AKS, EBS (`gp3`) on EKS, Persistent Disk (`standard-rwo`) on GKE, Longhorn, or
  `local-path` on a single-node k3s. Never Azure Files, EFS, NFS or another network share
  ([why](README.md#what-every-platform-needs)).
- **Keys from a Secret.** Both containers get `OFFICESENTRY_SECRET_KEY` and `OFFICESENTRY_ENCRYPTION_KEYS`
  from the `officesentry-keys` Secret, so no key is written to the volume or its backups. You create it with
  `kubectl`; it's in none of these files.
- **Probes.** The portal's liveness and readiness use `/health`, with up to 15 minutes to start (a database
  upgrade copies the database first). The worker's use `python -m officesentry healthcheck worker`.

## 1. Get the files and set them

Copy the folder from the repository, for example into your own GitOps repository, and open it:

```bash
git clone --depth 1 https://github.com/JackD99/OfficeSentry.git
cp -r OfficeSentry/v2/deploy/kubernetes officesentry && cd officesentry
```

- In `kustomization.yaml`: `newTag` is the [release](https://github.com/JackD99/OfficeSentry/releases) to
  run, and under `configMapGenerator` set `OFFICESENTRY_BASE_URL` (the address people will use) and
  `OFFICESENTRY_TIMEZONE`. Any other setting from [`.env.example`](../../.env.example) goes there as one more
  line.
- In `pvc.yaml`: the size ([How much disk](../../DEPLOY.md#how-much-disk)), and `storageClassName` if your
  default class isn't block storage.
- In `ingress.yaml`: your domain (twice), your controller's `ingressClassName`, and the cert-manager issuer
  in the annotation (or remove it and put your own certificate in the `officesentry-tls` Secret). Using the
  Gateway API? Set your Gateway in `httproute.yaml` and list it instead of `ingress.yaml`.

## 2. Create the keys Secret

```bash
kubectl create namespace officesentry
kubectl -n officesentry create secret generic officesentry-keys \
  --from-literal=OFFICESENTRY_SECRET_KEY="$(openssl rand -base64 48 | tr -d '\n')" \
  --from-literal=OFFICESENTRY_ENCRYPTION_KEYS="$(openssl rand -base64 32 | tr '+/' '-_')"
```

Save both values in your password manager now: without the encryption key, a backup or the volume can't be
read on a new cluster.

```bash
kubectl -n officesentry get secret officesentry-keys -o go-template='{{range $k, $v := .data}}{{$k}}={{$v | base64decode}}{{"\n"}}{{end}}'
```

Using External Secrets, Sealed Secrets or your cloud's key vault? Make the same Secret, with those two keys,
from there instead.

## 3. Apply

```bash
kubectl apply -k .
kubectl -n officesentry rollout status deploy/officesentry --timeout=15m
```

Then the first admin's link:

```bash
kubectl -n officesentry logs deploy/officesentry -c web | grep "No admin account yet"
kubectl -n officesentry exec deploy/officesentry -c web -- python -m officesentry setup-link   # prints it again
```

Open it, create the admin with two-step sign-in, and carry on from step 6 of
[First start](../../DEPLOY.md#first-start).

## The ingress and the visitor's address

`OFFICESENTRY_TRUSTED_PROXIES=1` (in `kustomization.yaml`) trusts one proxy: the ingress controller or
gateway. That's right when the controller sees each visitor's own address, which on a cloud load balancer
usually means its Service has `externalTrafficPolicy: Local` (or the load balancer passes the address with the
PROXY protocol). Sign in, then check *Settings → Activity log*: your sign-in should show your own address. If
it shows a node's or the load balancer's, fix the controller's Service rather than raising the number.

Allow a request 120 seconds (a long PDF can take a minute) and bodies of at least 2 MB, if your controller
limits either by default.

## Everyday commands

Every command in [Running it on your own server](../../DEPLOY.md) that starts `docker compose exec web` runs
the same way here:

```bash
kubectl -n officesentry exec deploy/officesentry -c web -- python -m officesentry <command>
```

| Task | How |
|---|---|
| A backup now | `... -c web -- python -m officesentry backup --keep 14` |
| What the backups did | `... -c web -- python -m officesentry backup-status` |
| A copy of a backup on your machine | `kubectl -n officesentry cp -c web <pod>:/data/backups/<file>.db.gz ./<file>.db.gz` (`kubectl -n officesentry get pods` names the pod) |
| The support package | `kubectl -n officesentry exec deploy/officesentry -c web -- python -m officesentry support-package --out - > support.zip` |
| The worker's log | `kubectl -n officesentry logs deploy/officesentry -c worker` |

The nightly CronJob keeps 14 backups in `/data/backups`, on the same volume. Keep copies off it too: volume
snapshots (your cloud's, or Velero), or copy the newest file out with `kubectl cp` from a scheduled job of
your own.

<!-- since 2.4.0 -->
From 2.4.0 the CronJob is optional. The worker makes the same copy by itself every night and keeps the last 7
([Backups](../../DEPLOY.md#backups)), skipping nights the CronJob has made one. To rely on the worker alone,
take `backup-cronjob.yaml` out of `kustomization.yaml`, run
`kubectl -n officesentry delete cronjob officesentry-backup`, and skip suspending and resuming it when you
restore.
<!-- /since -->

## Updating

Read the release notes first ([releases](https://github.com/JackD99/OfficeSentry/releases)), above all
**Action required**, **New Microsoft permissions** and **Database upgrade**. Then change `newTag` in
`kustomization.yaml` and apply again:

```bash
kubectl apply -k .
kubectl -n officesentry rollout status deploy/officesentry --timeout=15m
```

The old pod stops (a running collection gets two minutes to finish), and the new one upgrades the database
before the worker starts. The portal is down until then: seconds for a small database, minutes for a large one.

## Restoring a backup

1. Stop Office Sentry and the nightly backup:

   ```bash
   kubectl -n officesentry scale deploy/officesentry --replicas=0
   kubectl -n officesentry patch cronjob officesentry-backup -p '{"spec":{"suspend":true}}'
   ```

2. Start the maintenance pod (set its `image:` to the release you run first), and copy in a backup if it
   isn't already in `/data/backups`:

   ```bash
   kubectl apply -f maintenance.yaml
   kubectl -n officesentry wait --for=condition=Ready pod/officesentry-maintenance
   kubectl -n officesentry cp ./officesentry-20261005-033000.db.gz officesentry-maintenance:/data/restore.db.gz
   ```

3. Restore it ([Restoring a backup](../../DEPLOY.md#restoring-a-backup) explains what it checks and keeps):

   ```bash
   kubectl -n officesentry exec officesentry-maintenance -- python -m officesentry restore /data/restore.db.gz
   ```

4. Start again:

   ```bash
   kubectl -n officesentry delete pod officesentry-maintenance
   kubectl -n officesentry scale deploy/officesentry --replicas=1
   kubectl -n officesentry patch cronjob officesentry-backup -p '{"spec":{"suspend":false}}'
   ```

**On a new cluster**, create the `officesentry-keys` Secret with the values you saved (not new ones) before
step 1, apply as in [Apply](#3-apply), then restore as above.

## Changing the keys

Put a new encryption key in front of the old one in the Secret (`new,old`), restart the pod
(`kubectl -n officesentry rollout restart deploy/officesentry`), run
`... -c web -- python -m officesentry rotate-keys`, then remove the old key from the Secret and restart again.
Save the new value in your password manager ([Rotating keys](../../DEPLOY.md#rotating-keys)).
