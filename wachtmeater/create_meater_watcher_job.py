#!/usr/bin/env python3
"""Deploy MEATER Watcher as a Kubernetes Job.

Creates a complete set of Kubernetes resources in the ``meater`` namespace to
run the MEATER cook monitoring stack as a Job:

- Namespace ``meater``
- ConfigMap ``meater-scripts-<short>`` with the 4 script files read from the repo
- Secret ``meater-env-<short>`` with a generated ``wachtmeater.toml`` file
- Job ``meater-watcher-<short>`` running ``xomoxcc/wachtmeater:latest``
  (data via hostPath ``/mnt/nfs/meaterwatcher_shared``)

Resource names include a short UUID derived from the cook URL so that
multiple watchers can coexist in the same namespace.

All operations are idempotent -- existing resources are replaced (or
delete-and-recreated for Jobs, which are immutable).

Examples:
    Create the job for a cook URL::

        $ wachtmeater deploy --meater-url https://cooks.cloud.meater.com/cook/abc123

    Tear down resources for a specific cook::

        $ wachtmeater deploy --delete --meater-url https://cooks.cloud.meater.com/cook/abc123
"""

import time
from collections.abc import Callable
from pathlib import Path

from jinja2 import Template
from kubernetes import client, config
from kubernetes.client import ApiException
from loguru import logger

from wachtmeater import cfg, read_dot_env_to_environ

read_dot_env_to_environ()


MATRIX_ROOM: str = cfg.matrix.room
MATRIX_USER: str = cfg.matrix.user
MATRIX_PASSWORD: str = cfg.matrix.password
MATRIX_SERVER_ADDRESS: str = cfg.matrix.server_address

NAMESPACE: str = cfg.k8s.namespace


def _short_uuid(meater_url: str) -> str:
    """Return first 8 alphanumeric chars of the cook UUID for resource naming."""
    uuid = meater_url.rstrip("/").split("/")[-1]
    return uuid.replace("-", "")[:8].lower()


def build_config_content(meater_url: str) -> str:
    """Build the ``wachtmeater.toml`` config for the K8s meater-watcher container.

    Generates a TOML configuration with sections for CDP browser access, MEATER
    cook monitoring, SIP call alerting, and SMTP/IMAP email configuration.

    Args:
        meater_url: The MEATER Cloud cook URL to monitor.

    Returns:
        Multi-line TOML string suitable for ``wachtmeater.toml``.
    """
    meater_uuid: str = meater_url.split("/")[-1]

    template_path = Path(__file__).resolve().parent / "templates" / "wachtmeater.toml.j2"
    template = Template(template_path.read_text())
    return template.render(
        meater_url=meater_url,
        meater_uuid=meater_uuid,
        browser=cfg.browser,
        sip=cfg.sip,
        monitoring=cfg.monitoring,
        smtp=cfg.smtp,
        imap=cfg.imap,
        matrix=cfg.matrix,
        auth=cfg.auth,
    )


def apply_resource(
    create_fn: Callable[[], object],
    replace_fn: Callable[[], object],
    kind: str,
) -> None:
    """Create a K8s resource, falling back to replace if it already exists.

    Args:
        create_fn: Callable that creates the resource (no arguments).
        replace_fn: Callable that replaces the resource (no arguments).
        kind: Human-readable resource identifier for log output
            (e.g. ``"configmap/meater-scripts"``).

    Raises:
        ApiException: If creation fails with a status other than 409 Conflict.
    """
    try:
        create_fn()
        logger.info(f"Created {kind}")
    except ApiException as e:
        if e.status == 409:
            replace_fn()
            logger.info(f"Replaced {kind}")
        else:
            raise


