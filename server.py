#!/usr/bin/env python3
"""Prawntenna — RTL-SDR dongle host.

Lists the RTL-SDR dongles plugged into this machine and publishes each
one as an rtl_tcp server on a user-chosen IP port, so network receivers
(e.g. an automated satellite receiver) can use the dongles from
anywhere on the LAN.

Stdlib only. Usage: python3 server.py [web_port]  (default: 8080)
The HTTP API is documented in API.md, also served at /api.

The manager never disturbs dongles or rtl_tcp processes it did not
start: enumeration probes rtl_test with an out-of-range device index
(the device list is printed, nothing is opened), and Stop only reaches
processes spawned by this server or adopted from a previous run via
their pid. The system rtl_tcp daemon (e.g. port 1234) is left alone.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, 'index.html')
API_FILE = os.path.join(BASE_DIR, 'API.md')
STATE_FILE = os.path.join(BASE_DIR, 'dongles.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
WEB_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
RTL_BIND = '0.0.0.0'
START_TS = time.time()

# Out-of-range device index: rtl_test prints the device list and exits
# without opening any dongle, so listing is safe while dongles are busy.
ENUM_PROBE = ['rtl_test', '-d', '65535']

_lock = threading.RLock()
_ports = {}     # dongle id -> port last chosen by the user (persisted)
_meta = {}      # dongle id -> deep-scan details (persisted)
_params = {}    # dongle id -> rtl_tcp spawn options last chosen by the
                # user (persisted): freq/rate Hz, gain tenth-dB, ppm
                # signed, buffers/buflen counts, biastee bool, ds 0/1/2.
                # None = rtl_tcp default
_runtime = {}   # dongle id -> {'proc': Popen|None, 'pid', 'port', 'error', 'log'}
_scanning = set()
_scan_ts = {}       # dongle id -> time of the last deep-scan attempt (cooldown)
_auto_retry_ts = {}  # dongle id -> time of the last auto-republish attempt

# Sentinel for "argument not given": publish_dongle then reuses the
# remembered per-dongle option instead of clearing it (None would).
_UNSET = object()


# ---------- dongle enumeration ----------

def enumerate_dongles():
    """List connected dongles: [] if none, None if rtl-sdr tools missing."""
    try:
        p = subprocess.run(ENUM_PROBE, capture_output=True, timeout=5)
        text = (p.stdout + p.stderr).decode('utf-8', 'replace')
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as e:
        text = ((e.stdout or b'') + (e.stderr or b'')).decode('utf-8', 'replace')
    dongles = []
    for m in re.finditer(r'^\s*(\d+):\s+(.+?)\s*$', text, re.M):
        desc = m.group(2)
        sn = re.search(r'SN:?\s*(\S+)', desc)
        name = re.sub(r',?\s*SN:?\s*\S+\s*$', '', desc).strip().strip(',')
        dongles.append({'index': int(m.group(1)), 'name': name,
                        'serial': sn.group(1) if sn else None})
    return dongles


def dongle_id(d):
    return d['serial'] if d['serial'] else 'index-%d' % d['index']


def device_selector(d):
    """rtl_tcp's -d accepts a device index or a serial."""
    return d['serial'] if d['serial'] else str(d['index'])


def usb_sysfs_meta(serial):
    """USB descriptor info from sysfs (Linux) — no device open needed."""
    if not serial:
        return {}
    base = '/sys/bus/usb/devices'
    try:
        devs = os.listdir(base)
    except OSError:
        return {}
    for dev in devs:
        if not re.match(r'^\d+(-[\d.]+)+$', dev):   # e.g. "1-1.2"
            continue
        try:
            with open(os.path.join(base, dev, 'serial')) as f:
                if f.read().strip() != serial:
                    continue
        except OSError:
            continue
        out = {}
        for key in ('idVendor', 'idProduct', 'manufacturer', 'product',
                    'speed', 'busnum', 'devpath', 'version'):
            try:
                with open(os.path.join(base, dev, key)) as f:
                    val = f.read().strip()
                if val:
                    out[key] = val
            except OSError:
                pass
        return out
    return {}


