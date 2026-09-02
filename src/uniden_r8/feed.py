"""A local HTTP and server-sent-events feed, for something with a screen.

The e-paper panel refreshes every five minutes.  That is fine for "the detector
is linked and the battery is at 13.6 volts" and useless for a threat that lasts
four seconds, which is why ``docs/SAFETY.md`` calls it a health tile rather than
a radar display.  This module is the other half of that answer: a small server
that pushes alert transitions the instant they happen, so a phone propped on the
dash or a laptop in the passenger seat can show what the detector is seeing.

It is written against the standard library alone.  No web framework, no
templating engine, no CDN: this runs on a Pi Zero 2 W with 415 MiB of RAM and
frequently no route to the internet, and a dashboard that needs to fetch a
script from elsewhere is a dashboard that is blank in a tunnel.

Three things it is careful about
--------------------------------
**It cannot stall the BLE link.**  The same event loop holds the detector's
subscription.  So every write to a client is wrapped in a timeout and a slow
client is disconnected rather than waited for -- a browser on a bad Wi-Fi link
must cost itself its stream, not cost the vehicle its radar data.  Client
queues are bounded and drop their oldest entry, for the same reason the ingest
queue does.

**It is loopback-only unless someone changes that on purpose.**  There is no
authentication here, because there is no good place to put a credential on a
device that boots unattended in a car.  What there is instead is a default bind
of ``localhost`` and a loud warning from
:meth:`uniden_r8.config.Config.warnings` when that is changed.  Reaching it
from a phone is meant to go through the Tailscale interface the node already
has, not through an open port on a coffee-shop network.

**It adds sustained radio load, and says so.**  A held SSE connection is not a
bounded window like a discovery scan; it is Wi-Fi traffic for the whole drive
on the same 2.4 GHz front end as the vehicle's RFCOMM link.  That is why it is
off by default and why ``docs/RUNBOOK.md`` makes a comparison trial a gate
before it is used on a drive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from typing import Any, Final

__all__ = [
    "DEFAULT_PORT",
    "MAX_CLIENTS",
    "StateFeed",
    "INDEX_HTML",
]

DEFAULT_PORT: Final[int] = 8787

#: More than this many simultaneous viewers on a Pi Zero is a mistake, not a
#: use case.  The next connection is refused with a plain message rather than
#: accepted into a queue nobody is draining.
MAX_CLIENTS: Final[int] = 8

#: Per-client outbound backlog.  Dropping the oldest is right here for the same
#: reason it is wrong in the history: a viewer that fell behind wants the
#: current state, not a replay of the last minute.
CLIENT_QUEUE: Final[int] = 64

#: Longest a write to one client may take before that client is dropped.
WRITE_TIMEOUT_SECONDS: Final[float] = 2.0

#: Longest a request line and its headers may be.  This server answers three
#: routes; anything larger is not a request it needs to read.
MAX_REQUEST_BYTES: Final[int] = 8192

#: How often a comment frame is sent on an idle stream.  Without it a proxy or
#: a phone's power manager silently drops a connection that has said nothing.
KEEPALIVE_SECONDS: Final[float] = 20.0

INDEX_HTML: Final[str] = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>R8</title>
<style>
 :root{color-scheme:dark;--bg:#0b0d10;--fg:#e8eaed;--dim:#8b939c;--line:#1e242c}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:16px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 header{padding:14px 16px;border-bottom:1px solid var(--line);
        display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
 h1{margin:0;font-size:15px;letter-spacing:.14em;text-transform:uppercase}
 #link{font-size:13px;color:var(--dim)}
 main{padding:16px;display:grid;gap:16px;max-width:820px}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
 .tile{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
 .tile b{display:block;font-size:11px;color:var(--dim);letter-spacing:.1em;
         text-transform:uppercase;font-weight:600}
 .tile span{font-size:22px}
 #alerts{display:grid;gap:8px;min-height:64px}
 .alert{border:1px solid var(--line);border-left-width:5px;border-radius:8px;
        padding:10px 12px;display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
 .alert .band{font-size:24px;font-weight:700;min-width:3.2em}
 .alert .bars{letter-spacing:2px}
 .KA,.KA\\ POP{border-left-color:#ff5c5c}
 .K,.K\\ POP{border-left-color:#ffb454}
 .X{border-left-color:#6cc7ff}
 .LASER{border-left-color:#ff4de0}
 .clear{color:var(--dim);border:1px dashed var(--line);border-radius:8px;padding:12px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left}
 th{color:var(--dim);font-weight:600;font-size:11px;letter-spacing:.08em;
    text-transform:uppercase}
 .stale{color:#ffb454}
</style>
<header>
  <h1>Uniden R8</h1><span id="link">connecting&hellip;</span>
</header>
<main>
  <div class="tiles">
    <div class="tile"><b>Voltage</b><span id="v">&mdash;</span></div>
    <div class="tile"><b>Detector GPS</b><span id="g">&mdash;</span></div>
    <div class="tile"><b>Status</b><span id="s">&mdash;</span></div>
    <div class="tile"><b>Packets</b><span id="p">&mdash;</span></div>
  </div>
  <div id="alerts"><div class="clear">clear</div></div>
  <table><thead><tr><th>time</th><th>event</th><th>band</th><th>dir</th>
    <th>bars</th><th>freq</th><th>held</th></tr></thead>
    <tbody id="log"></tbody></table>
</main>
<script>
const $=(id)=>document.getElementById(id);
const bars=(n)=>n?"\\u2588".repeat(n)+"\\u00b7".repeat(8-n):"";
function paint(d){
  const t=d.telemetry||{},c=d.collector||{};
  $("v").textContent=t.voltage!=null?t.voltage.toFixed(1)+" V":"\\u2014";
  const dg=d.detector_gps||{};
  $("g").textContent=t.gps_locked?(dg.direction_8||"fix"):(t.gps_locked===false?"no fix":"\\u2014");
  $("s").textContent=c.status||"\\u2014";
  $("s").className=t.stale?"stale":"";
  const n=d.counters||{};
  $("p").textContent=(n.telemetry_packets||0)+"/"+(n.alert_packets||0);
  const box=$("alerts"); box.innerHTML="";
  const list=d.alerts||[];
  if(!list.length){box.innerHTML='<div class="clear">clear</div>';return;}
  for(const a of list){
    const el=document.createElement("div");
    el.className="alert "+(a.band||"");
    const f=a.frequency_ghz!=null?a.frequency_ghz.toFixed(3)+" GHz":(a.laser_gun||"");
    el.innerHTML='<span class="band">'+(a.band||"?")+'</span>'+
      '<span class="bars">'+bars(a.strength||a.strength_1_to_8)+'</span>'+
      '<span>'+f+'</span><span>'+(a.direction||"")+'</span>'+
      (a.muted?'<span>muted</span>':'');
    box.appendChild(el);
  }
}
function logRow(e){
  const a=e.alert||{},tb=$("log");
  const tr=tb.insertRow(0);
  const when=new Date(Number(e.wall_ns/1000000n||0)).toLocaleTimeString();
  tr.innerHTML="<td>"+when+"</td><td>"+e.kind.replace("alert_","")+"</td><td>"+
    (a.band||"")+"</td><td>"+(a.direction||"")+"</td><td>"+
    (a.strength_1_to_8||"")+"</td><td>"+
    (a.frequency_ghz!=null?a.frequency_ghz.toFixed(3):"")+"</td><td>"+
    (e.duration_s!=null?e.duration_s.toFixed(1)+"s":"")+"</td>";
  while(tb.rows.length>60) tb.deleteRow(60);
}
const es=new EventSource("/events");
es.onopen=()=>{$("link").textContent="live";};
es.onerror=()=>{$("link").textContent="reconnecting\\u2026";};
es.addEventListener("state",(m)=>paint(JSON.parse(m.data)));
es.addEventListener("alert",(m)=>logRow(JSON.parse(m.data)));
</script>
"""


