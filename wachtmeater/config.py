"""Typed configuration dataclasses populated from environment variables.

Two-phase loading:

1. TOML / flat ``.env`` files populate ``os.environ`` (unchanged K8s compat).
2. ``WachtmeaterConfig.from_environ()`` constructs typed dataclasses from
   the environment.

Config file loading order
-------------------------
``wachtmeater.__init__.read_dot_env_to_environ()`` resolves configuration
in the following priority (first match wins per variable, using
``os.environ.setdefault``):

1. ``CONFIG`` env var — if set and points to a file, that single file is
   loaded (TOML or flat ``.env`` depending on extension).
2. Otherwise the following paths are probed **in order** in both the
   package directory and the current working directory:

   - ``wachtmeater.toml``
   - ``.env``
   - ``.local.env``
   - ``wachtmeater.local.toml``

   Every file found is loaded; earlier files take precedence because
   ``setdefault`` never overwrites.

Environment variables are never overwritten — real env vars (e.g. from
a Kubernetes Pod spec) always win.

Environment variable reference
------------------------------
**[smtp]** — SMTP mail-sending settings:
    ``SMTP_SERVER`` (default ``smtp.example.com``),
    ``SMTP_SERVER_PORT`` (``587``),
    ``SMTP_SERVER_USER``, ``SMTP_SERVER_PASSWORD``,
    ``SMTP_SERVER_SECURITY`` (``starttls``),
    ``FROM_EMAIL``, ``DEFAULT_TO``.

**[imap]** — IMAP mailbox-access settings:
    ``IMAP_SERVER``, ``IMAP_SERVER_PORT`` (``143``),
    ``IMAP_SERVER_SECURITY`` (``starttls``),
    ``IMAP_SERVER_USER``, ``IMAP_SERVER_PASSWORD``,
    ``SAVE_TO_SENT`` (``true``).

**[ollama]** — Ollama LLM API:
    ``OLLAMA_API_KEY``.

**[meater]** — MEATER Cloud probe/state-file:
    ``MEATER_URL`` (cook share URL),
    ``STATE_FILE_DIR`` (``/data``),
    ``STATE_FILE_NAME`` (auto-generated from UUID when empty).

**[browser]** — Headless browser (CDP):
    ``BROWSER_CDP_URL`` (default ``http://chrome-kasmvnc…:9222``),
    ``SCREENSHOT_DIR`` (``/data``).

**[sip]** — SIP telephony and call behaviour:
    ``SOPERATORURL``, ``RECORDING_DIR``, ``SIP_DEST``, ``SIP_SERVER``,
    ``SIP_PORT`` (``5161``), ``SIP_TRANSPORT`` (``tls``),
    ``SIP_SRTP`` (``mandatory``), ``SIP_USER``, ``SIP_PASSWORD``,
    ``SIP_CALL_PRE_DELAY`` (``1.0``), ``SIP_CALL_POST_DELAY`` (``1.0``),
    ``SIP_CALL_INTER_DELAY`` (``2.1``),
    ``SIP_CALL_REPEAT_ANNOUNCEMENT`` (``2``),
    ``SIP_CALL_TRANSCRIBE`` (``true``),
    ``SIP_CALL_WAIT_FOR_SILENCE`` (``2.0``),
    ``SIP_CALL_VERBOSE`` (``true``).

**[monitoring]** — Cook-monitoring thresholds and intervals:
    ``AMBIENT_TEMP_DROP_THRESHOLD`` (``10``),
    ``CHECK_INTERVAL`` (``600``),
    ``STALL_WINDOW`` (``3``),
    ``COOKEND_ERROR_THRESHOLD`` (``3``),
    ``COOKEND_PROBE_REMOVED_TEMP`` (``35.0``).

**[matrix]** — Matrix messaging:
    ``MATRIX_ROOM``, ``MATRIX_SERVER_ADDRESS``, ``MATRIX_USER``,
    ``MATRIX_HOMESERVER``, ``MATRIX_PASSWORD``, ``CRYPTO_STORE_PATH``,
    ``MATRIX_AUTO_CREATE_ROOM`` (``false``), ``MATRIX_PITMASTER``.

**[auth]** — Authentication / Keycloak:
    ``AUTH_METHOD`` (``password``), ``KEYCLOAK_URL``, ``KEYCLOAK_REALM``,
    ``KEYCLOAK_CLIENT_ID``, ``KEYCLOAK_CLIENT_SECRET``,
    ``JWT_LOGIN_TYPE``.

**[k8s]** — Kubernetes job deployment:
    ``WACHTMEATER_IMAGE`` (``xomoxcc/wachtmeater:latest``),
    ``WACHTMEATER_JOB_COMMAND`` (``["wachtmeater", "watcher"]``),
    ``NAMESPACE`` (``meater``).

**[alerts]** — Default alert mode settings (first run):
    ``ALERT_DEFAULT_TEMPDOWN_ENABLED`` (``true``),
    ``ALERT_DEFAULT_RUHEPHASE_ENABLED`` (``false``),
    ``ALERT_DEFAULT_RUHEPHASE_TARGET_TEMP`` (``0.0``),
    ``ALERT_DEFAULT_STALL_ENABLED`` (``false``),
    ``ALERT_DEFAULT_STALL_MIN_DELTA`` (``1.0``),
    ``ALERT_DEFAULT_WRAP_ENABLED`` (``false``),
    ``ALERT_DEFAULT_WRAP_TARGET_TEMP`` (``0.0``),
    ``ALERT_DEFAULT_AMBIENT_RANGE_ENABLED`` (``false``),
    ``ALERT_DEFAULT_AMBIENT_RANGE_MIN`` (``0.0``),
    ``ALERT_DEFAULT_AMBIENT_RANGE_MAX`` (``0.0``),
    ``ALERT_DEFAULT_COOKEND_ENABLED`` (``true``).
"""