# ---------- helpers ----------

def _to_int(v):
    """HTTP query value -> positive int; None when empty/invalid/<= 0."""
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _to_signed(v):
    """HTTP query value -> signed int; None when empty/invalid."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    """HTTP query value -> bool; None when invalid."""
    s = str(v).strip().lower()
    if s in ('1', 'true', 'yes', 'on'):
        return True
    if s in ('0', 'false', 'no', 'off', ''):
        return False
    return None


def _to_ds(v):
    """HTTP query value -> direct-sampling mode 0/1/2; None otherwise."""
    try:
        n = int(float(v))
        return n if n in (0, 1, 2) else None
    except (TypeError, ValueError):
        return None


def port_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', port))
            return True
    except OSError:
        return False


def tcp_listening(port):
    """LISTEN check via /proc/net/tcp — makes no connection.

    A connect probe would be served by rtl_tcp like any client, and the
    instant probe close can poison it: rtl_tcp's write-error path sets
    its exit flag and cancels the USB transfers while the process keeps
    serving sockets — a silent, hung stream.
    """
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if (len(parts) > 3 and parts[3] == '0A'
                    and int(parts[1].rsplit(':', 1)[1], 16) == port):
                return True
    return False


def lan_ips():
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))  # no packets sent; just picks a route
            ip = s.getsockname()[0]
        if not ip.startswith('127.'):
            ips.append(ip)
    except OSError:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if not ip.startswith('127.') and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def log_path(did):
    return os.path.join(LOG_DIR, 'rtl_tcp_%s.log' % re.sub(r'[^\w.-]', '_', did))


# rtl_tcp lines that are startup info or its SIGTERM handler noise —
# not errors, even though they land in the log after a death
_LOG_NOISE = re.compile(
    r'Found \d+ device|^\s*\d+:|Using device|Signal caught|Allocating|'
    r'Found .* tuner|Tuned to|listening|zero-copy|PLL not locked|RTL-SDR')


def read_log_tail(path, lines=4):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            data = f.read().decode('utf-8', 'replace')
        ls = [l.strip() for l in data.splitlines() if l.strip()]
        return '\n'.join(ls[-lines:])
    except OSError:
        return ''


def error_from_log(path):
    """Meaningful error lines from an rtl_tcp log (noise dropped)."""
    lines = [l for l in read_log_tail(path, lines=12).splitlines()
             if l and not _LOG_NOISE.search(l)]
    return '\n'.join(lines[-4:])


# ---------- device meta / deep scan ----------

def _run_probe(cmd, timeout):
    """Run a probe tool; returns its combined output ('' on failure).

    rtl_test -t prints tuner and gain info at startup and then
    benchmarks forever — the timeout kills it, the info is already in
    the partial output.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (p.stdout + p.stderr).decode('utf-8', 'replace')
    except FileNotFoundError:
        return ''
    except subprocess.TimeoutExpired as e:
        return ((e.stdout or b'') + (e.stderr or b'')).decode('utf-8', 'replace')
    except OSError:
        return ''


def deep_scan(did):
    """Probe tuner/gains (rtl_test) and EEPROM (rtl_eeprom) of a dongle.

    Read-only — but the dongle must be free (not held by rtl_tcp or any
    other SDR program). Results are cached in _meta (persisted).
    """
    dongles = enumerate_dongles() or []
    d = next((x for x in dongles if dongle_id(x) == did), None)
    if d is None:
        return False, 'dongle not found — unplugged?'
    sel = device_selector(d)
    busy = False
    meta = {}
    out = _run_probe(['rtl_test', '-t', '-d', sel], 2)
    m = re.search(r'Found (.*?) tuner', out)
    if m:
        meta['tuner'] = m.group(1).strip()
    g = re.search(r'Supported gain values \((\d+)\):\s*([0-9.\s]+)', out)
    if g:
        meta['gains'] = ' '.join(g.group(2).split())
    busy = busy or 'usb_claim_interface' in out or 'Failed to open' in out
    # rtl_eeprom's -d only accepts a device index (not a serial)
    ep = _run_probe(['rtl_eeprom', '-d', str(d['index'])], 3)
    if 'Current configuration:' in ep:
        cfg = {}
        for ln in ep.split('Current configuration:')[1].splitlines():
            if ':' in ln and set(ln.strip()) != {'_'}:
                key, _, val = ln.partition(':')
                key, val = key.strip(), val.strip()
                if key and val:
                    cfg[key] = val
        if cfg:
            meta['eeprom'] = cfg
    busy = busy or 'usb_claim_interface' in ep or 'Failed to open' in ep
    if busy:
        return False, 'busy'
    with _lock:
        _meta[did] = meta
        save_state_locked()
    return True, 'ok'


