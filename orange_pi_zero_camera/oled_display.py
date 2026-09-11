#!/usr/bin/env python3
from __future__ import print_function

import fcntl
import glob
import json
import os
import socket
import subprocess
import time
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

CONFIG_PATH = os.environ.get("ORANGE_PI_CAMERA_CONFIG", "/etc/robotlidar/orange-pi-zero-camera.json")
I2C_SLAVE = 0x0703
WIDTH = 128
HEIGHT = 64
PAGES = 8

# 5x7 ASCII font. Each tuple is five vertical columns, least significant bit at top.
FONT = {
    " ": (0x00,0x00,0x00,0x00,0x00), "!": (0x00,0x00,0x5F,0x00,0x00),
    "#": (0x14,0x7F,0x14,0x7F,0x14), "%": (0x23,0x13,0x08,0x64,0x62),
    "'": (0x00,0x05,0x03,0x00,0x00), "(": (0x00,0x1C,0x22,0x41,0x00),
    ")": (0x00,0x41,0x22,0x1C,0x00), "+": (0x08,0x08,0x3E,0x08,0x08),
    ",": (0x00,0x50,0x30,0x00,0x00), "-": (0x08,0x08,0x08,0x08,0x08),
    ".": (0x00,0x60,0x60,0x00,0x00), "/": (0x20,0x10,0x08,0x04,0x02),
    "0": (0x3E,0x51,0x49,0x45,0x3E), "1": (0x00,0x42,0x7F,0x40,0x00),
    "2": (0x42,0x61,0x51,0x49,0x46), "3": (0x21,0x41,0x45,0x4B,0x31),
    "4": (0x18,0x14,0x12,0x7F,0x10), "5": (0x27,0x45,0x45,0x45,0x39),
    "6": (0x3C,0x4A,0x49,0x49,0x30), "7": (0x01,0x71,0x09,0x05,0x03),
    "8": (0x36,0x49,0x49,0x49,0x36), "9": (0x06,0x49,0x49,0x29,0x1E),
    ":": (0x00,0x36,0x36,0x00,0x00), ";": (0x00,0x56,0x36,0x00,0x00),
    "<": (0x08,0x14,0x22,0x41,0x00), "=": (0x14,0x14,0x14,0x14,0x14),
    ">": (0x00,0x41,0x22,0x14,0x08), "?": (0x02,0x01,0x51,0x09,0x06),
    "@": (0x32,0x49,0x79,0x41,0x3E),
    "A": (0x7E,0x11,0x11,0x11,0x7E), "B": (0x7F,0x49,0x49,0x49,0x36),
    "C": (0x3E,0x41,0x41,0x41,0x22), "D": (0x7F,0x41,0x41,0x22,0x1C),
    "E": (0x7F,0x49,0x49,0x49,0x41), "F": (0x7F,0x09,0x09,0x09,0x01),
    "G": (0x3E,0x41,0x49,0x49,0x7A), "H": (0x7F,0x08,0x08,0x08,0x7F),
    "I": (0x00,0x41,0x7F,0x41,0x00), "J": (0x20,0x40,0x41,0x3F,0x01),
    "K": (0x7F,0x08,0x14,0x22,0x41), "L": (0x7F,0x40,0x40,0x40,0x40),
    "M": (0x7F,0x02,0x0C,0x02,0x7F), "N": (0x7F,0x04,0x08,0x10,0x7F),
    "O": (0x3E,0x41,0x41,0x41,0x3E), "P": (0x7F,0x09,0x09,0x09,0x06),
    "Q": (0x3E,0x41,0x51,0x21,0x5E), "R": (0x7F,0x09,0x19,0x29,0x46),
    "S": (0x46,0x49,0x49,0x49,0x31), "T": (0x01,0x01,0x7F,0x01,0x01),
    "U": (0x3F,0x40,0x40,0x40,0x3F), "V": (0x1F,0x20,0x40,0x20,0x1F),
    "W": (0x3F,0x40,0x38,0x40,0x3F), "X": (0x63,0x14,0x08,0x14,0x63),
    "Y": (0x07,0x08,0x70,0x08,0x07), "Z": (0x61,0x51,0x49,0x45,0x43),
    "[": (0x00,0x7F,0x41,0x41,0x00), "\\": (0x02,0x04,0x08,0x10,0x20),
    "]": (0x00,0x41,0x41,0x7F,0x00), "_": (0x40,0x40,0x40,0x40,0x40),
}