import json
import os
from dataclasses import Field, asdict, dataclass, field, fields
from typing import Any, ClassVar, Self, TypeVar, dataclass_transform, overload


@overload
def env(var: str, *, default: bool) -> bool: ...
@overload
def env(var: str, *, default: int) -> int: ...
@overload
def env(var: str, *, default: float) -> float: ...
@overload
def env(var: str, *, default: str) -> str: ...
@overload
def env(var: str, *, default: list[str]) -> list[str]: ...


def env(var: str, *, default: str | int | float | bool | list[str]) -> str | int | float | bool | list[str]:
    """Declare a dataclass field linked to an environment variable.

    Args:
        var: Name of the environment variable to read at construction time.
        default: Fallback value used when the variable is unset.

    Returns:
        A ``dataclasses.field`` descriptor whose ``metadata["env"]`` key
        is inspected by ``_EnvMixin.from_environ``.
    """
    if isinstance(default, list):
        return field(default_factory=default.copy, metadata={"env": var})
    return field(default=default, metadata={"env": var})


def _coerce(value: str, target_type: type) -> str | int | float | bool | list[str]:
    """Coerce an environment-variable string to *target_type*.

    Args:
        value: Raw string value from ``os.environ``.
        target_type: The Python type to convert *value* into.

    Returns:
        The converted value.  JSON-encoded lists are parsed into
        ``list[str]``; scalar types use their built-in constructors;
        unrecognised types fall back to the original string.
    """
    if target_type is bool:
        return value.lower() in ("true", "1", "yes")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is list:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
        return [value]
    return value


def _resolve_type(f: Field[Any]) -> type:  # Field is invariant; Any is unavoidable
    """Resolve a dataclass field's annotation to a concrete type.

    Handles generic aliases (e.g. ``list[str]`` → ``list``) and string-form
    annotations produced by ``from __future__ import annotations``.

    Args:
        f: A dataclass ``Field`` whose ``.type`` may be a concrete type,
            a generic alias, or a string annotation.

    Returns:
        The resolved concrete ``type``, falling back to ``str`` when
        resolution fails.
    """
    t = f.type
    origin = getattr(t, "__origin__", None)
    if isinstance(origin, type):
        return origin
    if isinstance(t, type):
        return t
    mapping: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool, "list[str]": list}
    return mapping.get(t, str) if isinstance(t, str) else str


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