def force_scan(did):
    """Deep-scan now (guarded); returns (ok, message)."""
    with _lock:
        if did in _scanning:
            return False, 'already scanning'
        _scanning.add(did)
        _scan_ts[did] = time.time()
    try:
        return deep_scan(did)
    finally:
        with _lock:
            _scanning.discard(did)


def maybe_autoscan(did, published):
    """Auto-scan idle dongles once (retry at most every 60 s)."""
    if published:
        return
    with _lock:
        if did in _meta or did in _scanning:
            return
        if time.time() - _scan_ts.get(did, 0) < 60:
            return
    threading.Thread(target=force_scan, args=(did,), daemon=True).start()


def maybe_autorepublish(did, rt):
    """Bring back a published dongle whose rtl_tcp died unexpectedly.

    Deliberate stops pop the runtime entry, so only unexpected deaths
    (dongle glitch, rtl_tcp crash) end up here — republished on their
    remembered port, at most every 30 s.
    """
    if rt is None or entry_alive(rt) or not rt.get('port'):
        return
    with _lock:
        if time.time() - _auto_retry_ts.get(did, 0) < 30:
            return
        _auto_retry_ts[did] = time.time()
    port = rt['port']

    def work():
        time.sleep(2)  # give the kernel a moment to release the USB device
        publish_dongle(did, port)

    threading.Thread(target=work, daemon=True).start()


def stream_watchdog():
    """Kill a wedged rtl_tcp so the stream can heal itself.

    rtl_tcp streams continuously while it serves a client — silence
    means its USB transfers were cancelled (its signal handler sets an
    exit flag, but the process keeps its sockets open, leaving a hung
    stream). A rtl_tcp that stopped listening accepts no clients at all.
    In both cases SIGKILL it: the proxy relays unwind, the auto-republish
    watchdog brings up a fresh rtl_tcp and the receiver reconnects.
    """
    while True:
        time.sleep(5)
        victims = []
        with _lock:
            now = time.time()
            for did, rt in _runtime.items():
                if not entry_alive(rt):
                    continue
                stalled = (rt.get('client') is not None
                           and now - rt.get('iq_ts', 0) > 10)
                deaf = (now - rt.get('born', 0) > 15
                        and not tcp_listening(rt.get('iport') or 0))
                if stalled or deaf:
                    victims.append((did, rt))
        for did, rt in victims:
            try:
                if rt.get('proc') is not None:
                    rt['proc'].kill()
                else:
                    os.kill(rt['pid'], signal.SIGKILL)
            except OSError:
                continue
            rt['error'] = 'rtl_tcp stalled — killed, auto-restarting'
            with _lock:
                _auto_retry_ts[did] = 0   # republish on the next poll


def pid_alive(pid):
    """POSIX-only liveness probe (pid adoption is a /proc feature)."""
    return bool(pid) and os.path.isdir('/proc/%d' % pid)


def entry_alive(rt):
    if rt.get('proc') is not None:
        return rt['proc'].poll() is None
    return pid_alive(rt.get('pid'))


# ---------- state persistence ----------

def load_state():
    global _ports, _meta, _params
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        _ports = state.get('ports', {})
        _meta = state.get('meta', {})
        _params = state.get('params', {})
        return state.get('published', {})
    except (OSError, ValueError):
        return {}


