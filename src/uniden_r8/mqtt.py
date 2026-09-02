"""Publication to an MQTT broker, for home automation and remote dashboards.

Optional in every sense.  ``paho-mqtt`` is an extra rather than a dependency,
the import happens inside the one function that needs it, and the whole module
is inert until ``mqtt.enabled`` is set.  A node with no broker never loads a
line of this.

Why paho, and why its own thread
--------------------------------
``paho.mqtt`` with ``loop_start()`` runs its network I/O on a thread it owns,
and ``publish()`` only enqueues.  That is exactly the property this project
needs: the asyncio loop holding the BLE subscription must never wait on a TCP
socket to a broker that has gone away, and an async MQTT client sharing that
loop would do precisely that.  The threading is the feature, not an accident of
the library choice.

What is published, and how little of it
---------------------------------------
Alert transitions and a slow heartbeat.  **Not** the 1 Hz telemetry stream:
this is the first component in the project that puts sustained Wi-Fi traffic on
the same 2.4 GHz front end as the vehicle's RFCOMM link, and a packet a second
for a whole drive is a meaningful load on a Pi Zero 2 W's shared radio for very
little benefit.  ``docs/SAFETY.md`` records that reasoning, and
``docs/RUNBOOK.md`` makes a with-and-without comparison a gate before this runs
on a drive.

Topics, under a configurable base:

===============================  ==========================================
``<base>/status``                ``online`` / ``offline``, retained, with a
                                 last will so a dead collector says so
``<base>/state``                 the current document, retained
``<base>/alert``                 one transition per message, **not** retained
``<base>/availability``          Home Assistant's availability topic
===============================  ==========================================

Retention is chosen per topic on purpose.  A retained *state* means a dashboard
that connects mid-drive sees something immediately; a retained *alert* would
mean a broker replaying a threat that ended twenty minutes ago to every client
that subscribes, which is worse than silence.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Final

__all__ = [
    "MqttPublisher",
    "MqttUnavailable",
    "topic_map",
]


class MqttUnavailable(RuntimeError):
    """``paho-mqtt`` is not installed, or the broker could not be reached."""


#: Quality of service.  Zero, deliberately: an alert that arrives late is not
#: an alert, so a retry mechanism buys nothing here and costs radio time on a
#: link this project is trying to leave alone.
QOS: Final[int] = 0

#: How often the state document is published when nothing has changed.
#: Transitions publish immediately; this is the floor.
HEARTBEAT_SECONDS: Final[float] = 60.0


def topic_map(base: str) -> dict[str, str]:
    """Return the topics under *base*.  One place, so nothing drifts."""
    root = base.strip("/") or "unidenr8"
    return {
        "status": f"{root}/status",
        "state": f"{root}/state",
        "alert": f"{root}/alert",
        "availability": f"{root}/availability",
        "discovery_prefix": "homeassistant",
        "root": root,
    }


class MqttPublisher:
    """A thin, failure-tolerant wrapper around a paho client.

    Every method is safe to call whether or not the broker is reachable, and
    none of them raises: a broker outage must degrade to "nothing was
    published" and never to "the collector stopped collecting".
    """

    def __init__(  # noqa: PLR0913 - keyword-only broker settings
        self,
        *,
        host: str = "localhost",
        port: int = 1883,
        username: str = "",
        password: str | None = None,
        base_topic: str = "unidenr8",
        detail: bool = False,
        tls: bool = False,
        home_assistant: bool = False,
        client_factory: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.topics = topic_map(base_topic)
        self.detail = detail
        self.tls = tls
        self.home_assistant = home_assistant
        #: Injection seam: the tests substitute a recording client so the topic
        #: and retention decisions are provable with no broker anywhere.
        self._client_factory = client_factory
        self._client: Any = None
        self.connected = False
        self.published = 0
        self.errors = 0
        self.last_error = ""

    # ------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Connect and start the network thread.  Returns success."""
        try:
            client = self._build()
        except MqttUnavailable as exc:
            self.last_error = str(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.last_error = type(exc).__name__
            return False

        self._client = client
        try:
            # The will is set before connecting, which is the only time it can
            # be: it is what the broker publishes if this process dies without
            # saying goodbye, and a status topic that can only ever say
            # "online" is not a status topic.
            client.will_set(self.topics["status"], "offline", QOS, retain=True)
            if self.username:
                client.username_pw_set(self.username, self.password or None)
            if self.tls:
                client.tls_set()
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            self.errors += 1
            self._client = None
            return False

        self.connected = True
        self._publish(self.topics["status"], "online", retain=True)
        self._publish(self.topics["availability"], "online", retain=True)
        if self.home_assistant:
            self.announce()
        return True

    def stop(self) -> None:
        """Say goodbye properly, then disconnect.  Safe to call twice."""
        if self._client is None:
            return
        self._publish(self.topics["status"], "offline", retain=True)
        self._publish(self.topics["availability"], "offline", retain=True)
        with contextlib.suppress(Exception):
            self._client.loop_stop()
        with contextlib.suppress(Exception):
            self._client.disconnect()
        self._client = None
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._client is not None,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "base_topic": self.topics["root"],
            "detail": self.detail,
            "published": self.published,
            "errors": self.errors,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------- publish

    def publish_state(self, document: dict[str, Any]) -> None:
        """Retained, so a dashboard connecting mid-drive sees something."""
        self._publish(self.topics["state"], _json(document), retain=True)

    def publish_event(self, event: dict[str, Any]) -> None:
        """Not retained: a replayed alert from an hour ago is a false alarm."""
        self._publish(self.topics["alert"], _json(event), retain=False)

    def announce(self) -> None:
        """Emit Home Assistant MQTT discovery documents.

        Three sensors and a binary sensor: enough for an automation to react to
        a detection and for a dashboard to show link health, without turning
        this module into a description of somebody else's data model.
        """
        device = {
            "identifiers": ["unidenr8"],
            "name": "Uniden R8",
            "manufacturer": "Uniden",
            "model": "R8",
        }
        prefix = self.topics["discovery_prefix"]
        state = self.topics["state"]
        common = {
            "device": device,
            "availability_topic": self.topics["availability"],
            "state_topic": state,
        }
        entities: tuple[tuple[str, str, dict[str, Any]], ...] = (
            ("sensor", "voltage", {
                "name": "R8 voltage", "unique_id": "unidenr8_voltage",
                "device_class": "voltage", "unit_of_measurement": "V",
                "value_template": "{{ value_json.telemetry.voltage }}",
            }),
            ("sensor", "status", {
                "name": "R8 status", "unique_id": "unidenr8_status",
                "value_template": "{{ value_json.collector.status }}",
            }),
            ("sensor", "band", {
                "name": "R8 band", "unique_id": "unidenr8_band",
                "value_template":
                    "{{ value_json.alerts[0].band if value_json.alerts else 'clear' }}",
            }),
            ("binary_sensor", "alerting", {
                "name": "R8 alerting", "unique_id": "unidenr8_alerting",
                "device_class": "safety",
                "value_template":
                    "{{ 'ON' if value_json.alerts else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
            }),
        )
        for component, slug, config in entities:
            self._publish(
                f"{prefix}/{component}/unidenr8/{slug}/config",
                _json({**common, **config}),
                retain=True,
            )

    # ------------------------------------------------------------ internals

    def _build(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from paho.mqtt import client as paho  # noqa: PLC0415 - optional extra
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise MqttUnavailable(
                "MQTT publication needs paho-mqtt, which is an optional extra.  "
                "Install it with:  pip install 'uniden-r8-ble[mqtt]'"
            ) from exc
        # paho 2.x requires an explicit callback API version and 1.x does not
        # accept the argument at all.  Both are in the wild -- Debian ships
        # 1.6 -- so the version is discovered rather than assumed.
        version = getattr(paho, "CallbackAPIVersion", None)
        if version is not None:
            return paho.Client(version.VERSION2, client_id="uniden-r8")
        return paho.Client(client_id="uniden-r8")

    def _publish(self, topic: str, payload: Any, *, retain: bool) -> None:
        """Publish, counting failures instead of propagating them."""
        if self._client is None:
            return
        try:
            self._client.publish(topic, payload, qos=QOS, retain=retain)
            self.published += 1
        except Exception as exc:  # noqa: BLE001 - a broker outage is routine
            self.errors += 1
            self.last_error = type(exc).__name__


def _json(document: Any) -> str:
    return json.dumps(document, separators=(",", ":"), default=str)
