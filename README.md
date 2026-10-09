# Prawntenna — RTL-SDR Dongle Host

A lightweight web frontend that lists the RTL-SDR dongles plugged into
the machine it runs on and publishes each one as an `rtl_tcp` server on
an IP port of your choice, so network receivers (e.g. an automated
satellite receiver) can use the dongles from anywhere on the LAN.

## Run

```bash
sudo apt install rtl-sdr          # provides rtl_tcp + rtl_test
python3 server.py [port]           # default: 8080
```

Stdlib only — no pip dependencies. For unattended use on a dongle host
(adjust `User=` and the path in `prawntenna.service` to your setup):

```bash
sudo cp prawntenna.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now prawntenna
```

### HTTPS front (example domain: prawntenna.example.com)

nginx proxies port 80/443 (with a Let's Encrypt certificate) to the app
on 127.0.0.1:8080:

```bash
sudo tee /etc/nginx/sites-available/prawntenna <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name prawntenna.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/prawntenna /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d prawntenna.example.com   # adds 443 + HTTP->HTTPS redirect
```

Renewal is handled automatically by the Debian certbot timer. The host is
reachable via its IPv6 AAAA record, so the DNS entry must be updated if
the router's IPv6 prefix ever changes (see `EVENTLOG`-style ops notes).

## Features

- **Dongle listing** — manufacturer, serial and device index of every
  connected dongle. Enumeration probes `rtl_test` with an out-of-range
  device index: the device list is printed, nothing is opened, so
  dongles that are busy (recording, serving rtl_tcp) are not disturbed.
- **Publish / stop** — per dongle, pick a port and hit Publish. This
  runs `rtl_tcp` as a child process bound to a private loopback port,
  with a built-in proxy owning the public port; the dashboard shows the
  connect address, and stop terminates it again.
- **Client / tune monitoring** — the proxy passes the IQ stream through
  unmodified while watching the client's rtl_tcp commands, so the
  dashboard shows who is connected (address + duration) and what the
  receiver tuned the dongle to: frequency, sample rate, gain mode/value,
  AGC, ppm, direct sampling. rtl_tcp itself never reports state back —
  the command stream is the only source of truth.
- **Persistence** — port choices are remembered per dongle serial in
  `dongles.json`. If Prawntenna (or the machine) restarts while dongles
  are published, the surviving `rtl_tcp` processes are re-adopted by pid
  and otherwise restarted automatically.
- **Safety** — Prawntenna only manages processes it started or adopted.
  System rtl_tcp daemons (like a permanently running port 1234 service)
  are never touched; their dongle simply reports "busy" if you try to
  publish it.
