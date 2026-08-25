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
- HTTPS: `443/tcp`

Raspberry Pi video uses SRT/MPEG-TS by default. H.264 is copied end-to-end; the server FFmpeg bridge only remuxes SRT/MPEG-TS to the existing localhost RTP ingest and does not decode or encode video.

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

The SRT ingest bridge requires FFmpeg with `libsrt` support:

```bash
apt update
apt install -y ffmpeg
ffmpeg -protocols 2>/dev/null | grep -E '^  srt$'
```

The last command must print `srt`.

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

The same port rules must be allowed in the Timeweb cloud firewall if one is attached to the server. In particular, UDP `12000-12099` must be reachable from Raspberry Pi devices for SRT.

## Build

```bash
cd /opt/robotlidar
git pull
cd radxa_zero3e/server
go mod download
go build -trimpath -ldflags="-s -w" -o robotlidar-server .
systemctl restart robotlidar
curl http://127.0.0.1:8000/health
```

After a Raspberry with video enabled registers, the server log should contain lines similar to:

```text
RTP ingest TRACTOR-RPI-...: udp://0.0.0.0:10000
SRT ingest TRACTOR-RPI-...: srt://0.0.0.0:12000 -> RTP 127.0.0.1:10000 (copy)
```

Check listeners with:

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
