#!/usr/bin/env python3
"""hsb.py - terminal-only LTC4282 (DC2024A-A) hot-swap bench for LTM4707 testing, via a Total Phase Aardvark.

  python hsb.py status | run [-s SECONDS] | off | clear | flash | regs   [--vin 12|5]
  run:   configure, check the supply, zero the current, release the output, time the PGOODs, then
         monitor/log/protect until Ctrl+C or -s seconds (then OFF). Trips latch: use 'clear', then 'run'.
  flash: write these settings as the power-up defaults (output OFF at power-up). Needs JP6 on WRITE.
Only dependency: pip install aardvark_py.  Register numbers are from the LTC4282 datasheet Rev. C.
"""
import argparse, csv, sys, threading, time
from array import array
from datetime import datetime

# ---------------- settings for your board ----------------
ADDR = 0x55                  # 7-bit address: ADR0-2 all H = 0x55, all NC = 0x4A
RSENSE = 0.0003              # ohm, effective (stock DC2024A: 10 x 3 mOhm)
INTERNAL_THRESHOLDS = False  # True once R1, R2, R3, R7, R8 are removed (required for 5 V)
ALERT_TO_ON = False          # True once E12 ALERT is wired to E7 ON (JP2 = FLOAT)
PGOOD = [1, 2]               # GPIOs wired to LTM4707 PGOODs (GPIO1 = E3, GPIO2 = E13, GPIO3 = E14); [] = off
VOUT_GPIO = 3                # GPIO reading LTM4707 Vout through a divider (2 = E13, 3 = E14; remove R4 / R6); None = off
VOUT_RATIO = 4.0             # divider ratio: (3k + 1k) / 1k. Calibrate: ratio * (meter Vout / displayed Vout)
PGOOD_TIMEOUT = 2.0          # s to watch the PGOODs after release
ILIM_CODE = 0                # current limit 12.5 mV + 3.125 mV * code (code 0 = 41.7 A on the stock board)
LIMITS = {  # sw_*: checked here every conversion; hw_*: chip alarm registers (ALERT)
    12: dict(sw_iin=12.0, sw_pin=140.0, sw_vin_min=11.0, sw_vin_max=13.0, hw_iin=15.0, hw_pin=160.0, hw_vin=13.2),
    5: dict(sw_iin=28.0, sw_pin=140.0, sw_vin_min=4.6, sw_vin_max=5.4, hw_iin=30.0, hw_pin=160.0, hw_vin=5.5)}
VOUT_MIN, VOUT_MAX = None, None  # optional Vout trip limits in volts (min is checked 1 s after release)
# ----------------------------------------------------------
say, STOP, on_sample = print, threading.Event(), None       # GUI hooks: output, stop flag, sample callback (hsb_gui.py)
VFS, MODE = {5: 8.32, 12: 16.64}, {5: 1, 12: 2}           # ADC full scale and VIN_MODE code
FAULTS = {0x40: "FET-bad", 0x20: "FET short", 0x08: "power bad", 0x04: "overcurrent", 0x02: "undervoltage", 0x01: "overvoltage"}
ALARMS = {0x80: "power alarm", 0x20: "current alarm", 0x08: "voltage alarm"}
PGOOD = [n for n in PGOOD if n != VOUT_GPIO]               # a pin cannot be both
SIG = [("FET on", 1, 0x80), ("gates >8V", 0, -0x02), ("hot-swap PG", 1, 0x08)] + \
      [(f"PGOOD{n}", 2, {1: 0x20, 2: 0x40, 3: 0x80}[n]) for n in PGOOD]   # name, byte in 0x1D-0x1F, mask (<0 = active low)


def _anchor():  # align a high-resolution clock to a wall-clock tick (time.time() ticks every 15.6 ms on Windows before Python 3.13)
    t = time.time()
    while (n := time.time()) == t: pass
    return n - time.perf_counter()