def log(msg):
    print("[OLED] " + str(msg), flush=True)


def load_config():
    try:
        with open(CONFIG_PATH, "r") as fh:
            return json.load(fh)
    except Exception:
        return {}


def parse_addr(value):
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except Exception:
        return 0x3C


def sh(args, timeout=2):
    try:
        cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, timeout=timeout)
        return cp.returncode, cp.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def service_state(name):
    code, out = sh(["systemctl", "is-active", name], 2)
    return out.upper() if code == 0 else (out.upper() if out else "DOWN")


def network_info():
    code, out = sh(["ip", "-4", "-o", "addr", "show", "scope", "global"], 2)
    if code == 0:
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 4 and p[1] != "lo":
                return p[1], p[3].split("/")[0]
    try:
        return "NET", socket.gethostbyname(socket.gethostname())
    except Exception:
        return "NET", "0.0.0.0"


def uptime_text():
    try:
        sec = int(float(open("/proc/uptime").read().split()[0]))
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days:
            return "{}D {:02d}:{:02d}".format(days, hours, mins)
        return "{:02d}:{:02d}".format(hours, mins)
    except Exception:
        return "?"


def memory_text():
    try:
        vals = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            vals[k] = int(v.strip().split()[0])
        total = vals.get("MemTotal", 0) // 1024
        avail = vals.get("MemAvailable", vals.get("MemFree", 0)) // 1024
        used = max(total - avail, 0)
        return "{}/{}M".format(used, total)
    except Exception:
        return "?"


def disk_text():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize / (1024.0 ** 3)
        free = st.f_bavail * st.f_frsize / (1024.0 ** 3)
        return "{:.1f}/{:.1f}G".format(total - free, total)
    except Exception:
        return "?"


def temp_text():
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            val = float(open(path).read().strip())
            if val > 1000:
                val /= 1000.0
            if -20 < val < 150:
                return "{:.0f}C".format(val)
        except Exception:
            pass
    return "N/A"


def short_host(url):
    try:
        h = urlparse(str(url)).hostname
        return h or "?"
    except Exception:
        return "?"


def trim(text, width=21):
    text = str(text).upper()
    return text[:width]


class SSD1306(object):
    def __init__(self, device, address=0x3C, flip=False):
        self.device = device
        self.address = address
        self.fd = os.open(device, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, address)
        self.command([
            0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
            0x8D, 0x14, 0x20, 0x00,
            0xA0 if flip else 0xA1,
            0xC0 if flip else 0xC8,
            0xDA, 0x12, 0x81, 0x7F, 0xD9, 0xF1, 0xDB, 0x40,
            0xA4, 0xA6, 0xAF
        ])
        self.clear()

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass

    def command(self, values):
        for i in range(0, len(values), 16):
            os.write(self.fd, bytes(bytearray([0x00] + values[i:i+16])))

    def data(self, values):
        for i in range(0, len(values), 16):
            os.write(self.fd, bytes(bytearray([0x40] + values[i:i+16])))

    def show(self, buf):
        self.command([0x21, 0, WIDTH - 1, 0x22, 0, PAGES - 1])
        self.data(buf)

    def clear(self):
        self.show(bytearray(WIDTH * PAGES))


