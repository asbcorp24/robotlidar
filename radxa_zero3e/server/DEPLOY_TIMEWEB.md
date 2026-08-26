# RobotLiDAR server on Timeweb

Production deployment target:

- domain: `tele.ар-баш.рф`
- punycode: `tele.xn----7sbbd7e6b.xn--p1ai`
- HTTP application: `127.0.0.1:8000`
- legacy RTP ingest: UDP `10000-11999`
- reliable Raspberry SRT ingest: UDP `12000-12099`
- WebRTC ICE (Pion direct): UDP `40000-40100`
- TURN/STUN listener: `3478/tcp`, `3478/udp`
- TURN relay allocation: UDP `50000-50100`
- HTTPS/WSS: `443/tcp`

Raspberry Pi video uses SRT/MPEG-TS by default. H.264 is copied end-to-end. The Go server receives SRT directly, reconstructs MPEG-TS/PES, packetizes the existing H.264 Annex-B stream to RTP in memory, and writes it into Pion WebRTC. There is no FFmpeg process, decode, or encode on the server.

Remote control uses an outbound WebSocket opened by Raspberry Pi to the server. This works through NAT and does not require forwarding UDP port 6000 on the Raspberry-side router. Legacy UDP control remains only as a fallback for local/older clients.

## Environment

```text
LISTEN_ADDR=127.0.0.1:8000
DB_PATH=/var/lib/robotlidar/camera_hub.db
STUN_URL=stun:stun.l.google.com:19302
WEBRTC_UDP_MIN=40000
WEBRTC_UDP_MAX=40100
TURN_URL=turn:tele.xn----7sbbd7e6b.xn--p1ai:3478
TURN_USERNAME=robotlidar
TURN_PASSWORD=<strong-random-password>
```

`TURN_URL` may contain more than one URL separated by commas, for example UDP and TCP variants.

## Server packages

No FFmpeg or libsrt system package is required for SRT ingest. SRT is implemented by the Go dependency `github.com/datarhei/gosrt` and is linked into `robotlidar-server`.

Only the normal build toolchain is needed:

```bash
apt update
apt install -y golang-go
```

If Go was installed from the official archive instead, the distro package is not required.

## Nginx: WebSocket is required

The HTTPS reverse proxy must pass WebSocket Upgrade headers to the Go server. In the `server { ... }` block for `tele.xn----7sbbd7e6b.xn--p1ai`, the proxy location should contain at least:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Then validate and reload:

```bash
nginx -t
systemctl reload nginx
```

The device control URL is:

```text
wss://tele.xn----7sbbd7e6b.xn--p1ai/api/devices/<DEVICE_ID>/control-ws
```

## UFW

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 3478/tcp
ufw allow 3478/udp
ufw allow 10000:11999/udp
ufw allow 12000:12099/udp
ufw allow 40000:40100/udp
ufw allow 50000:50100/udp
ufw reload
```

No inbound `6000/udp` rule is required for Raspberry devices using the WebSocket control channel.

The same SRT/WebRTC/TURN port rules must be allowed in the Timeweb cloud firewall if one is attached to the server. In particular, UDP `12000-12099` must be reachable from Raspberry Pi devices for SRT.

## Build

```bash
cd /opt/robotlidar
git pull
cd radxa_zero3e/server
go mod tidy
go build -trimpath -ldflags="-s -w" -o robotlidar-server .
systemctl restart robotlidar
curl http://127.0.0.1:8000/health
```

After a Raspberry with video and remote control enabled registers, the server log should contain lines similar to:

```text
RTP ingest TRACTOR-RPI-...: udp://0.0.0.0:10000
SRT ingest TRACTOR-RPI-...: srt://0.0.0.0:12000 (pure Go MPEG-TS/H.264 -> Pion)
SRT TRACTOR-RPI-... publisher connected from ...
SRT H.264 MPEG-TS detected video PID ...
CONTROL/WSS TRACTOR-RPI-... connected from ...
```

The RTP port remains allocated for backwards compatibility with legacy clients. In SRT mode, video does not pass through that UDP socket; SRT packets are parsed and written to the Pion track directly in memory.

Check listeners/logs with:

```bash
ss -lunp | grep -E ':120[0-9][0-9]|:100[0-9][0-9]'
journalctl -u robotlidar -f
```

## coturn example

Install:

```bash
apt update
apt install -y coturn
```

Example `/etc/turnserver.conf`:

```text
listening-port=3478
fingerprint
lt-cred-mech
realm=tele.xn----7sbbd7e6b.xn--p1ai
user=robotlidar:<same-password-as-TURN_PASSWORD>
min-port=50000
max-port=50100
no-multicast-peers
no-cli
```

Enable and restart:

```bash
sed -i 's/^#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/; s/^TURNSERVER_ENABLED=0/TURNSERVER_ENABLED=1/' /etc/default/coturn
systemctl enable coturn
systemctl restart coturn
systemctl status coturn --no-pager
```

For a coturn server behind NAT, additionally configure `external-ip=`. On a Timeweb VM with the public IP directly assigned to the interface this is normally unnecessary.

## systemd

Example `/etc/systemd/system/robotlidar.service` environment section:

```ini
Environment=LISTEN_ADDR=127.0.0.1:8000
Environment=DB_PATH=/var/lib/robotlidar/camera_hub.db
Environment=STUN_URL=stun:stun.l.google.com:19302
Environment=WEBRTC_UDP_MIN=40000
Environment=WEBRTC_UDP_MAX=40100
Environment=TURN_URL=turn:tele.xn----7sbbd7e6b.xn--p1ai:3478
Environment=TURN_USERNAME=robotlidar
EnvironmentFile=-/etc/robotlidar/turn.env
```

Store the password outside the unit in `/etc/robotlidar/turn.env`:

```text
TURN_PASSWORD=<strong-random-password>
```

Protect it:

```bash
mkdir -p /etc/robotlidar
chmod 700 /etc/robotlidar
chmod 600 /etc/robotlidar/turn.env
```

Then:

```bash
systemctl daemon-reload
systemctl restart robotlidar
journalctl -u robotlidar -n 100 --no-pager
```