class _Client:
    """One connected SSE viewer and its bounded backlog."""

    __slots__ = ("writer", "queue", "wake", "dropped")

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.queue: deque[bytes] = deque()
        self.wake = asyncio.Event()
        self.dropped = 0

    def offer(self, frame: bytes) -> None:
        while len(self.queue) >= CLIENT_QUEUE:
            self.queue.popleft()
            self.dropped += 1
        self.queue.append(frame)
        self.wake.set()


class StateFeed:
    """Serves the current state and a live event stream on one local port.

    The collector calls :meth:`publish_state` and :meth:`publish_event`; both
    are synchronous, non-blocking, and safe to call from the same loop that
    holds the BLE link.  Everything that could wait -- a socket write, a slow
    client -- happens in the per-client task.
    """

    def __init__(
        self,
        bind: str = "localhost",
        port: int = DEFAULT_PORT,
        *,
        detail: bool = False,
    ) -> None:
        self.bind = bind
        self.port = port
        self.detail = detail
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[_Client] = set()
        self._state: bytes = b"{}"
        self.served = 0
        self.refused = 0
        self.last_error = ""

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Bind the port.  Returns success; a failure is reported, not raised.

        A port already in use must not stop the collector: radar data is the
        product and the dashboard is a convenience.
        """
        try:
            self._server = await asyncio.start_server(
                self._handle, self.bind, self.port
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.last_error = f"{type(exc).__name__} binding port {self.port}"
            return False
        return True

    async def stop(self) -> None:
        for client in list(self._clients):
            client.offer(b"")          # the empty frame means "close"
            client.wake.set()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._server is not None,
            "bind": self.bind,
            "port": self.port,
            "clients": len(self._clients),
            "served": self.served,
            "refused": self.refused,
            "dropped_frames": sum(client.dropped for client in self._clients),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------- publish

    def publish_state(self, document: dict[str, Any]) -> None:
        """Record the current state and push it to every viewer."""
        self._state = _json(document)
        self._broadcast(b"event: state\ndata: " + self._state + b"\n\n")

    def publish_event(self, event: dict[str, Any]) -> None:
        """Push one alert transition.  This is the reason the feed exists."""
        self._broadcast(b"event: alert\ndata: " + _json(event) + b"\n\n")

    def _broadcast(self, frame: bytes) -> None:
        for client in self._clients:
            client.offer(frame)

    # -------------------------------------------------------------- server

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """The connection callback.  Nothing may escape it.

        asyncio reports an exception from a ``client_connected_cb`` to the event
        loop's exception handler -- and that loop is the one holding the
        detector's subscription.  A malformed request from a browser extension
        must cost one closed socket, not a line in the journal and an
        unhandled-exception path through the machinery carrying the radar data.
        So the whole body is guarded, narrowly first and then absolutely.
        """
        try:
            await self._route(reader, writer)
        except Exception:  # noqa: BLE001 - see the docstring; nothing escapes
            _close(writer)

    async def _route(  # noqa: PLR0911 - one return per route reads best
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=5.0
            )
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            # Raised when the separator is not found within the stream reader's
            # own limit.  It descends from Exception directly rather than from
            # ValueError, so it is named here explicitly -- a header longer than
            # the reader's limit is exactly the case this bound exists for.
            asyncio.LimitOverrunError,
            ValueError,
            OSError,
        ):
            _close(writer)
            return
        if len(request) > MAX_REQUEST_BYTES:
            _close(writer)
            return

        line = request.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split(" ")
        method = parts[0] if parts else ""
        path = parts[1].split("?", 1)[0] if len(parts) > 1 else "/"
        self.served += 1

        if method not in {"GET", "HEAD"}:
            await _respond(writer, 405, b"text/plain", b"method not allowed\n")
            return
        if path in {"/", "/index.html"}:
            await _respond(writer, 200, b"text/html; charset=utf-8",
                           INDEX_HTML.encode("utf-8"))
            return
        if path == "/healthz":
            await _respond(writer, 200, b"text/plain", b"ok\n")
            return
        if path == "/state":
            await _respond(writer, 200, b"application/json", self._state)
            return
        if path == "/events":
            await self._stream(writer)
            return
        await _respond(writer, 404, b"text/plain", b"not found\n")

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        """Hold one SSE connection until the client goes away."""
        if len(self._clients) >= MAX_CLIENTS:
            self.refused += 1
            await _respond(writer, 503, b"text/plain", b"too many viewers\n")
            return

        client = _Client(writer)
        self._clients.add(client)
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: close\r\n"
                b"X-Accel-Buffering: no\r\n\r\n"
            )
            client.offer(b"event: state\ndata: " + self._state + b"\n\n")
            while True:
                if not client.queue:
                    client.wake.clear()
                    try:
                        await asyncio.wait_for(
                            client.wake.wait(), timeout=KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        writer.write(b": keepalive\n\n")
                        await asyncio.wait_for(
                            writer.drain(), timeout=WRITE_TIMEOUT_SECONDS
                        )
                        continue
                while client.queue:
                    frame = client.queue.popleft()
                    if not frame:
                        return
                    writer.write(frame)
                # A viewer on a bad link costs itself its stream; it must never
                # cost the detector its subscription.
                await asyncio.wait_for(
                    writer.drain(), timeout=WRITE_TIMEOUT_SECONDS
                )
        except (TimeoutError, OSError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(client)
            _close(writer)


def _json(document: Any) -> bytes:
    """Serialise compactly, on one line, as SSE requires.

    ``default=str`` rather than a failure: a document that cannot be encoded
    must still reach the dashboard in some form, because the dashboard is often
    how someone notices the document is wrong.
    """
    return json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")


async def _respond(
    writer: asyncio.StreamWriter, status: int, content_type: bytes, body: bytes
) -> None:
    reason = {200: b"OK", 404: b"Not Found", 405: b"Method Not Allowed",
              503: b"Service Unavailable"}.get(status, b"OK")
    try:
        writer.write(
            b"HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            % (status, reason, content_type, len(body))
        )
        writer.write(body)
        await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)
    except (TimeoutError, OSError):
        pass
    finally:
        _close(writer)


def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