def render(lines):
    buf = bytearray(WIDTH * PAGES)
    for row, line in enumerate(lines[:8]):
        x = 0
        for ch in trim(line):
            glyph = FONT.get(ch, FONT["?"])
            for col in glyph:
                if x < WIDTH:
                    buf[row * WIDTH + x] = col
                x += 1
            if x < WIDTH:
                buf[row * WIDTH + x] = 0
            x += 1
            if x >= WIDTH:
                break
    return buf


def build_pages(cfg, dev, addr):
    iface, ip = network_info()
    stream = service_state("orange-pi-zero-camera.service")
    web = service_state("orange-pi-zero-web.service")
    mode = str(cfg.get("input_mode", "rtsp"))
    camera_host = short_host(cfg.get("input_url", "")) if mode == "rtsp" else cfg.get("video_device", "/dev/video0")
    try:
        load = os.getloadavg()[0]
    except Exception:
        load = 0.0

    page1 = [
        "ROBOTLIDAR ORANGE PI",
        "IP " + ip,
        "NET {} {}".format(iface, "UP" if ip != "0.0.0.0" else "DOWN"),
        "WEB 8088 " + web,
        "STREAM " + stream,
        "SERVER " + short_host(cfg.get("server_url", "")),
        "SRT {}MS".format(cfg.get("srt_latency_ms", 200)),
        "ID " + str(cfg.get("device_id", "?")),
    ]

    page2 = [
        "VIDEO SETTINGS",
        "MODE " + mode,
        "CAM " + str(camera_host),
        "RES {}X{}".format(cfg.get("width", "?"), cfg.get("height", "?")),
        "FPS {}".format(cfg.get("fps", "?")),
        "BIT {}K".format(cfg.get("bitrate_kbps", "?")),
        "PTZ " + ("ON" if cfg.get("ptz_enabled", False) else "OFF"),
        "ONVIF " + ("AUTO" if cfg.get("onvif_auto_discovery", False) else "MANUAL"),
    ]

    page3 = [
        "SYSTEM",
        "UP " + uptime_text(),
        "LOAD {:.2f}".format(load),
        "RAM " + memory_text(),
        "DISK " + disk_text(),
        "TEMP " + temp_text(),
        "I2C " + os.path.basename(dev),
        "OLED 0X{:02X}".format(addr),
    ]
    return [page1, page2, page3]


def find_display(cfg):
    addr = parse_addr(cfg.get("display_i2c_address", "0x3c"))
    bus = cfg.get("display_i2c_bus", "auto")
    flip = bool(cfg.get("display_flip", False))
    if str(bus).lower() != "auto":
        candidates = ["/dev/i2c-{}".format(bus)]
    else:
        candidates = sorted(glob.glob("/dev/i2c-*"))
    last = None
    for dev in candidates:
        oled = None
        try:
            oled = SSD1306(dev, addr, flip)
            return oled, dev, addr
        except Exception as exc:
            last = exc
            if oled:
                oled.close()
    raise RuntimeError("SSD1306 not found at 0x{:02X}; buses={}; last={}".format(addr, candidates, last))


def main():
    while True:
        cfg = load_config()
        if not cfg.get("display_enabled", True):
            log("display disabled in config")
            time.sleep(10)
            continue
        oled = None
        try:
            oled, dev, addr = find_display(cfg)
            log("SSD1306 ready: {} address=0x{:02X}".format(dev, addr))
            page = 0
            while True:
                cfg = load_config()
                if not cfg.get("display_enabled", True):
                    oled.clear()
                    break
                pages = build_pages(cfg, dev, addr)
                oled.show(render(pages[page % len(pages)]))
                page += 1
                delay = float(cfg.get("display_rotate_sec", 4.0) or 4.0)
                time.sleep(max(1.0, delay))
        except KeyboardInterrupt:
            if oled:
                oled.clear()
            return
        except Exception as exc:
            log("ERROR: {}".format(exc))
            time.sleep(5)
        finally:
            if oled:
                oled.close()


if __name__ == "__main__":
    main()