def create_resources(meater_url: str, hostpath: str = "/mnt/nfs/meaterwatcher_shared") -> None:
    """Create all Kubernetes resources for the MEATER watcher stack.

    Creates (or replaces) the namespace, ConfigMap, Secret, and Job
    in order. Script files are read from the local repo checkout and
    embedded into the ConfigMap. The Job is deleted and recreated if it
    already exists since K8s Jobs are immutable. Data is stored via
    hostPath at ``/mnt/nfs/meaterwatcher_shared``.

    Args:
        meater_url: The MEATER Cloud cook URL to embed in the ``.env`` Secret.
        hostpath: Host filesystem path mounted into the container for
            persistent data storage (default ``/mnt/nfs/meaterwatcher_shared``).

    Raises:
        SystemExit: If any of the required script files are missing on disk.
        ApiException: On unexpected Kubernetes API errors.
    """
    logger.info("Loading Kubernetes configuration...")
    config.load_kube_config()
    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()

    # 1. Namespace
    ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
    try:
        v1.create_namespace(body=ns)
        logger.info(f"Created namespace/{NAMESPACE}")
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"namespace/{NAMESPACE} already exists")
        else:
            raise

    # 2. ConfigMap with script files
    # cm_data = {}
    # for name, path in SCRIPT_FILES.items():
    #     if not path.exists():
    #         logger.error(f"{path} not found")
    #         sys.exit(1)
    #     cm_data[name] = path.read_text()
    # cm_data["entrypoint.sh"] = build_entrypoint_script()

    short = _short_uuid(meater_url)
    logger.debug(f"Resource suffix: {short} (from {meater_url})")
    # cm_name = f"meater-scripts-{short}"
    secret_name = f"meater-env-{short}"
    job_name = f"meater-watcher-{short}"
    annotations = {"wachtmeater/meater-url": meater_url}

    # cm = client.V1ConfigMap(
    #     metadata=client.V1ObjectMeta(
    #         name=cm_name, namespace=NAMESPACE, annotations=annotations,
    #     ),
    #     data=cm_data,
    # )
    # apply_resource(
    #     lambda: v1.create_namespaced_config_map(NAMESPACE, cm),
    #     lambda: v1.replace_namespaced_config_map(cm_name, NAMESPACE, cm),
    #     f"configmap/{cm_name}",
    # )

    # 3. Secret with wachtmeater.toml
    logger.debug("Generating wachtmeater.toml config...")
    wachtmeater_toml_config = build_config_content(meater_url)
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            namespace=NAMESPACE,
            annotations=annotations,
        ),
        string_data={"wachtmeater.toml": wachtmeater_toml_config},
    )
    apply_resource(
        lambda: v1.create_namespaced_secret(NAMESPACE, secret),
        lambda: v1.replace_namespaced_secret(secret_name, NAMESPACE, secret),
        f"secret/{secret_name}",
    )

    # 4. Job
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=NAMESPACE,
            annotations=annotations,
        ),
        spec=client.V1JobSpec(
            backoff_limit=5,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="OnFailure",
                    containers=[
                        client.V1Container(
                            name="meater-watcher",
                            image=cfg.k8s.image,
                            # security_context=client.V1SecurityContext(run_as_user=0),
                            command=cfg.k8s.job_command,
                            env=[
                                client.V1EnvVar(
                                    name="CONFIG",
                                    value="/config/wachtmeater.toml",
                                ),
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="config",
                                    mount_path="/config",
                                    read_only=True,
                                ),
                                client.V1VolumeMount(
                                    name="data",
                                    mount_path="/data",
                                ),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "50m", "memory": "128Mi"},
                                limits={"cpu": "200m", "memory": "256Mi"},
                            ),
                        ),
                    ],
                    volumes=[
                        client.V1Volume(
                            name="config",
                            secret=client.V1SecretVolumeSource(
                                secret_name=secret_name,
                            ),
                        ),
                        client.V1Volume(
                            name="data",
                            host_path=client.V1HostPathVolumeSource(
                                path=hostpath,
                                type="DirectoryOrCreate",
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )

    # Jobs can't be updated in-place — delete and recreate
    try:
        batch_v1.create_namespaced_job(NAMESPACE, job)
        logger.info(f"Created job/{job_name}")
    except ApiException as e:
        if e.status == 409:
            batch_v1.delete_namespaced_job(
                job_name,
                NAMESPACE,
                propagation_policy="Foreground",
            )
            logger.info(f"Deleted old job/{job_name}")
            # Wait briefly for deletion to propagate, then recreate
            logger.debug(f"Waiting for job/{job_name} deletion to propagate...")
            for _ in range(30):
                try:
                    batch_v1.read_namespaced_job(job_name, NAMESPACE)
                    time.sleep(1)
                except ApiException as e2:
                    if e2.status == 404:
                        break
            batch_v1.create_namespaced_job(NAMESPACE, job)
            logger.info(f"Created job/{job_name}")
        else:
            raise

    logger.success("Done. Check with: kubectl -n meater get all")


def delete_resources(meater_url: str) -> None:
    """Delete Kubernetes resources for a specific cook.

    Removes resources in reverse dependency order: Job, Secret, ConfigMap.
    The shared namespace is kept intact. Resources that don't exist are
    silently skipped.

    Args:
        meater_url: The MEATER Cloud cook URL whose resources to delete.

    Raises:
        ApiException: On unexpected Kubernetes API errors.
    """
    logger.info("Loading Kubernetes configuration...")
    config.load_kube_config()
    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()

    short = _short_uuid(meater_url)
    job_name = f"meater-watcher-{short}"
    secret_name = f"meater-env-{short}"
    cm_name = f"meater-scripts-{short}"
    logger.debug(f"Resource suffix: {short} — targeting job/{job_name}, secret/{secret_name}, configmap/{cm_name}")

    resources: list[tuple[Callable[..., object], tuple[str, str], str]] = [
        (batch_v1.delete_namespaced_job, (job_name, NAMESPACE), f"job/{job_name}"),
        (v1.delete_namespaced_secret, (secret_name, NAMESPACE), f"secret/{secret_name}"),
        (v1.delete_namespaced_config_map, (cm_name, NAMESPACE), f"configmap/{cm_name}"),
    ]
    for fn, args, kind in resources:
        try:
            kwargs = {}
            if "job" in kind:
                kwargs["propagation_policy"] = "Foreground"
            fn(*args, **kwargs)
            logger.info(f"Deleted {kind}")
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"{kind} not found (skipped)")
            else:
                raise

    logger.success("Done. Resources deleted for this cook.")