_OFFSET = _anchor()
def now(): return _OFFSET + time.perf_counter()
def iso(t): return datetime.fromtimestamp(t).isoformat(sep=" ", timespec="microseconds")
def names(v, table): return [n for bit, n in table.items() if v & bit]
def u16(b, i): return b[i] << 8 | b[i + 1]


class Aardvark:
    def __init__(self, khz=100):
        import aardvark_py as aa
        self.aa, self.h = aa, aa.aa_open(0)
        if self.h <= 0: sys.exit(f"cannot open the Aardvark: {aa.aa_status_string(self.h)}")
        aa.aa_configure(self.h, aa.AA_CONFIG_GPIO_I2C); aa.aa_i2c_pullup(self.h, aa.AA_I2C_PULLUP_BOTH)
        aa.aa_target_power(self.h, aa.AA_TARGET_POWER_NONE); self.rate(khz)

    def rate(self, khz): self.aa.aa_i2c_bitrate(self.h, khz)

    def read(self, reg, n):
        aa = self.aa
        st, _ = aa.aa_i2c_write_ext(self.h, ADDR, aa.AA_I2C_NO_STOP, array("B", [reg]))
        st2, data, got = aa.aa_i2c_read_ext(self.h, ADDR, aa.AA_I2C_NO_FLAGS, n)
        if st or st2 or got != n: aa.aa_i2c_free_bus(self.h); raise IOError(f"I2C read 0x{reg:02X} failed ({st}/{st2})")
        return bytes(data)

    def write(self, reg, *vals):
        st, n = self.aa.aa_i2c_write_ext(self.h, ADDR, self.aa.AA_I2C_NO_FLAGS, array("B", [reg, *vals]))
        if st or n != len(vals) + 1: raise IOError(f"I2C write 0x{reg:02X} failed ({st})")


def sample(b, vin):
    r, a, vfs = b.read(0x00, 0x20), b.read(0x34, 0x18), VFS[vin]
    k = 0.04 / 65535 / RSENSE                                       # amps per VSENSE code
    return dict(t=now(), vin=u16(a, 6) * vfs / 65535, iin=u16(a, 12) * k, pin=u16(a, 18) * k * vfs * 65536 / 65535,
                wh=int.from_bytes(r[0x12:0x18], "big") * k * vfs * 0.065535 * 256 / 65535 / 3600,
                vout=u16(a, 0) * 1.28 / 65535 * VOUT_RATIO if VOUT_GPIO else None,      # VGPIO 0x34
                n=int.from_bytes(r[0x18:0x1C], "big"), fault=r[4], adc=r[5] & r[3], alert=r[0x1C] & 0xC0,
                st0=r[0x1E], st1=r[0x1F], on=bool(r[0x1E] & 0x80), pg=bool(r[0x1E] & 8), uv=r[0x1E] & 2, ov=r[0x1E] & 1)


def image(vin):
    """Register image 0x00-0x11 for this profile (RAM now, or EEPROM at +0x20 for power-up)."""
    L, vfs, m = LIMITS[vin], VFS[vin], MODE[vin]
    th = 0b10_01_01 if INTERNAL_THRESHOLDS else 0                   # power-good 10 %, UV 5 %, OV 5 %
    return {0x00: 0xA0, 0x01: th << 2 | m, 0x02: 0, 0x03: 0, 0x04: 0, 0x05: 0, 0x06: 100,  # latch faults, FET off
            0x07: 0x30 if PGOOD else 0, 0x08: 0, 0x09: 0xFF, 0x0A: 0, 0x0C: 0, 0x0E: 0,   # GPIO1 input when timing
            0x0B: max(1, min(255, int(L["hw_vin"] / vfs * 255))),
            0x0D: max(1, min(255, int(L["hw_iin"] * RSENSE * 255 / 0.04))),
            0x0F: max(1, min(255, int(L["hw_pin"] * RSENSE * 65025 / (0.04 * vfs * 256)))),
            0x11: ILIM_CODE << 5 | m << 3 | 0x04 | (0 if VOUT_GPIO == 3 else 0x02)}  # foldback = VIN mode, ADC: SOURCE + GPIO2/3, 12-bit


