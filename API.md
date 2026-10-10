# Prawntenna HTTP API

Prawntenna manages RTL-SDR dongles on its host and republishes each one
as a standard `rtl_tcp` server on the LAN. This document describes the
HTTP control API — it is written for both humans and AI agents
implementing client software (e.g. an automated satellite receiver).
The running server also serves this file at `GET /api`.

All examples assume the manager runs on `http://HOST:8080/`.

## Conventions

- Every endpoint is a plain HTTP **GET** with query-string parameters —
  anything that can fetch a URL can drive the manager (no POST bodies,
  no headers, no auth).
- Action endpoints return JSON: `{"success": true|false, "message": str}`.
  On `success: false`, `message` explains what went wrong.
- A **dongle id** is the dongle's serial number string (e.g.
  `48263793`); if a dongle has no serial, the id is `index-N`.
- The manager never disturbs dongles or rtl_tcp processes it did not
  start itself.
- Port, spawn options and scan results are remembered per dongle in
  `dongles.json` and survive restarts of the manager or the host.

## Endpoints

### `GET /` — dashboard

Human-facing HTML dashboard showing all dongles and their state.

### `GET /dongles.json` — full state

The one endpoint a client polls. Response fields:

```
{
  "success": true,
  "dongles": [ { ...one entry per connected dongle... } ],
  "tools": {"rtl_tcp": true, "rtl_test": true},
  "bind": "0.0.0.0",
  "host": {"name": "...", "ips": ["192.168.3.245"], "uptime": 12345}
}
```

Per-dongle entry:

| Field | Meaning |
|---|---|
| `id` | stable dongle id (serial) — use this in API calls |
| `index`, `name`, `serial` | enumeration info |
| `published` | `true` if an rtl_tcp is serving this dongle right now |
| `port` | public rtl_tcp port while published, else `null` |
| `pid` | rtl_tcp process id while published, else `null` |
| `error` | last error message if the last publish/republish failed |
| `client` | `{"addr": "ip:port", "since": ts}` while a client is connected, else `null` — only one client at a time |
| `tune` | last rtl_tcp commands the connected client sent (see below) |
| `suggested_port` | a free choice suggestion (`1234 + index`) |
| `remembered_port` | port this dongle was last published on |
| `remembered_params` | spawn options remembered for this dongle (same keys as the `/publish` params) |
| `spawn_opts` | rtl_tcp options string currently in effect |
| `usb` | USB descriptor info from sysfs |
| `meta` | deep-scan results: tuner, gains, eeprom, ... |
| `scanning` | `true` while a deep-scan is running |

`tune` keys (updated live as the connected client sends rtl_tcp
commands): `ppm`, `freq_hz`, `rate_hz`, `gain_mode`, `gain_tenth_db`,
`if_gain`, `agc`, `direct_sampling`, `offset_tuning`.

### `GET /publish?d=ID&port=PORT[&options...]` — publish a dongle

Starts (or restarts) an `rtl_tcp` for dongle `ID`, reachable on
`HOST:PORT`. Publishing again on the same port replaces the rtl_tcp
process; publishing is idempotent and a valid way to re-tune.

Parameters:

| Param | rtl_tcp flag | Value | Notes |
|---|---|---|---|
| `d` | — | dongle id | **required** |
| `port` | — | 1024–65535 | **required** |
| `freq` | `-f` | Hz | tune frequency at startup |
| `rate` | `-s` | Hz | sample rate |
| `gain` | `-g` | tenths of dB | `0` = auto gain |
| `ppm` | `-P` | signed int | crystal correction, may be negative |
| `buffers` | `-b` | count | number of USB transfer buffers |
| `buflen` | `-n` | count | max linked-list buffers kept |
| `biastee` | `-T` | `1`/`0`/`true`/`false` | antenna bias-tee power (hardware-dependent) |
| `ds` | `-D` | `0`/`1`/`2` | direct sampling: 0 off, 1 I-ADC, 2 Q-ADC (HF) |

**Tri-state rule for all option params** (this is the core of the API):

- **absent** — keep the value remembered for this dongle
- **present but empty or invalid** (`?ppm=`) — clear it, rtl_tcp default
- **present and valid** — apply it and remember it for next time

**Warning about `buffers`/`buflen` on multi-dongle hosts:** on a
Raspberry Pi with two dongles sharing one USB 2 bus, non-default
`-b`/`-n` values (measured: `-b 32 -n 512`) collapsed the throughput of
**both** dongles to a few percent — the victim's stream delivered 3.4%
of its nominal sample rate with a raised noise floor. With rtl_tcp's
default buffer settings the same dongles coexist at full rate even
with one streaming at 2.048 MSPS. Leave `buffers`/`buflen` unset
unless you have measured a benefit on your specific hardware.

Typical error messages: the port already publishes another dongle, the
port is used by another service on the host, the dongle is unplugged,
or rtl_tcp failed to start (its log tail is returned).

### `GET /stop?d=ID` — stop a dongle

Stops the rtl_tcp spawned for this dongle and releases its port.

### `GET /scan?d=ID` — deep-scan a dongle

Probes tuner, gain list and EEPROM with `rtl_test`/`rtl_eeprom`. Only
works while the dongle is **not** held by an rtl_tcp or other SDR
software — stop it first. Results appear in `dongles.json` under
`meta`. Idle dongles are scanned automatically once; `scanning` in the
state shows a scan in progress.

### `GET /api` — this document

Served as `text/markdown` so client software can discover the API at
runtime.

## The published port speaks rtl_tcp

The public port is a standard `rtl_tcp` server (bytes are relayed
unmodified, Prawntenna only observes):

- One client at a time; a second connection is closed immediately.
- Commands from the client: 1 command byte + 4-byte big-endian uint32:

| Byte | Meaning |
|---|---|
| `0x00` | set ppm correction |
| `0x01` | set frequency [Hz] |
| `0x02` | set sample rate [Hz] |
| `0x03` | set gain mode (1 = manual) |
| `0x04` | set gain [tenths of dB] |
| `0x05` | set IF gain stage |
| `0x07` | set tuner AGC on/off |
| `0x08` | set direct sampling (0/1/2) |
| `0x09` | set offset tuning on/off |

- After the first command, an endless stream of interleaved unsigned
  8-bit I/Q samples flows from the server to the client.

Runtime commands always override the spawn options set via `/publish`.

## Self-healing behaviour clients should know

- If rtl_tcp dies or the stream wedges, Prawntenna kills it and
  **automatically republishes** the dongle on the same port within
  ~30 s. The public port stays owned by Prawntenna during this.
- Recommended client loop: connect → on disconnect, retry with backoff
  (a few seconds); only call `/publish` again if `/dongles.json` shows
  the dongle as not `published`.

## Recommended client flow

```
1. GET /dongles.json                    -> pick a dongle by id
2. GET /publish?d=ID&port=P&freq=...    -> publish it
3. TCP connect to HOST:P                -> rtl_tcp protocol
4. on stream drop: reconnect (backoff)  -> manager self-heals
5. when done: GET /stop?d=ID            -> release the dongle
```

Examples:

```
curl "http://192.168.3.245:8080/publish?d=48263793&port=1234&freq=137680000&rate=240000"
curl "http://192.168.3.245:8080/publish?d=48263793&port=1234&ppm=-3&biastee=1"
curl "http://192.168.3.245:8080/publish?d=48263793&port=1234&gain="   # clear remembered gain
curl "http://192.168.3.245:8080/stop?d=48263793"
```