def save_state_locked():
    """Caller must hold _lock. Persists live children for later adoption."""
    published = {did: {'port': rt['port'], 'pid': rt['pid'],
                       'iport': rt.get('iport')}
                 for did, rt in _runtime.items() if entry_alive(rt)}
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'ports': _ports, 'published': published,
                       'meta': _meta, 'params': _params}, f, indent=2)
    except OSError:
        pass


# ---------- publish / stop ----------

def _bind_listener(port, addr=RTL_BIND):
    """TCP listener for the public rtl_tcp port (None if taken)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, port))
        s.listen(1)
        return s
    except OSError:
        return None


def _close_sock(s):
    try:
        s.close()
    except OSError:
        pass


def _detach_listener(rt):
    """Forget and close the entry's listener, waking its accept loop.

    close() alone does not interrupt a thread blocked in accept() —
    the in-flight syscall keeps the socket (and its port) alive in
    the kernel. The loop re-checks ownership via a short timeout, so
    it exits within a second of the detach."""
    with _lock:
        lsock = rt.get('listener')
        rt['listener'] = None
    if lsock is not None:
        _close_sock(lsock)


# rtl_tcp client commands: 1 byte id + uint32 big-endian parameter.
# rtl_tcp itself never reports its state back — parsing the command
# stream on the way through is the only way to know the tune settings.
_CMD_KEYS = {0x00: 'ppm', 0x01: 'freq_hz', 0x02: 'rate_hz',
             0x03: 'gain_mode', 0x04: 'gain_tenth_db', 0x05: 'if_gain',
             0x07: 'agc', 0x08: 'direct_sampling', 0x09: 'offset_tuning'}


def _relay(src, dst, entry, parse_cmds):
    """Copy bytes src→dst; parse rtl_tcp commands if parse_cmds."""
    pending = b''
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            if parse_cmds:
                pending += data
                while len(pending) >= 5:
                    cmd = pending[0]
                    key = _CMD_KEYS.get(cmd)
                    if key:
                        with _lock:
                            entry['tune'][key] = int.from_bytes(pending[1:5], 'big')
                    pending = pending[5:]
            if not parse_cmds:
                entry['iq_ts'] = time.time()  # feeds the stream watchdog
            dst.sendall(data)
    except OSError:
        pass
    finally:
        _close_sock(src)
        _close_sock(dst)  # unblocks the sibling relay


def _proxy_serve(entry, client, upstream):
    """Serve one connected client; clear monitor state on disconnect."""
    t_cmd = threading.Thread(target=_relay, args=(client, upstream, entry, True),
                             daemon=True)
    t_iq = threading.Thread(target=_relay, args=(upstream, client, entry, False),
                            daemon=True)
    t_cmd.start()
    t_iq.start()
    t_cmd.join()
    t_iq.join()
    with _lock:
        if entry.get('client'):
            entry['client'] = None
            entry['tune'] = {}


def _proxy_loop(entry):
    """Accept loop for a published dongle's public port.

    The client connects here; Prawntenna relays to rtl_tcp on its
    private loopback port, so it can show who is connected and which
    frequency/gain/rate the client tunes to. Bytes pass through
    unmodified, and only one client is served at a time — exactly like
    rtl_tcp itself.

    The accept has a short timeout: close() does not interrupt a
    thread blocked in accept() (the syscall keeps the port bound),
    so stop/port-change detaches the listener from the entry and
    this loop notices that within a second and exits.
    """
    lsock = entry['listener']
    lsock.settimeout(1.0)
    while True:
        try:
            client, addr = lsock.accept()
        except socket.timeout:
            with _lock:
                if entry.get('listener') is not lsock:
                    return  # stopped / port reassigned: listener closed
            continue
        except OSError:
            return  # listener closed: dongle stopped / manager shutting down
        with _lock:
            if entry.get('client'):
                _close_sock(client)  # rtl_tcp serves one client at a time
                continue
            entry['client'] = {'addr': '%s:%d' % (addr[0], addr[1]),
                               'since': int(time.time())}
            entry['tune'] = {}
            entry['iq_ts'] = time.time()
        try:
            upstream = socket.create_connection(('127.0.0.1', entry['iport']),
                                                timeout=5)
        except OSError:
            with _lock:
                entry['client'] = None
            _close_sock(client)
            continue
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_proxy_serve, args=(entry, client, upstream),
                         daemon=True).start()


def kill_entry(rt):
    proc = rt.get('proc')
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(3)
    elif rt.get('pid'):
        try:
            os.kill(rt['pid'], signal.SIGTERM)
        except OSError:
            return
        for _ in range(30):
            if not pid_alive(rt['pid']):
                return
            time.sleep(0.1)
        try:
            os.kill(rt['pid'], signal.SIGKILL)
        except OSError:
            pass


def stop_dongle(did):
    """Stop a published dongle (own child or adopted pid)."""
    with _lock:
        rt = _runtime.get(did)
    if rt is None:
        return False, 'dongle not published'
    _detach_listener(rt)  # closes the listener, ends the proxy accept loop
    kill_entry(rt)
    with _lock:
        _runtime.pop(did, None)
        save_state_locked()
    return True, 'rtl_tcp stopped (port %d released)' % rt['port']


def publish_dongle(did, port, freq=_UNSET, rate=_UNSET, gain=_UNSET,
                   ppm=_UNSET, buffers=_UNSET, buflen=_UNSET,
                   biastee=_UNSET, ds=_UNSET):
    """Publish a dongle; restarts it if already published.

    rtl_tcp listens on a private loopback port; a Prawntenna proxy owns
    the public port and relays both directions, so the dashboard can
    show the connected client and its tune commands.

    On a same-port restart the public listener and its proxy thread are
    kept and only the rtl_tcp child is replaced: rebinding a port
    fails while a client is (re)connecting on it, and a listener that
    survives watchdog kills is what lets the auto-republish recover a
    stalled stream.

    Optional rtl_tcp spawn options: freq/rate in Hz, gain in tenths
    of dB, ppm signed, buffers/buflen USB transfer buffer counts,
    biastee bool, ds direct-sampling mode 0/1/2. _UNSET (internal
    callers) reuses the remembered per-dongle value, None clears it
    (rtl_tcp default), a value applies and remembers it.
    """
    dongles = enumerate_dongles()
    if dongles is None:
        return False, 'rtl-sdr tools not found on this host'
    d = next((x for x in dongles if dongle_id(x) == did), None)
    if d is None:
        return False, 'dongle not found — unplugged? (refresh the page)'
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, 'invalid port'
    if not (1024 <= port <= 65535):
        return False, 'port must be between 1024 and 65535'
    with _lock:
        for other, rt in _runtime.items():
            if other != did and rt.get('port') == port and entry_alive(rt):
                return False, ('port %d already publishes dongle %s'
                               % (port, other))
        # reap dead entries holding the port: their rtl_tcp is gone
        # (crashed or the dongle was unplugged) and only their listener
        # socket still binds the port. An unplugged dongle is never
        # enumerated again, so nothing else would ever free it — the
        # user asking for the port wins over auto-republish.
        stale = [other for other, rt in _runtime.items()
                 if other != did and rt.get('port') == port
                 and not entry_alive(rt)]
        for other in stale:
            rt = _runtime.pop(other, None)
            if rt is not None and rt.get('listener'):
                _close_sock(rt['listener'])
        if stale:
            save_state_locked()
        old = _runtime.get(did)
    # same port: keep the listener (and its proxy thread); only the
    # rtl_tcp child behind it is replaced
    reuse = old if (old is not None and old.get('port') == port
                    and old.get('listener')) else None
    if reuse is not None:
        lsock = reuse['listener']
    else:
        if shutil.which('rtl_tcp') is None:
            return False, 'rtl_tcp not found on this host (install rtl-sdr)'
        # bind first: on failure nothing has been torn down yet
        lsock = _bind_listener(port)
        if lsock is None:
            return False, ('port %d is already in use on this host '
                           '(another rtl_tcp or service)' % port)
    if old is not None:
        kill_entry(old)
        if reuse is None:
            _detach_listener(old)  # port change: old listener goes
    # free loopback port for rtl_tcp itself
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(('127.0.0.1', 0))
        iport = probe.getsockname()[1]
        probe.close()
    except OSError:
        if reuse is None:
            _close_sock(lsock)
        return False, 'cannot allocate an internal port'
    log = log_path(did)
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        lf = open(log, 'wb')
    except OSError:
        if reuse is None:
            _close_sock(lsock)
        return False, 'cannot write log %s' % log
    # rtl_tcp spawn options: explicit values here, else the remembered
    # per-dongle ones (None = rtl_tcp default). -f/-s take Hz, -g takes
    # tenths of dB where 0 means auto — skipped when falsy. -P is
    # signed (0 = no correction, skipped), -b/-n take counts, -T is a
    # flag, -D takes 0/1/2 where 0 = off, skipped.
    with _lock:
        remembered = _params.get(did) or {}
    if freq is _UNSET:
        freq = remembered.get('freq')
    if rate is _UNSET:
        rate = remembered.get('rate')
    if gain is _UNSET:
        gain = remembered.get('gain')
    if ppm is _UNSET:
        ppm = remembered.get('ppm')
    if buffers is _UNSET:
        buffers = remembered.get('buffers')
    if buflen is _UNSET:
        buflen = remembered.get('buflen')
    if biastee is _UNSET:
        biastee = remembered.get('biastee')
    if ds is _UNSET:
        ds = remembered.get('ds')
    opts = []
    if freq:
        opts += ['-f', str(int(freq))]
    if rate:
        opts += ['-s', str(int(rate))]
    if gain:
        opts += ['-g', str(int(gain))]
    if ppm:
        opts += ['-P', str(int(ppm))]
    if buffers:
        opts += ['-b', str(int(buffers))]
    if buflen:
        opts += ['-n', str(int(buflen))]
    if biastee:
        opts += ['-T']
    if ds:
        opts += ['-D', str(ds)]
    argv = ['rtl_tcp', '-a', '127.0.0.1', '-p', str(iport),
            '-d', device_selector(d)] + opts
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=lf,
            start_new_session=True)
    except FileNotFoundError:
        lf.close()
        if reuse is None:
            _close_sock(lsock)
        return False, 'rtl_tcp not found on this host (install rtl-sdr)'
    lf.close()
    if reuse is not None:
        entry = reuse   # mutate in place: the proxy thread keeps working
        with _lock:
            entry.update({'proc': proc, 'pid': proc.pid, 'iport': iport,
                          'client': None, 'tune': {}, 'error': '',
                          'log': log, 'spawn_opts': ' '.join(opts),
                          'iq_ts': 0, 'born': time.time()})
    else:
        entry = {'proc': proc, 'pid': proc.pid, 'port': port, 'iport': iport,
                 'listener': lsock, 'client': None, 'tune': {},
                 'error': '', 'log': log, 'spawn_opts': ' '.join(opts),
                 'iq_ts': 0, 'born': time.time()}
    with _lock:
        _runtime[did] = entry
        _ports[did] = port
        _params[did] = {'freq': freq, 'rate': rate, 'gain': gain,
                        'ppm': ppm, 'buffers': buffers, 'buflen': buflen,
                        'biastee': biastee, 'ds': ds}
        save_state_locked()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = error_from_log(log)
            entry['error'] = tail or ('rtl_tcp exited with code %s' % proc.returncode)
            if reuse is None:
                # forget the closed socket: a later same-port publish
                # must bind a fresh listener, not reuse a dead one
                _detach_listener(entry)
            with _lock:
                save_state_locked()  # dead entry: not persisted as published
            return False, entry['error']
        if tcp_listening(iport):
            if reuse is None:
                threading.Thread(target=_proxy_loop, args=(entry,),
                                 daemon=True).start()
            return True, 'rtl_tcp serving %s on %s:%d' % (d['name'], RTL_BIND, port)
        time.sleep(0.1)
    entry['error'] = 'rtl_tcp did not start listening on port %d' % port
    if reuse is None:
        _detach_listener(entry)  # closed: not reusable (see above)
    return False, entry['error']


def _rtl_tcp_listen_port(pid):
    """The -p port of an rtl_tcp process, from /proc (None if not ours)."""
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            parts = [p for p in f.read().decode('utf-8', 'replace').split('\0') if p]
    except OSError:
        return None
    if not any('rtl_tcp' in p for p in parts):
        return None
    try:
        return int(parts[parts.index('-p') + 1])
    except (ValueError, IndexError):
        return None


def startup_restore(saved):
    """Adopt rtl_tcp children from a previous run; restart the rest."""
    for did, info in saved.items():
        port, pid = info.get('port'), info.get('pid')
        iport = info.get('iport')
        lport = _rtl_tcp_listen_port(pid) if pid and pid_alive(pid) else None
        if port and iport and lport == iport:
            # proxy-mode child survived: re-serve its public port
            lsock = _bind_listener(port)
            if lsock:
                with _lock:
                    _runtime[did] = {'proc': None, 'pid': pid, 'port': port,
                                      'iport': iport, 'listener': lsock,
                                      'client': None, 'tune': {},
                                      'error': '', 'log': log_path(did),
                                      'iq_ts': 0, 'born': time.time()}
                    threading.Thread(target=_proxy_loop,
                                     args=(_runtime[did],), daemon=True).start()
                continue
        if port and lport == port:
            # pre-proxy rtl_tcp still bound to the public port: stop it
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(30):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            if pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.5)
        # process is gone (or was migrated): restart if the dongle is here
        dongles = enumerate_dongles()
        if dongles is not None and any(dongle_id(x) == did for x in dongles):
            publish_dongle(did, port)
        else:
            with _lock:
                save_state_locked()  # drops the stale published entry


# ---------- API ----------

def api_state():
    dongles = enumerate_dongles()
    tools = {'rtl_tcp': bool(shutil.which('rtl_tcp')),
             'rtl_test': bool(shutil.which('rtl_test'))}
    out = []
    if dongles is not None:
        for d in dongles:
            did = dongle_id(d)
            with _lock:
                rt = _runtime.get(did)
                remembered = _ports.get(did)
                scanning = did in _scanning
                if rt is not None and not rt.get('error') and not entry_alive(rt):
                    rt['error'] = (error_from_log(rt['log'])
                                   or 'rtl_tcp process ended unexpectedly')
                published = rt is not None and entry_alive(rt)
                port = rt.get('port') if rt else None
                pid = rt.get('pid') if published else None
                client = rt.get('client') if published else None
                tune = dict(rt.get('tune') or {}) if published else {}
                error = ''
                if rt is not None and not published:
                    error = rt.get('error') or ''
                meta = dict(_meta.get(did) or {})
                spawn_opts = (rt or {}).get('spawn_opts', '')
                remembered_params = dict(_params.get(did) or {})
            if published and 'tuner' not in meta:
                # the rtl_tcp startup log names the tuner — available
                # even while the dongle is held, no probing possible
                log_text = read_log_tail(rt['log'], lines=30)
                m = re.search(r'Found (.*?) tuner', log_text)
                if m:
                    meta['tuner'] = m.group(1).strip() + ' (from rtl_tcp log)'
                t = re.search(r'Tuned to (\d+) Hz', log_text)
                if t:
                    meta['startup_tuned_hz'] = t.group(1)
            maybe_autoscan(did, published)
            maybe_autorepublish(did, rt)
            out.append({'id': did, 'index': d['index'], 'name': d['name'],
                        'serial': d['serial'], 'published': published,
                        'port': port, 'pid': pid, 'error': error,
                        'client': client, 'tune': tune,
                        'suggested_port': 1234 + d['index'],
                        'remembered_port': remembered,
                        'remembered_params': remembered_params,
                        'spawn_opts': spawn_opts,
                        'usb': usb_sysfs_meta(d['serial']),
                        'meta': meta, 'scanning': scanning})
    return {'success': True, 'dongles': out, 'tools': tools,
            'bind': RTL_BIND,
            'host': {'name': socket.gethostname(), 'ips': lan_ips(),
                     'uptime': int(time.time() - START_TS)}}


class Handler(BaseHTTPRequestHandler):
    server_version = 'Prawntenna/1.0'

    def do_GET(self):
        parsed = urlparse(self.path)
        # keep_blank_values: 'freq=' (empty) means "clear the remembered
        # value" and must stay distinguishable from an absent param
        path, params = parsed.path, parse_qs(parsed.query,
                                             keep_blank_values=True)
        try:
            if path == '/':
                self._serve_index()
            elif path == '/dongles.json':
                self._json(api_state())
            elif path == '/api':
                self._serve_api_doc()
            elif path == '/publish':
                did = (params.get('d') or [''])[0]
                port = (params.get('port') or [''])[0]
                if not did:
                    self._json({'success': False, 'message': 'missing dongle id'})
                else:
                    # all spawn options follow the same rule: absent →
                    # keep remembered, empty or invalid → clear it
                    # (rtl_tcp default), valid → apply
                    freq = _to_int(params['freq'][0]) if 'freq' in params else _UNSET
                    rate = _to_int(params['rate'][0]) if 'rate' in params else _UNSET
                    gain = _to_int(params['gain'][0]) if 'gain' in params else _UNSET
                    ppm = _to_signed(params['ppm'][0]) if 'ppm' in params else _UNSET
                    buffers = _to_int(params['buffers'][0]) if 'buffers' in params else _UNSET
                    buflen = _to_int(params['buflen'][0]) if 'buflen' in params else _UNSET
                    biastee = _to_bool(params['biastee'][0]) if 'biastee' in params else _UNSET
                    ds = _to_ds(params['ds'][0]) if 'ds' in params else _UNSET
                    ok, msg = publish_dongle(did, port, freq, rate, gain,
                                             ppm, buffers, buflen, biastee, ds)
                    self._json({'success': ok, 'message': msg})
            elif path == '/stop':
                did = (params.get('d') or [''])[0]
                if not did:
                    self._json({'success': False, 'message': 'missing dongle id'})
                else:
                    ok, msg = stop_dongle(did)
                    self._json({'success': ok, 'message': msg})
            elif path == '/scan':
                did = (params.get('d') or [''])[0]
                if not did:
                    self._json({'success': False, 'message': 'missing dongle id'})
                else:
                    ok, msg = force_scan(did)
                    self._json({'success': ok,
                                'message': ('device details scanned' if ok
                                            else ('dongle is busy — held by another '
                                                  'process, stop it first' if msg == 'busy'
                                                  else msg))})
            else:
                self._json({'success': False, 'message': 'not found'}, 404)
        except Exception as e:  # a broken request must not kill the server
            try:
                self._json({'success': False, 'message': str(e)}, 500)
            except OSError:
                pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_doc(self):
        """API.md as text/markdown — the machine-facing API docs."""
        try:
            with open(API_FILE, 'rb') as f:
                body = f.read()
        except OSError:
            self._json({'success': False, 'message': 'API.md missing'}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/markdown; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self):
        try:
            with open(INDEX_FILE, 'rb') as f:
                body = f.read()
        except OSError:
            self._json({'success': False, 'message': 'index.html missing'}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # the dashboard polls every few seconds; keep the console quiet


def main():
    saved = load_state()
    threading.Thread(target=startup_restore, args=(saved,), daemon=True).start()
    threading.Thread(target=stream_watchdog, daemon=True).start()
    httpd = ThreadingHTTPServer(('0.0.0.0', WEB_PORT), Handler)
    print('Prawntenna serving on http://0.0.0.0:%d' % WEB_PORT, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nbye — published dongles keep running '
              'and are re-adopted on the next start', flush=True)


if __name__ == '__main__':
    main()
