"""Configuration: one TOML file, validated strictly, with safe defaults.

Everything the collector can do beyond "connect and publish a state file" is
off until it is switched on here.  History, coordinates, MQTT and the local
feed all default to disabled, because each of them is a place data can go, and
a feature that turns itself on is a feature that surprises someone.

Why a file at all
-----------------
The first version of this project was one node's tooling: the OBDLink health
gate named ``hummer-rfcomm`` in the source, the state directory was a constant,
and the systemd unit hard-coded a home directory.  That is fine for one Pi and
useless to anyone else, including a future Jeremy with a second vehicle.  This
module is what moves those decisions out of the code, so the same tree runs on
the Hummer node with the OBD guard armed and on a bare Raspberry Pi with it
switched off.

Validation is deliberately unforgiving
--------------------------------------
An unknown key is an error, not a warning.  A typo in ``record_coordinates``
that silently left coordinate logging at its default would be exactly the kind
of quiet failure this project's privacy rules exist to prevent, and "your
config had a typo" is a much better failure than "your config did not do what
you thought".  Every numeric value is range-checked at load, so a bad number
fails before the radio is touched rather than in the middle of a drive.

No secrets in this file
-----------------------
There is no ``password`` key, only ``password_file``.  A broker password in a
config file is a password in a backup, in a diff, and eventually in a paste;
pointing at a ``0600`` file keeps it in one place with permissions that mean
something.  The loader refuses a password file that is group- or
world-readable.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CONFIG_ENV_VAR",
    "DEFAULT_CONFIG_NAMES",
    "ConfigError",
    "CollectorConfig",
    "ObdConfig",
    "HistoryConfig",
    "GnssConfig",
    "MqttConfig",
    "FeedConfig",
    "Config",
    "load_config",
    "find_config",
    "example_toml",
]

#: Overrides the search below.  Set it in a systemd unit so the service's
#: configuration is explicit rather than dependent on a working directory.
CONFIG_ENV_VAR: Final[str] = "UNIDEN_R8_CONFIG"

#: Searched in order, relative to the working directory then the user's config
#: home.  The first that exists wins; none existing is not an error, because
#: the defaults are a complete, working configuration on their own.
DEFAULT_CONFIG_NAMES: Final[tuple[str, ...]] = (
    "unidenr8.toml",
    ".unidenr8.toml",
)


class ConfigError(ValueError):
    """The configuration file is malformed, unknown, or out of range."""


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorConfig:
    """The collector itself."""

    #: Where ``state.json`` and ``state-v2.json`` are written.  Created 0700.
    state_dir: str = ".state"
    #: BlueZ controller to pin the detector to, e.g. ``hci1``.  Empty means
    #: "whatever BlueZ picks".  Naming a second USB adapter here is the
    #: documented remedy if the shared controller cannot carry the vehicle's
    #: RFCOMM link and a BLE notification stream at once.
    adapter: str = ""
    #: Write the detailed schema-2 document alongside the schema-1 one.  On by
    #: default: it lands in the same owner-only directory, and the whole point
    #: of decoding the full packet is to be able to look at it.
    detail: bool = True
    #: How often state is rewritten when nothing has changed.  Transitions
    #: publish immediately regardless; this is the floor, not the rate.
    heartbeat_seconds: float = 5.0
    #: Depth of the ingest queue between the BLE callback and the consumer.
    queue_size: int = 256
    #: Telemetry older than this is reported as stale rather than as current.
    stale_after_seconds: float = 10.0


@dataclass(frozen=True)
class ObdConfig:
    """The OBDLink health gate.

    On the Hummer node this is the most important section in the file: it is
    what stops a long-running BLE session from competing with the vehicle's
    telemetry link.  On a bare Pi with no OBDLink it must be switched off, or
    the collector will refuse to run and be right to.
    """

    #: When false, no OBD probe runs and the collector never publishes
    #: ``obd-blocked``.  Set this on any node that has no OBDLink.
    guard: bool = True
    #: The systemd unit queried with ``systemctl is-active``.  Queried only.
    unit: str = "hummer-rfcomm"
    #: The device node checked for existence.  Never opened.
    device: str = "/dev/rfcomm0"
    #: How often the gate re-checks while the detector link is held.
    interval_seconds: float = 15.0


@dataclass(frozen=True)
class HistoryConfig:
    """The local SQLite history."""

    enabled: bool = False
    #: Relative paths resolve against the state directory.
    path: str = "history.db"
    #: Rows older than this are deleted on a periodic sweep.  Zero disables
    #: expiry, which is a choice a person can make and not a default.
    #:
    #: This is the *secondary* bound.  It measures against a wall clock, and
    #: the board this runs on has none worth trusting, so a sweep that would
    #: remove most of a table is refused rather than performed.  ``max_rows``
    #: below is what actually bounds the card.
    retain_days: int = 30
    #: Rows kept per table, deleted lowest-id-first.  This is the bound that
    #: works: ``rowid`` is monotone and completely immune to the clock, so it
    #: keeps a card bounded on a vehicle node that never sees NTP -- which is
    #: the normal state of one.  Zero means unlimited, and hands the whole job
    #: back to ``retain_days``.  The default is roughly thirty days of
    #: telemetry at the default sampling interval.
    max_rows: int = 250_000
    #: Telemetry is recorded at most this often.  The detector sends about one
    #: packet a second; storing every one is 86 400 rows a day of mostly
    #: identical voltage readings, on an SD card.
    telemetry_every_seconds: float = 10.0
    #: Store the detector's heading, speed and altitude in the history.  These
    #: are position-adjacent -- a log of them is a rough trace of a drive -- so
    #: recording them is opt-in even though decoding them is not.
    record_detector_motion: bool = False
    #: Store each alert notification exactly as it arrived.  On by default,
    #: unlike the two above, because an alert packet carries no position -- and
    #: because it is what makes the derived alert tracks re-derivable.  A
    #: better matcher written later needs something to run against, and this
    #: protocol is still being reverse-engineered.
    record_alert_snapshots: bool = True


@dataclass(frozen=True)
class GnssConfig:
    """External coordinates, read from ``gpsd``.

    The detector does not put latitude or longitude on the wire.  Nothing here
    changes that: this section configures a *separate* source, and the schema
    keeps it in a separate branch so a fix from a USB receiver can never be
    mistaken for something the detector said.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 2947
    #: Whether latitude and longitude may be stored or published at all.  With
    #: this false the client still runs and still reports fix quality, speed
    #: and course -- useful for validating the detector's own readings --
    #: without ever recording where the vehicle was.
    record_coordinates: bool = False
    #: A fix older than this is treated as no fix.
    stale_after_seconds: float = 5.0


