"""hsb_gui.py - minimal Kivy window for hsb.py (keep both files in one folder).  pip install kivy aardvark_py"""
import os, threading, time
os.environ.setdefault("KIVY_NO_ARGS", "1")
import hsb
from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, Line
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

SERIES = (("Vin", "V", "40d060", 0.2), ("Iin", "A", "50a0ff", 0.5), ("Pin", "W", "ffa040", 5.0), ("Vout", "V", "c080ff", 0.05))
SERIES, WINDOW = SERIES[:4 if hsb.VOUT_GPIO else 3], 60.0  # name, unit, color, min span; seconds shown


class Chart(Widget):  # rolling strip chart, one autoscaled band per trace
    pts = []

    def redraw(self):
        t1 = self.pts[-1][0] if self.pts else 0.0
        self.pts = pts = [p for p in self.pts if p[0] > t1 - WINDOW]
        self.canvas.clear(); h, ranges = self.height / len(SERIES), []
        with self.canvas:
            for i, (name, unit, hexc, mins) in enumerate(SERIES):
                ys = [p[i + 1] for p in pts] or [0.0]; lo, hi = min(ys), max(ys); span = max(hi - lo, mins); y0 = self.top - (i + 1) * h
                Color(.35, .35, .35); Line(rectangle=(self.x, y0 + 2, self.width, h - 4))
                Color(*(int(hexc[j:j + 2], 16) / 255 for j in (0, 2, 4)))
                Line(points=[c for p, y in zip(pts, ys) for c in (self.right - self.width * (t1 - p[0]) / WINDOW,
                                                                   y0 + 8 + (h - 16) * (y - (lo + hi - span) / 2) / span)], width=1.3)
                ranges.append(f"[color={hexc}]{name} {lo:.3f}-{hi:.3f} {unit}[/color]")
        return "   ".join(ranges) + f"   (last {WINDOW:.0f} s, autoscaled)"


class HSB(App):
    title = "LTC4282 hot-swap bench"

    def build(self):
        self.out, self.live, self.alarm, self.busy, self.bus = [], "", "", False, None
        hsb.say = lambda s="", end="\n": setattr(self, "live", s.strip()) if end == "" else self.out.append(str(s).replace("\a", ""))
        root, bar = BoxLayout(orientation="vertical", padding=8, spacing=6), BoxLayout(size_hint_y=None, height=52, spacing=6)
        self.vin = Spinner(text="12", values=["12", "5"], size_hint_x=0.4)
        self.secs = TextInput(hint_text="run seconds", multiline=False, input_filter="float")
        for w in (self.vin, self.secs): bar.add_widget(w)
        for n, c in (("RUN", (0, .65, .2, 1)), ("OFF", (.85, .1, .1, 1)), ("CLEAR", (.9, .6, 0, 1)), ("FLASH", (.45, .45, .45, 1)), ("STATUS", (.2, .4, .85, 1))):
            bar.add_widget(Button(text=n, bold=True, background_normal="", background_color=c, on_press=lambda _, n=n: self.cmd(n.lower())))
        self.head = Label(font_size="17sp", font_name="RobotoMono-Regular", size_hint_y=None, height=70, markup=True, halign="center")
        self.head.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        self.log = TextInput(readonly=True, font_name="RobotoMono-Regular", font_size="13sp", size_hint_y=0.45)
        self.chart, self.scale = Chart(size_hint_y=0.55), Label(size_hint_y=None, height=24, markup=True, font_size="13sp")
        hsb.on_sample = lambda *v: self.chart.pts.append(v)
        for w in (bar, self.head, self.scale, self.chart, self.log): root.add_widget(w)
        Clock.schedule_interval(self.refresh, 0.1)
        return root

    def refresh(self, _):
        new = []
        while self.out: new.append(self.out.pop(0))
        self.alarm = next((x.strip() for x in reversed(new) if "TRIP" in x or "COMM LOST" in x), self.alarm)
        if new: self.log.text += "\n".join(new) + "\n"; self.log.cursor = self.log.get_cursor_from_index(len(self.log.text))
        self.head.text = f"[color=ff3030][b]{self.alarm}[/b][/color]" if self.alarm else self.live or ("working..." if self.busy else "idle")
        self.scale.text = self.chart.redraw()

    def cmd(self, name):
        if name == "off" and self.busy: return hsb.STOP.set()
        if self.busy: return self.out.append("busy: press OFF first")
        self.alarm, self.busy = "" if name in ("run", "clear") else self.alarm, True
        if name == "run": self.chart.pts = []
        args = [name, "--vin", self.vin.text] + (["-s", self.secs.text] if name == "run" and self.secs.text else [])
        threading.Thread(target=self.work, args=(args,), daemon=True).start()

    def work(self, args):
        hsb.STOP.clear()
        try: self.bus = self.bus or hsb.Aardvark(); hsb.main(args, self.bus)
        except SystemExit as e: self.out.append(f"stopped: {e.code}") if e.code not in (None, 0) else None
        except Exception as e: self.out.append(f"error: {e}"); self.bus = None
        finally: self.busy, self.live = False, ""

    def on_stop(self):  # closing the window during a run turns the output OFF first
        hsb.STOP.set()
        for _ in range(60):
            if not self.busy: break
            time.sleep(0.05)


if __name__ == "__main__":
    HSB().run()