def off(b):
    b.write(0x02, 0, 0); b.write(0x00, b.read(0x00, 1)[0] & ~0x08)


def clear(b, vin):
    b.write(0x02, 0, 0)
    if ALERT_TO_ON and INTERNAL_THRESHOLDS:   # releasing ALERT raises ON (= turn on): hold gates off with a UV "brake"
        b.write(0x01, image(vin)[0x01] & 0xCC | 0x33); time.sleep(0.01)
        if not b.read(0x1E, 1)[0] & 2: b.write(0x01, image(vin)[0x01]); sys.exit("clear aborted: UV brake did not engage")
    b.write(0x1C, 0); b.write(0x00, 0xA0); b.write(0x04, 0, 0); b.write(0x01, image(vin)[0x01])
    time.sleep(0.08); b.write(0x00, 0xA0); b.write(0x04, 0, 0); b.write(0x1C, 0)
    say("cleared; output OFF")


def check(s, vin, ctx):
    L, causes = LIMITS[vin], names(s["fault"], FAULTS) + names(s["adc"], ALARMS)
    if s["alert"] and not causes: causes.append("alert latched")
    if not s["on"] and not causes: causes.append("MOSFETs switched off")
    iin, pin = s["iin"] - ctx["zero"], s["pin"] - ctx["zero"] * s["vin"]
    if iin > L["sw_iin"]: causes.append(f"Iin {iin:.3f} A > {L['sw_iin']} A")
    if pin > L["sw_pin"]: causes.append(f"Pin {pin:.2f} W > {L['sw_pin']} W")
    if s["vin"] > L["sw_vin_max"]: causes.append(f"Vin {s['vin']:.3f} V > {L['sw_vin_max']} V")
    if now() - ctx["t_on"] > 1.0 and s["vin"] < L["sw_vin_min"]: causes.append(f"Vin {s['vin']:.3f} V < {L['sw_vin_min']} V")
    if VOUT_GPIO and VOUT_MAX is not None and s["vout"] > VOUT_MAX: causes.append(f"Vout {s['vout']:.3f} V > {VOUT_MAX} V")
    if VOUT_GPIO and VOUT_MIN is not None and now() - ctx["t_on"] > 1.0 and s["vout"] < VOUT_MIN: causes.append(f"Vout {s['vout']:.3f} V < {VOUT_MIN} V")
    return causes


def trip(b, causes, log):
    if ALERT_TO_ON: b.write(0x1C, 0x80)      # latch through ALERT -> ON
    b.write(0x00, 0xA0)
    say(f"\n\a*** TRIP {iso(now())}: {'; '.join(causes)}  (output OFF, latched: run 'clear')")
    log("trip", note="; ".join(causes))


def capture(b, vin, pre, t_w0, t_w1, anchor, log, ctx):
    """Time every edge of FET, gates, hot-swap PG and the PGOODs from the release instant."""
    lv = lambda raw: [bool(raw[i] & abs(m)) != (m < 0) for _, i, m in SIG]
    t0 = (t_w0 + t_w1) / 2; w0 = anchor[0] + t0 - anchor[1]; lev = lv(pre)
    low = {i: not lev[i] for i in range(3, len(SIG))}; up, prev, last_edge, last_chk = {}, t0, t0, t0
    say(f"released {iso(w0)}  (+/-{(t_w1 - t_w0) * 500:.2f} ms)"); log("release", t=w0)
    while (tq := time.perf_counter()) < t0 + PGOOD_TIMEOUT:
        now = lv(b.read(0x1D, 3)); tm = (tq + time.perf_counter()) / 2
        for i, v in enumerate(now):
            if v != lev[i]:
                te, ed = (prev + tm) / 2, "rise" if v else "fall"; dt = (te - t0) * 1000; last_edge = te
                say(f"  {iso(w0 + te - t0)}  +{dt:8.3f} ms (+/-{(tm - prev) * 500:.2f})  {SIG[i][0]} {ed}")
                log("edge", t=w0 + te - t0, note=f"{SIG[i][0]} {ed} +{dt:.3f} ms +/-{(tm - prev) * 500:.3f}")
                if i >= 3: low[i] |= not v; up[i] = dt if v and low[i] else None
        lev, prev = now, tm
        if not now[0] and tm - t0 > 0.002: break
        if tm - last_chk > 0.1:              # keep protecting during the capture
            last_chk = tm; s = sample(b, vin)
            if c := check(s, vin, ctx): trip(b, c, log); return False
        if all(now[i] and low[i] for i in low) and now[1] and tm - last_edge > 0.02: break
    for i in low:
        res = f"up at +{up[i]:.3f} ms" if up.get(i) is not None else ("high throughout (no low seen)" if lev[i] else "NOT up")
        say(f"  {SIG[i][0]}: {res}"); log("pgood", note=f"{SIG[i][0]} {res}")
    return True