@dataclass(frozen=True)
class MqttConfig:
    """Publication to a broker.

    Anything sent here leaves the node.  The detail flag is separate from the
    collector's own so that a rich local history and a minimal broker feed are
    a normal configuration rather than a compromise.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    #: Path to a ``0600`` file holding the password.  There is no inline
    #: password key, on purpose.
    password_file: str = ""
    base_topic: str = "unidenr8"
    #: Send the full decoded surface rather than the conservative subset.
    detail: bool = False
    #: Emit Home Assistant MQTT discovery documents on connect.
    home_assistant: bool = False
    #: TLS.  Off by default because the expected broker is on the same node;
    #: turning it on is the right move for anything else.
    tls: bool = False


@dataclass(frozen=True)
class FeedConfig:
    """A local HTTP/SSE feed, for a dashboard or a phone on the same network.

    Bound to loopback by default.  Changing that publishes live vehicle
    telemetry to every device on the network the Pi is attached to, so the
    loader warns about it rather than treating it as ordinary.
    """

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8787
    detail: bool = False


@dataclass(frozen=True)
class Config:
    """The whole configuration."""

    collector: CollectorConfig = field(default_factory=CollectorConfig)
    obd: ObdConfig = field(default_factory=ObdConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    gnss: GnssConfig = field(default_factory=GnssConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    #: Where this came from, for the "why is it behaving like that" question.
    source: str = "defaults"

    def as_dict(self) -> dict[str, Any]:
        document = asdict(self)
        # The password *path* is configuration; the password is not, and this
        # document is printed by `uniden-r8 config`.
        return document

    @property
    def history_path(self) -> Path:
        """The history database, resolved against the state directory."""
        candidate = Path(self.history.path)
        if candidate.is_absolute():
            return candidate
        return Path(self.collector.state_dir) / candidate

    def warnings(self) -> list[str]:
        """Configuration that is legal but worth saying out loud.

        Returned rather than logged so the CLI can print them and the tests can
        assert on them.  None of these is an error: they are all things a
        person may genuinely want, and all things a person may not have meant.
        """
        notes: list[str] = []
        if not self.obd.guard:
            notes.append(
                "OBD guard disabled: the collector will hold the detector link "
                "without checking the vehicle's RFCOMM binding first"
            )
        if self.feed.enabled and self.feed.bind not in {"127.0.0.1", "::1", "localhost"}:
            notes.append(
                f"feed bound to {self.feed.bind}: live vehicle telemetry will be "
                f"reachable from other machines on that network"
            )
        if self.mqtt.enabled and not self.mqtt.tls and self.mqtt.host not in {
            "127.0.0.1", "::1", "localhost"
        }:
            notes.append(
                "MQTT to a remote broker without TLS: state will cross the "
                "network in clear text"
            )
        if self.gnss.record_coordinates:
            notes.append(
                "coordinate recording enabled: the history will contain where "
                "the vehicle has been"
            )
        if self.history.enabled and self.history.retain_days == 0:
            notes.append("history retention disabled: rows are never expired")
        return notes


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

#: ``(section, key) -> (minimum, maximum)`` for every numeric value.  Kept as
#: data so the checks cannot drift from the defaults they guard.
_RANGES: Final[dict[tuple[str, str], tuple[float, float]]] = {
    ("collector", "heartbeat_seconds"): (0.5, 300.0),
    ("collector", "queue_size"): (8, 100_000),
    ("collector", "stale_after_seconds"): (1.0, 3600.0),
    ("obd", "interval_seconds"): (1.0, 600.0),
    ("history", "retain_days"): (0, 3650),
    ("history", "max_rows"): (0, 100_000_000),
    ("history", "telemetry_every_seconds"): (0.0, 3600.0),
    ("gnss", "port"): (1, 65535),
    ("gnss", "stale_after_seconds"): (0.5, 600.0),
    ("mqtt", "port"): (1, 65535),
    ("feed", "port"): (1, 65535),
}

_SECTIONS: Final[dict[str, type]] = {
    "collector": CollectorConfig,
    "obd": ObdConfig,
    "history": HistoryConfig,
    "gnss": GnssConfig,
    "mqtt": MqttConfig,
    "feed": FeedConfig,
}


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the configuration file to use, or ``None`` for pure defaults.

    An explicitly named file that does not exist is an error; a *searched* file
    that does not exist is not.  The distinction matters: "I told you where it
    is" and "look around and use one if you find one" deserve different
    answers, and silently ignoring a ``--config`` typo would run the collector
    with a configuration nobody chose.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"no configuration file at {path}")
        return path

    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        path = Path(from_env)
        if not path.is_file():
            raise ConfigError(
                f"{CONFIG_ENV_VAR} points at {path}, which does not exist"
            )
        return path

    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate

    home = Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    ) / "uniden-r8" / "config.toml"
    return home if home.is_file() else None


def _coerce(section: str, key: str, value: Any, expected: Any) -> Any:
    """Check one value against its declared type and range."""
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"[{section}] {key} must be true or false")
        return value
    if expected is int:
        # bool is a subclass of int; an accidental `true` where a number
        # belongs must not silently become 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"[{section}] {key} must be a whole number")
        return _ranged(section, key, value)
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"[{section}] {key} must be a number")
        return float(_ranged(section, key, float(value)))
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"[{section}] {key} must be a string")
        return value
    raise ConfigError(f"[{section}] {key} has an unsupported type")


def _ranged(section: str, key: str, value: float) -> float:
    bounds = _RANGES.get((section, key))
    if bounds is None:
        return value
    low, high = bounds
    if not low <= value <= high:
        raise ConfigError(
            f"[{section}] {key} = {value} is outside the permitted "
            f"range {low:g}-{high:g}"
        )
    return value


def _build_section(name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            f"[{name}] has unknown key(s): {', '.join(unknown)}.  "
            f"Known keys: {', '.join(sorted(known))}"
        )
    values = {
        key: _coerce(name, key, value, known[key].type
                     if not isinstance(known[key].type, str)
                     else _named_type(known[key].type))
        for key, value in raw.items()
    }
    return cls(**values)


_TYPE_NAMES: Final[dict[str, type]] = {
    "bool": bool, "int": int, "float": float, "str": str,
}


def _named_type(name: str) -> type:
    """Resolve a stringised annotation.

    ``from __future__ import annotations`` makes every dataclass field type a
    string, so the validator has to map the name back to the type rather than
    trusting ``f.type`` to be a class.
    """
    resolved = _TYPE_NAMES.get(name.strip())
    if resolved is None:
        raise ConfigError(f"unsupported configuration type {name!r}")
    return resolved


def load_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate the configuration.

    Returns the defaults when no file is found, which is a complete and working
    configuration: connect to the bonded detector, guard the OBDLink, publish
    two state documents, and send nothing anywhere else.
    """
    path = find_config(explicit)
    if path is None:
        return Config()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {type(exc).__name__}") from exc

    unknown_sections = sorted(set(raw) - set(_SECTIONS))
    if unknown_sections:
        raise ConfigError(
            f"{path}: unknown section(s) {', '.join(unknown_sections)}.  "
            f"Known sections: {', '.join(sorted(_SECTIONS))}"
        )

    built = {
        name: _build_section(name, cls, raw[name])
        for name, cls in _SECTIONS.items()
        if name in raw
    }
    config = Config(**built, source=str(path))
    _check_password_file(config)
    return config