@dataclass_transform(field_specifiers=(env,))
def envdataclass(cls: type[_T]) -> type[_T]:
    """Dataclass decorator that recognises ``env()`` as a field specifier.

    Args:
        cls: The class to decorate.  Its fields should be declared using
            the ``env()`` helper so that ``_EnvMixin.from_environ`` can
            populate them from environment variables.

    Returns:
        The class wrapped with ``@dataclass``.
    """
    return dataclass(cls)


class _EnvMixin:
    """Mixin providing ``from_environ`` to dataclass subclasses.

    Subclasses must be decorated with ``@dataclass``.  The
    ``__dataclass_fields__`` class variable is declared so that mypy
    recognises the mixin as satisfying the ``DataclassInstance`` protocol.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]  # Field is invariant; Any is unavoidable

    @classmethod
    def from_environ(cls) -> Self:
        """Construct an instance by reading ``os.environ``.

        Each field whose ``metadata`` contains an ``"env"`` key is populated
        from the corresponding environment variable.  Missing variables fall
        back to the field's dataclass default.

        Returns:
            A new instance of the concrete dataclass subclass.
        """
        kwargs: dict[str, str | int | float | bool | list[str]] = {}
        for f in fields(cls):
            env_var = f.metadata.get("env")
            if env_var is None:
                continue
            raw = os.environ.get(env_var)
            if raw is not None:
                kwargs[f.name] = _coerce(raw, f.type if isinstance(f.type, type) else _resolve_type(f))
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Section dataclasses
# ---------------------------------------------------------------------------


@envdataclass
class SmtpConfig(_EnvMixin):
    """SMTP mail-sending settings.

    Attributes:
        server: SMTP server hostname (``SMTP_SERVER``).
        port: SMTP server port (``SMTP_SERVER_PORT``).
        user: SMTP login user (``SMTP_SERVER_USER``).
        password: SMTP login password (``SMTP_SERVER_PASSWORD``).
        security: Connection security mode (``SMTP_SERVER_SECURITY``).
        from_email: Sender address with display name (``FROM_EMAIL``).
        default_to: Default recipient address (``DEFAULT_TO``).
    """

    server: str = env("SMTP_SERVER", default="smtp.example.com")
    port: int = env("SMTP_SERVER_PORT", default=587)
    user: str = env("SMTP_SERVER_USER", default="user@example.com")
    password: str = env("SMTP_SERVER_PASSWORD", default="changeme")
    security: str = env("SMTP_SERVER_SECURITY", default="starttls")
    from_email: str = env("FROM_EMAIL", default="Example User <user@example.com>")
    default_to: str = env("DEFAULT_TO", default="recipient@example.com")


@envdataclass
class ImapConfig(_EnvMixin):
    """IMAP mailbox-access settings.

    Attributes:
        server: IMAP server hostname (``IMAP_SERVER``).
        port: IMAP server port (``IMAP_SERVER_PORT``).
        security: Connection security mode (``IMAP_SERVER_SECURITY``).
        user: IMAP login user (``IMAP_SERVER_USER``).
        password: IMAP login password (``IMAP_SERVER_PASSWORD``).
        save_to_sent: Whether to save sent mails to Sent folder (``SAVE_TO_SENT``).
    """

    server: str = env("IMAP_SERVER", default="imap.example.com")
    port: int = env("IMAP_SERVER_PORT", default=143)
    security: str = env("IMAP_SERVER_SECURITY", default="starttls")
    user: str = env("IMAP_SERVER_USER", default="user@example.com")
    password: str = env("IMAP_SERVER_PASSWORD", default="changeme")
    save_to_sent: bool = env("SAVE_TO_SENT", default=True)


@envdataclass
class OllamaConfig(_EnvMixin):
    """Ollama LLM API settings.

    Attributes:
        api_key: Ollama API key (``OLLAMA_API_KEY``).
    """

    api_key: str = env("OLLAMA_API_KEY", default="your-ollama-api-key-here")


@envdataclass
class MeaterConfig(_EnvMixin):
    """MEATER Cloud probe/state-file settings.

    Attributes:
        url: Full MEATER Cloud cook share URL (``MEATER_URL``).
        state_file_dir: Directory for the JSON state file (``STATE_FILE_DIR``).
        state_file_name: Explicit state file name; auto-generated from UUID
            when empty (``STATE_FILE_NAME``).
    """

    url: str = env("MEATER_URL", default="")
    state_file_dir: str = env("STATE_FILE_DIR", default="/data")
    state_file_name: str = env("STATE_FILE_NAME", default="")


@envdataclass
class BrowserConfig(_EnvMixin):
    """Headless browser (CDP) settings.

    Attributes:
        cdp_url: Chrome DevTools Protocol HTTP endpoint (``BROWSER_CDP_URL``).
        screenshot_dir: Directory for cook-page screenshots (``SCREENSHOT_DIR``).
    """

    cdp_url: str = env("BROWSER_CDP_URL", default="http://chrome-kasmvnc.kasmvnc.svc.cluster.local:9222")
    screenshot_dir: str = env("SCREENSHOT_DIR", default="/data")


@envdataclass
class SipConfig(_EnvMixin):
    """SIP telephony and call-behaviour settings.

    Attributes:
        operator_url: URL of the SIP operator service (``SOPERATORURL``).
        recording_dir: Directory for call recordings (``RECORDING_DIR``).
        dest: SIP destination number (``SIP_DEST``).
        server: SIP server hostname (``SIP_SERVER``).
        port: SIP server port (``SIP_PORT``).
        transport: SIP transport protocol (``SIP_TRANSPORT``).
        srtp: SRTP mode (``SIP_SRTP``).
        user: SIP account user (``SIP_USER``).
        password: SIP account password (``SIP_PASSWORD``).
        call_pre_delay: Seconds to wait before the call (``SIP_CALL_PRE_DELAY``).
        call_post_delay: Seconds to wait after the call (``SIP_CALL_POST_DELAY``).
        call_inter_delay: Seconds between repeated calls (``SIP_CALL_INTER_DELAY``).
        call_repeat_announcement: How many times to repeat the announcement
            (``SIP_CALL_REPEAT_ANNOUNCEMENT``).
        call_transcribe: Whether to transcribe the call (``SIP_CALL_TRANSCRIBE``).
        call_wait_for_silence: Seconds to wait for silence before hanging up
            (``SIP_CALL_WAIT_FOR_SILENCE``).
        call_verbose: Enable verbose call logging (``SIP_CALL_VERBOSE``).
    """

    operator_url: str = env("SOPERATORURL", default="http://sipstuff-operator.sipstuff.svc.cluster.local/call")
    recording_dir: str = env("RECORDING_DIR", default="/data/recordings")
    dest: str = env("SIP_DEST", default="")
    server: str = env("SIP_SERVER", default="sip.example.com")
    port: int = env("SIP_PORT", default=5161)
    transport: str = env("SIP_TRANSPORT", default="tls")
    srtp: str = env("SIP_SRTP", default="mandatory")
    user: str = env("SIP_USER", default="1005")
    password: str = env("SIP_PASSWORD", default="changeme")
    call_pre_delay: float = env("SIP_CALL_PRE_DELAY", default=1.0)
    call_post_delay: float = env("SIP_CALL_POST_DELAY", default=1.0)
    call_inter_delay: float = env("SIP_CALL_INTER_DELAY", default=2.1)
    call_repeat_announcement: int = env("SIP_CALL_REPEAT_ANNOUNCEMENT", default=2)
    call_transcribe: bool = env("SIP_CALL_TRANSCRIBE", default=True)
    call_wait_for_silence: float = env("SIP_CALL_WAIT_FOR_SILENCE", default=2.0)
    call_verbose: bool = env("SIP_CALL_VERBOSE", default=True)


@envdataclass
class MonitoringConfig(_EnvMixin):
    """Cook-monitoring thresholds and intervals.

    Attributes:
        ambient_temp_drop_threshold: Celsius drop from max ambient that
            triggers a fire-out alert (``AMBIENT_TEMP_DROP_THRESHOLD``).
        check_interval: Seconds between periodic MEATER checks
            (``CHECK_INTERVAL``).
        stall_window: Number of consecutive checks used for stall
            detection (``STALL_WINDOW``).
        cookend_error_threshold: Consecutive fetch errors before declaring
            cook ended (``COOKEND_ERROR_THRESHOLD``).
        cookend_probe_removed_temp: Internal temp below this (after having
            been above 50 C) triggers probe-removed detection
            (``COOKEND_PROBE_REMOVED_TEMP``).
    """

    ambient_temp_drop_threshold: int = env("AMBIENT_TEMP_DROP_THRESHOLD", default=10)
    check_interval: int = env("CHECK_INTERVAL", default=600)
    stall_window: int = env("STALL_WINDOW", default=3)
    cookend_error_threshold: int = env("COOKEND_ERROR_THRESHOLD", default=3)
    cookend_probe_removed_temp: float = env("COOKEND_PROBE_REMOVED_TEMP", default=35.0)


@envdataclass
class MatrixConfig(_EnvMixin):
    """Matrix messaging settings.

    Attributes:
        room: Default room ID or alias to join (``MATRIX_ROOM``).
        server_address: Matrix server address for API calls
            (``MATRIX_SERVER_ADDRESS``).
        user: Matrix user localpart or full MXID (``MATRIX_USER``).
        homeserver: Synapse URL for nio login (``MATRIX_HOMESERVER``).
        password: Matrix account password (``MATRIX_PASSWORD``).
        crypto_store_path: Persistent directory for nio SQLite E2EE stores
            (``CRYPTO_STORE_PATH``).
        auto_create_room_for_meater_uuid: Whether to auto-create a room per
            cook UUID (``MATRIX_AUTO_CREATE_ROOM``).
        pitmaster: Matrix user ID to invite into auto-created rooms
            (``MATRIX_PITMASTER``).
    """

    room: str = env("MATRIX_ROOM", default="!exampleroom:matrix.example.com")
    server_address: str = env("MATRIX_SERVER_ADDRESS", default="http://synapse.matrix.svc.cluster.local:8008")
    user: str = env("MATRIX_USER", default="")
    homeserver: str = env("MATRIX_HOMESERVER", default="http://synapse.matrix.svc.cluster.local:8008")
    password: str = env("MATRIX_PASSWORD", default="")
    crypto_store_path: str = env("CRYPTO_STORE_PATH", default="/data/crypto_store")
    auto_create_room_for_meater_uuid: bool = env("MATRIX_AUTO_CREATE_ROOM", default=False)
    pitmaster: str = env("MATRIX_PITMASTER", default="")


@envdataclass
class AuthConfig(_EnvMixin):
    """Authentication and Keycloak settings.

    Attributes:
        method: Login method — ``"password"`` or ``"jwt"`` (``AUTH_METHOD``).
        keycloak_url: Keycloak server URL (``KEYCLOAK_URL``).
        keycloak_realm: Keycloak realm name (``KEYCLOAK_REALM``).
        keycloak_client_id: OIDC client ID (``KEYCLOAK_CLIENT_ID``).
        keycloak_client_secret: OIDC client secret
            (``KEYCLOAK_CLIENT_SECRET``).
        jwt_login_type: Matrix login type used for JWT auth
            (``JWT_LOGIN_TYPE``).
    """

    method: str = env("AUTH_METHOD", default="password")
    keycloak_url: str = env("KEYCLOAK_URL", default="")
    keycloak_realm: str = env("KEYCLOAK_REALM", default="")
    keycloak_client_id: str = env("KEYCLOAK_CLIENT_ID", default="")
    keycloak_client_secret: str = env("KEYCLOAK_CLIENT_SECRET", default="")
    jwt_login_type: str = env("JWT_LOGIN_TYPE", default="com.famedly.login.token.oauth")


@envdataclass
class K8sConfig(_EnvMixin):
    """Kubernetes job deployment settings.

    Attributes:
        image: Container image for the watcher Job
            (``WACHTMEATER_IMAGE``).
        job_command: Entrypoint command list for the container
            (``WACHTMEATER_JOB_COMMAND``).
        namespace: Kubernetes namespace for all resources (``NAMESPACE``).
    """

    image: str = env("WACHTMEATER_IMAGE", default="xomoxcc/wachtmeater:latest")
    job_command: list[str] = env("WACHTMEATER_JOB_COMMAND", default=["wachtmeater", "watcher"])
    namespace: str = env("NAMESPACE", default="meater")


@envdataclass
class AlertDefaultsConfig(_EnvMixin):
    """Default alert mode settings applied on first run (no existing state file).

    Attributes:
        tempalert_tempdown_enabled: Enable ambient temp drop alert
            (``ALERT_DEFAULT_TEMPDOWN_ENABLED``).
        tempalert_ruhephase_enabled: Enable rest/cooldown alert
            (``ALERT_DEFAULT_RUHEPHASE_ENABLED``).
        ruhephase_target_temp: Target cooldown temperature in Celsius
            (``ALERT_DEFAULT_RUHEPHASE_TARGET_TEMP``).
        tempalert_stall_enabled: Enable stall detection
            (``ALERT_DEFAULT_STALL_ENABLED``).
        stall_min_delta: Minimum rise in Celsius over the stall window
            (``ALERT_DEFAULT_STALL_MIN_DELTA``).
        tempalert_wrap_enabled: Enable wrap reminder
            (``ALERT_DEFAULT_WRAP_ENABLED``).
        wrap_target_temp: Internal temp at which to remind about wrapping
            (``ALERT_DEFAULT_WRAP_TARGET_TEMP``).
        tempalert_ambient_range_enabled: Enable ambient range alert
            (``ALERT_DEFAULT_AMBIENT_RANGE_ENABLED``).
        ambient_range_min: Minimum acceptable ambient temp
            (``ALERT_DEFAULT_AMBIENT_RANGE_MIN``).
        ambient_range_max: Maximum acceptable ambient temp
            (``ALERT_DEFAULT_AMBIENT_RANGE_MAX``).
        tempalert_cookend_enabled: Enable cook-end detection
            (``ALERT_DEFAULT_COOKEND_ENABLED``).
    """

    tempalert_tempdown_enabled: bool = env("ALERT_DEFAULT_TEMPDOWN_ENABLED", default=True)
    tempalert_ruhephase_enabled: bool = env("ALERT_DEFAULT_RUHEPHASE_ENABLED", default=False)
    ruhephase_target_temp: float = env("ALERT_DEFAULT_RUHEPHASE_TARGET_TEMP", default=0.0)
    tempalert_stall_enabled: bool = env("ALERT_DEFAULT_STALL_ENABLED", default=False)
    stall_min_delta: float = env("ALERT_DEFAULT_STALL_MIN_DELTA", default=1.0)
    tempalert_wrap_enabled: bool = env("ALERT_DEFAULT_WRAP_ENABLED", default=False)
    wrap_target_temp: float = env("ALERT_DEFAULT_WRAP_TARGET_TEMP", default=0.0)
    tempalert_ambient_range_enabled: bool = env("ALERT_DEFAULT_AMBIENT_RANGE_ENABLED", default=False)
    ambient_range_min: float = env("ALERT_DEFAULT_AMBIENT_RANGE_MIN", default=0.0)
    ambient_range_max: float = env("ALERT_DEFAULT_AMBIENT_RANGE_MAX", default=0.0)
    tempalert_cookend_enabled: bool = env("ALERT_DEFAULT_COOKEND_ENABLED", default=True)


@dataclass
class WatcherState:
    """Per-cook persistent state (serialized to JSON)."""

    # Temperature tracking
    last_internal_temp: float | None = None
    last_ambient_temp: float | None = None
    max_ambient_temp: float | None = None
    max_internal_temp: float | None = None
    last_check: str | None = None
    target_reached_calls: int = 0
    internal_temp_history: list[float] = field(default_factory=list)
    consecutive_errors: int = 0
    # Alert modes
    tempalert_tempdown_enabled: bool = True
    tempalert_ruhephase_enabled: bool = False
    ruhephase_target_temp: float | None = None
    tempalert_stall_enabled: bool = False
    stall_min_delta: float = 1.0
    stall_alerted: bool = False
    tempalert_wrap_enabled: bool = False
    wrap_target_temp: float | None = None
    wrap_alerted: bool = False
    tempalert_ambient_range_enabled: bool = False
    ambient_range_min: float | None = None
    ambient_range_max: float | None = None
    tempalert_cookend_enabled: bool = True
    cook_ended: bool = False
    matrix_room_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WatcherState":
        """Construct a ``WatcherState`` from a plain dictionary.

        Unknown keys (e.g. from an older state file format) are silently
        ignored.

        Args:
            d: Dictionary, typically loaded from the JSON state file.

        Returns:
            A new ``WatcherState`` populated with the known keys from *d*.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """Serialize this state to a plain dictionary for JSON persistence.

        Returns:
            A dict representation of all fields (via ``dataclasses.asdict``).
        """
        return asdict(self)


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------