def run(b, vin, seconds):
    s = sample(b, vin)
    if s["alert"]: sys.exit("an alert is latched: run 'clear' first")
    for reg, val in sorted(image(vin).items()): b.write(reg, val)
    b.write(0x11, image(vin)[0x11] & ~0x04); time.sleep(0.15); vdd = u16(b.read(0x3A, 2), 0) * VFS[vin] / 65535
    b.write(0x11, image(vin)[0x11]); time.sleep(0.15); s = sample(b, vin)
    if s["uv"] or s["ov"]: sys.exit(f"supply {vdd:.3f} V is outside the {vin} V window ({'UV' if s['uv'] else 'OV'})")
    zs, n0 = [], s["n"]
    for _ in range(100):                     # three fresh conversions with the output off -> current zero
        time.sleep(0.02); s = sample(b, vin); zs += [s["iin"]] if s["n"] != n0 else []; n0 = s["n"]
        if len(zs) == 3: break
    else: sys.exit("the ADC is not converting")
    ctx = dict(zero=sum(zs) / 3, t_on=now())
    name = datetime.now().strftime("hsb_%Y%m%d_%H%M%S.csv"); f = open(name, "w", newline=""); w = csv.writer(f)
    w.writerow(["time", "kind", "vin_V", "iin_A", "pin_W", "energy_Wh", "vout_V", "status", "fault_log", "note"])
    def log(kind, s=None, note="", t=None):
        w.writerow([iso(t or now()), kind] + ([f"{s['vin']:.4f}", f"{s['iin'] - ctx['zero']:.4f}", f"{s['pin']:.3f}",
                   f"{s['wh']:.6f}", "" if s["vout"] is None else f"{s['vout']:.4f}", f"0x{s['st0']:02X}{s['st1']:02X}",
                   f"0x{s['fault']:02X}"] if s else [""] * 7) + [note]); f.flush()
    log("start", note=f"{vin} V profile, supply {vdd:.3f} V, zero {ctx['zero'] * 1000:.1f} mA")
    say(f"supply {vdd:.3f} V, zero offset {ctx['zero'] * 1000:.1f} mA, logging to {name}")
    b.write(0x1D, 0x40); b.write(0x1D, 0); b.write(0x04, 0, 0); b.write(0x1C, 0); b.write(0x02, 0x6F, 0xA8)  # arm alerts
    try:
        if PGOOD: b.rate(400); pre = b.read(0x1D, 3); anchor = (now(), time.perf_counter())
        t_w0 = time.perf_counter(); b.write(0x00, 0xA8); t_w1 = time.perf_counter(); ctx["t_on"] = now()
        if PGOOD:
            ok = capture(b, vin, pre, t_w0, t_w1, anchor, log, ctx); b.rate(100)
            if not ok: return 2
        last, errs, end = None, 0, now() + (seconds or 1e12)
        while now() < end and not STOP.is_set():
            time.sleep(0.02)
            try: s = sample(b, vin); errs = 0
            except IOError as e:
                errs += 1
                if errs == 5: say(f"\n*** COMM LOST {iso(now())}: {e} (chip limits still active)"); log("comm", note=str(e))
                continue
            if s["n"] == last: continue
            last = s["n"]; log("sample", s)
            if on_sample and s["pg"]: on_sample(s["t"], s["vin"], s["iin"] - ctx["zero"], s["pin"] - ctx["zero"] * s["vin"], s["vout"] or 0.0)
            if c := check(s, vin, ctx): trip(b, c, log); return 2
            pg = "".join("1" if s["st1"] & SIG[i][2] else "0" for i in range(3, len(SIG)))
            say(f"\r{iso(s['t'])[11:23]}  ON  Vin {s['vin']:7.3f} V  Iin {s['iin'] - ctx['zero']:7.3f} A  "
                  f"Pin {s['pin'] - ctx['zero'] * s['vin']:7.2f} W  " + (f"Vout {s['vout']:6.3f} V  " if VOUT_GPIO else "") +
                  f"{s['wh']:9.5f} Wh  {'PG' if s['pg'] else '--'}  PGOOD {pg or '-'} ", end="")
        say()
    except KeyboardInterrupt:
        say()
    off(b); log("off"); say(f"output OFF {iso(now())}"); f.close()
    return 0


