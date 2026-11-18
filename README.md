# hsb (Kivy window): LTC4282 / DC2024A bench for LTM4707 testing

`hsb_gui.py` is a small Kivy window over `hsb.py`. Keep both in this folder; `hsb.py` does all the bench work.

Windows: install the Total Phase Aardvark USB driver and Python 3.10+, then double-click `run_windows.bat`.
That installs `aardvark_py` and `kivy` the first time, then opens the window. By hand:
`py -m pip install -r requirements.txt` then `py hsb_gui.py`.

The window has:
- a 12/5 V selector;
- an optional run time in seconds;
- the buttons RUN, OFF, CLEAR, FLASH and STATUS.

The top line shows live readings, or the trip cause in red. Below it, a live chart shows Vin, Iin and Pin over the
last 60 s, one autoscaled band each (change `WINDOW` at the top of `hsb_gui.py`). The log pane shows the timed PGOOD edges
and messages.
Closing the window during a run turns the output OFF first.
`hsb.py` also works on its own in a terminal: `py hsb.py status | run | off | clear | flash | regs`.

Settings are at the top of `hsb.py`:
- address 0x55;
- sense resistance;
- limits;
- PGOOD channels;
- the `INTERNAL_THRESHOLDS` and `ALERT_TO_ON` flags.

Each run writes `hsb_YYYYMMDD_HHMMSS.csv` in this folder: samples, µs-timestamped edges, trips and on/off events.