@dataclass
class WachtmeaterConfig:
    """Root configuration aggregating all service-specific sections.

    Attributes:
        smtp: SMTP mail-sending settings.
        imap: IMAP mailbox-access settings.
        ollama: Ollama LLM API settings.
        meater: MEATER Cloud probe/state-file settings.
        browser: Headless browser (CDP) settings.
        sip: SIP telephony and call-behaviour settings.
        monitoring: Cook-monitoring thresholds and intervals.
        matrix: Matrix messaging settings.
        auth: Authentication and Keycloak settings.
        k8s: Kubernetes job deployment settings.
        alerts: Default alert mode settings for first run.
    """

    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    imap: ImapConfig = field(default_factory=ImapConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    meater: MeaterConfig = field(default_factory=MeaterConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    sip: SipConfig = field(default_factory=SipConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    matrix: MatrixConfig = field(default_factory=MatrixConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    k8s: K8sConfig = field(default_factory=K8sConfig)
    alerts: AlertDefaultsConfig = field(default_factory=AlertDefaultsConfig)

    @classmethod
    def from_environ(cls) -> Self:
        """Build the full configuration tree from ``os.environ``.

        Returns:
            A fully populated ``WachtmeaterConfig`` instance.
        """
        return cls(
            smtp=SmtpConfig.from_environ(),
            imap=ImapConfig.from_environ(),
            ollama=OllamaConfig.from_environ(),
            meater=MeaterConfig.from_environ(),
            browser=BrowserConfig.from_environ(),
            sip=SipConfig.from_environ(),
            monitoring=MonitoringConfig.from_environ(),
            matrix=MatrixConfig.from_environ(),
            auth=AuthConfig.from_environ(),
            k8s=K8sConfig.from_environ(),
            alerts=AlertDefaultsConfig.from_environ(),
        )


SECTION_REGISTRY: dict[str, type[_EnvMixin]] = {
    "smtp": SmtpConfig,
    "imap": ImapConfig,
    "ollama": OllamaConfig,
    "meater": MeaterConfig,
    "browser": BrowserConfig,
    "sip": SipConfig,
    "monitoring": MonitoringConfig,
    "matrix": MatrixConfig,
    "auth": AuthConfig,
    "k8s": K8sConfig,
    "alerts": AlertDefaultsConfig,
}