def _check_password_file(config: Config) -> None:
    """Refuse a broker password anyone else on the machine can read."""
    if not config.mqtt.enabled or not config.mqtt.password_file:
        return
    path = Path(config.mqtt.password_file)
    if not path.is_file():
        raise ConfigError(f"[mqtt] password_file {path} does not exist")
    mode = path.stat().st_mode & 0o077
    if mode:
        raise ConfigError(
            f"[mqtt] password_file {path} is readable by group or other "
            f"(mode {path.stat().st_mode & 0o777:o}); chmod 600 it"
        )


def read_password(config: Config) -> str | None:
    """Return the broker password, or ``None`` if none is configured."""
    if not config.mqtt.password_file:
        return None
    return Path(config.mqtt.password_file).read_text(encoding="utf-8").strip()


def example_toml() -> str:
    """A complete, commented configuration file showing every default.

    Emitted by ``uniden-r8 config --example``.  Generated from the dataclasses
    rather than maintained by hand, so it cannot describe a key that no longer
    exists or omit one that was just added.
    """
    lines = [
        "# uniden-r8 configuration.  Every value below is the default;",
        "# delete anything you do not need to change.",
        "#",
        "# Searched for as ./unidenr8.toml, ./.unidenr8.toml, then",
        "# $XDG_CONFIG_HOME/uniden-r8/config.toml.  Override with",
        f"# {CONFIG_ENV_VAR} or --config.",
        "",
    ]
    for name, cls in _SECTIONS.items():
        lines.append(f"[{name}]")
        instance = cls()
        for f in fields(cls):
            value = getattr(instance, f.name)
            lines.append(f"{f.name} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe(config: Config) -> str:
    """A human-readable dump of the effective configuration."""
    lines = [f"configuration from {config.source}", ""]
    for name in _SECTIONS:
        section = getattr(config, name)
        lines.append(f"[{name}]")
        for f in fields(section):
            lines.append(f"  {f.name:<26} {getattr(section, f.name)!r}")
        lines.append("")
    notes = config.warnings()
    if notes:
        lines.append("notes:")
        lines += [f"  ! {note}" for note in notes]
    return "\n".join(lines)


assert is_dataclass(Config)  # noqa: S101 - a construction guard, not a test
