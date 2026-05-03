# Wachtmeater Operator — K8s manifests

Long-running pod that listens in a configured Matrix room
(`MATRIX_OPERATOR_LISTENING_ROOM`) for `operator …` commands and
spawns/destroys/lists per-cook watcher Jobs in the `meater` namespace.

## Files

| File | Purpose |
|---|---|
| `serviceaccount.yaml` | Dedicated `ServiceAccount` for the operator pod. |
| `role.yaml` | Namespaced `Role` with create/list/delete on `jobs` and `secrets`. |
| `rolebinding.yaml` | Binds the Role to the ServiceAccount. |
| `deployment.yaml` | `Deployment` with `replicas: 1`, `strategy: Recreate` (only one Matrix session at a time). |
| `configmap.example.yaml` | Example `wachtmeater.local.toml` ConfigMap — copy and fill in real values before applying. |

## Setup

1. Create the namespace if it does not exist:

   ```bash
   kubectl create namespace meater
   ```

2. Create the operator's config ConfigMap.  Easiest is to keep your
   local `wachtmeater.local.toml` (with `operator_listening_room` set)
   and:

   ```bash
   kubectl -n meater create configmap wachtmeater-operator-config \
       --from-file=wachtmeater.local.toml=./wachtmeater.local.toml
   ```

   Alternatively, copy `configmap.example.yaml`, replace the
   placeholders, and `kubectl apply -f`.

   The ConfigMap entry `wachtmeater.local.toml` is mounted via
   `subPath` directly into the container's `WORKDIR` (`/app`), so the
   file ends up at `/app/wachtmeater.local.toml` and is picked up by
   `read_dot_env_to_environ()`'s built-in lookup — no `CONFIG` env var
   needed.

3. Apply the RBAC + Deployment:

   ```bash
   kubectl apply -f serviceaccount.yaml
   kubectl apply -f role.yaml
   kubectl apply -f rolebinding.yaml
   kubectl apply -f deployment.yaml
   ```

4. Watch the logs to confirm it joined the listening room:

   ```bash
   kubectl -n meater logs deploy/wachtmeater-operator -f
   ```

## Required config keys

The operator reads the same TOML as a watcher.  In addition to all
normal `[matrix]`, `[auth]`, `[k8s]`, `[sip]` settings, set:

| Key | Purpose |
|---|---|
| `[matrix].operator_listening_room` | Room ID (`!…:srv`) or alias (`#…:srv`) the operator listens in. |
| `[matrix].operator_crypto_store_path` | Separate nio E2EE store path; default `/data/operator_crypto_store`. Avoids SQLite races with watcher pods that share the same Matrix user. |
| `[matrix].pitmaster` | The MXID allowed to issue operator commands.  Other senders are silently ignored. |

The operator runs under the same Matrix user as the watcher pods.  At
startup it auto-trusts every device of its own MXID
(`trust_devices_for_user`), so messages in shared rooms decrypt without
manual verification.

## Recognised commands

In the configured operator room, only `cfg.matrix.pitmaster` may issue:

```
operator new <MEATER_URL>      → create_resources(url)  (spawns a watcher Job)
operator delete <spec>         → delete_resources(url)  (spec = URL, UUID, short, or list-index)
operator list                  → list active meater-watcher-* Jobs
operator status                → operator health summary
operator help / hilfe          → command reference
```

## Notes

* `strategy: Recreate` is intentional — running two operator pods would
  produce duplicate replies and could race on `operator new` / `delete`.
* The hostPath mount (`/mnt/nfs/meaterwatcher_shared`) matches what
  `wachtmeater/create_meater_watcher_job.py` mounts into watcher Pods,
  so crypto stores and screenshots all sit on the same NFS share.
* RBAC is namespaced.  The operator cannot see or touch resources
  outside `meater`.

## Ansible

For the homelab setup these manifests are deployed via:

```
ansible/roles/k8s_workloads/tasks/iot/kubectlstuff_wachtmeater_operator.yml
```

(see the Ansible repo).  The files here are the canonical references
that the Ansible role templates render against.