def flash(b, vin):
    for reg, val in sorted(image(vin).items()):
        if b.read(reg + 0x20, 1)[0] == val: continue
        b.write(reg + 0x20, val)
        for _ in range(25):
            if not b.read(0x1F, 1)[0] & 0x08: break   # EEPROM busy
            time.sleep(0.002)
        if b.read(reg + 0x20, 1)[0] != val: sys.exit(f"EEPROM 0x{reg + 0x20:02X} did not take: is JP6 on WRITE?")
        say(f"EE 0x{reg + 0x20:02X} <- 0x{val:02X}")
    say("EEPROM matches the settings")


def status(b, vin):
    s = sample(b, vin)
    say(f"{iso(s['t'])}  output {'ON' if s['on'] else 'OFF'}  Vout(source) {s['vin']:.3f} V  Iin {s['iin']:.3f} A  "
          f"Pin {s['pin']:.2f} W  " + (f"Vout {s['vout']:.3f} V  " if VOUT_GPIO else "") + f"energy {s['wh']:.5f} Wh\n  PG {s['pg']}  UV {bool(s['uv'])}  OV {bool(s['ov'])}  "
          f"alert latched {bool(s['alert'])}  GPIO1-3 {s['st1'] >> 5 & 1}{s['st1'] >> 6 & 1}{s['st1'] >> 7 & 1}  "
          f"faults {names(s['fault'], FAULTS) or 'none'}  alarms {names(s['adc'], ALARMS) or 'none'}")


def main(argv=None, bus=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "run", "off", "clear", "flash", "regs"])
    ap.add_argument("--vin", type=int, choices=[12, 5], default=12)
    ap.add_argument("-s", "--seconds", type=float, default=0, help="run time (default: until Ctrl+C)")
    a = ap.parse_args(argv)
    if a.vin == 5 and not INTERNAL_THRESHOLDS: sys.exit("5 V needs INTERNAL_THRESHOLDS = True (dividers removed)")
    b = bus or Aardvark()
    if a.cmd == "regs":
        d = b.read(0x00, 0x32) + bytes(2) + b.read(0x34, 0x1C)
        for i in range(0, 0x50, 16): say(f"0x{i:02X}: " + " ".join(f"{x:02X}" for x in d[i:i + 16]))
        return 0
    cmds = dict(status=status, off=lambda b, v: off(b) or say("output OFF"), clear=clear, flash=flash,
                run=lambda b, v: run(b, v, a.seconds))
    return cmds[a.cmd](b, a.vin) or 0


if __name__ == "__main__":
    sys.exit(main())
