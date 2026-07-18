#!/usr/bin/env python3
import spidev
import time
import numpy as np

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1000000
spi.mode = 0b00

def crc8(data24):
    crc = 0xFF
    for i in range(23, -1, -1):
        bit = (data24 >> i) & 0x01
        temp = crc & 0x80
        if bit == 0x01:
            temp ^= 0x80
        crc = (crc << 1) & 0xFF
        if temp > 0:
            crc ^= 0x1D
    return (~crc) & 0xFF

def build_frame(data24):
    c = crc8(data24)
    return list(data24.to_bytes(3, 'big') + bytes([c]))

SW_RESET     = [0xB4, 0x00, 0x20, 0x98]
CHANGE_MODE1 = [0xB4, 0x00, 0x00, 0x1F]
READ_STATUS  = [0x18, 0x00, 0x00, 0xE5]
READ_ACC_X   = [0x04, 0x00, 0x00, 0xF7]
READ_ACC_Y   = [0x08, 0x00, 0x00, 0xFD]
READ_ACC_Z   = [0x0C, 0x00, 0x00, 0xFB]

def nop_frame():
    return build_frame(0x000000)

def xfer(cmd, label=None):
    r = spi.xfer2(list(cmd))
    if label:
        print(f"{label}: {[hex(b) for b in r]}")
    return r

def startup():
    xfer(SW_RESET, "SW_RESET")
    time.sleep(0.005)
    xfer(CHANGE_MODE1, "MODE1")
    time.sleep(0.02)
    xfer(READ_STATUS)
    xfer(READ_STATUS)
    r = xfer(READ_STATUS)
    print(f"RS after startup: {r[0] & 0x03:02b}\n")

def decode(r):
    raw = (r[1] << 8) | r[2]
    if raw & 0x8000:
        raw -= 65536
    return raw / 2700.0

def read_axis(cmd):
    xfer(cmd)
    time.sleep(0.001)
    r = xfer(nop_frame())
    return decode(r)

# ---------------- Noise / drift removal settings ----------------
DEADBAND_G = 0.02

# Per-axis baseline adaptation speed (higher = snaps back to 0 faster)
BASELINE_ALPHA = {
    'x': 0.02,
    'y': 0.02,
    'z': 0.08,   # faster for Z since it usually has bigger swings
}
# ------------------------------------------------------------------

startup()

print("Initializing baseline... keep sensor still for a moment")
baseline = {'x': 0.0, 'y': 0.0, 'z': 0.0}
init_samples = {'x': [], 'y': [], 'z': []}
for _ in range(30):
    init_samples['x'].append(read_axis(READ_ACC_X))
    init_samples['y'].append(read_axis(READ_ACC_Y))
    init_samples['z'].append(read_axis(READ_ACC_Z))
    time.sleep(0.01)
baseline['x'] = np.mean(init_samples['x'])
baseline['y'] = np.mean(init_samples['y'])
baseline['z'] = np.mean(init_samples['z'])
print(f"Initial baseline -> X:{baseline['x']:+.3f}g Y:{baseline['y']:+.3f}g Z:{baseline['z']:+.3f}g")
print("Ready. Move the sensor - it should return to 0.00 whenever it stops, in ANY orientation.\n")

def process(axis, new_value):
    alpha = BASELINE_ALPHA[axis]
    baseline[axis] = (1 - alpha) * baseline[axis] + alpha * new_value
    deviation = new_value - baseline[axis]
    if abs(deviation) < DEADBAND_G:
        return 0.000
    return deviation

start_time = time.time()
while True:
    t = time.time() - start_time

    gx = read_axis(READ_ACC_X)
    gy = read_axis(READ_ACC_Y)
    gz = read_axis(READ_ACC_Z)

    sx = process('x', gx)
    sy = process('y', gy)
    sz = process('z', gz)

    print(f"t={t:.1f}s  X={sx:+.3f}g  Y={sy:+.3f}g  Z={sz:+.3f}g")
    time.sleep(0.2)
