#!/usr/bin/env python3

import rtmidi
import serial
import serial.tools.list_ports
import time
import traceback
import sys
import threading
from enum import Enum
import asyncio

# ─────────────────────────────────────────────
# MIDI CONSTANTS
# ─────────────────────────────────────────────
MIDI_SYSEX = 0xF0
MIDI_SYSEX_TYPE_NON_REALTIME = 0x7E
MIDI_SYSEX_END = 0xF7
MIDI_SYSEX_GENERAL_INFORMATION = 0x06
MIDI_SYSEX_REQUEST_IDENTITY = 0x01
MIDI_SYSEX_REPLY_IDENTITY = 0x02

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
class LogLevel(Enum):
    INFO = 1
    WARN = 2
    ERROR = 3
    DEBUG = 4
    VERBOSE = 5

LOG_LEVELS = [LogLevel.INFO]

def logger(level):
    if level not in LOG_LEVELS:
        return lambda *a: None
    return lambda *a: print(f"[{level.name}]", *a)

info = logger(LogLevel.INFO)
warn = logger(LogLevel.WARN)
error = logger(LogLevel.ERROR)
debug = logger(LogLevel.DEBUG)
verbose = logger(LogLevel.VERBOSE)

# ─────────────────────────────────────────────
class dotdict(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

# ─────────────────────────────────────────────
def logException(e):
    error(e)
    traceback.print_exc()

# ─────────────────────────────────────────────
def serial_set_callback(serial_dev, cb, on_exception):
    stop_event = threading.Event()

    def worker():
        while not stop_event.is_set():
            try:
                data = serial_dev.read(3)
                if data:
                    cb(data)
            except (OSError, serial.SerialException, TypeError):
                break
            

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    return lambda: stop_event.set()

# ─────────────────────────────────────────────
class Serial2Midi:
    def __init__(self, name, baud_rate, sleep_interval, eval_match,
                 manual_device=None, midi_suffix=None, forced_midi_name=None):
        self.name = name
        self.manual_device = manual_device
        self.baud_rate = baud_rate
        self.sleep_interval = sleep_interval
        self.eval_match = eval_match
        self.midi_suffix = midi_suffix
        self.forced_midi_name = forced_midi_name

        self.start_time = time.time()
        self.should_stop = False
        self._interrupt = None

    # ─────────────────────────────────────────
    async def run(self):
        midi_name = (
            self.forced_midi_name
            if self.forced_midi_name
            else f"{self.name}{('-' + self.midi_suffix) if self.midi_suffix else ''}"
        )

        midi_in = rtmidi.MidiIn()
        midi_out = rtmidi.MidiOut()

        midi_in.set_client_name(midi_name)
        midi_out.set_client_name(midi_name)

        midi_in.open_virtual_port(midi_name)
        midi_out.open_virtual_port(midi_name)

        info(f"MIDI device created: {midi_name}")

        while not self.should_stop:
            device_path = self.manual_device

            if not device_path:
                devices = [d async for d in findDevices(self.eval_match)]
                if not devices:
                    info("No serial devices found")
                    time.sleep(self.sleep_interval)
                    continue
                device_path = devices[0].device_path

            info(f"Using serial device: {device_path}")

            def open_serial():
                while not self.should_stop:
                    try:
                        return serial.Serial(
                            device_path,
                            self.baud_rate,
                            timeout=1,
                            exclusive=False if sys.platform == "win32" else True
                        )
                    except serial.SerialException as e:
                        warn(f"Serial open failed: {e}")
                        time.sleep(self.sleep_interval)
                return None

            ser = open_serial()
            if ser is None:
                continue

            interrupt = threading.Event()
            self._interrupt = interrupt.set

            midi_in.set_callback(
                lambda msg, t: self.process_midi_out(msg[0], ser)
            )

            def on_serial_error(e):
                logException(e)
                interrupt.set()

            stop_serial = serial_set_callback(
                ser,
                lambda buf: self.process_serial_in(buf, midi_out),
                on_serial_error
            )

            interrupt.wait()

            stop_serial()
            ser.close()
            time.sleep(self.sleep_interval)

        midi_in.close_port()
        midi_out.close_port()
        info("Exited cleanly")

    # ─────────────────────────────────────────
    def stop(self):
        self.should_stop = True
        if self._interrupt:
            self._interrupt()

    # ─────────────────────────────────────────
    def process_serial_in(self, buf, midi_out):
        if len(buf) == 3:
            info(self.ts(), "[SER → MIDI]", hex(buf[0]), hex(buf[1]), hex(buf[2]))
            midi_out.send_message(buf)

    def process_midi_out(self, buf, ser):
        try:
            if buf:
                info(self.ts(), "[MIDI → SER]", hex(buf[0]), hex(buf[1]), hex(buf[2]))
                ser.write(buf)
        except Exception as e:
            logException(e)
            self.stop()

    def ts(self):
        return round(time.time() - self.start_time, 3)

# ─────────────────────────────────────────────
async def findDevices(eval_match):
    ports = serial.tools.list_ports.comports()

    async def inspect(p):
        return dotdict({
            "device_path": p.device,
            "usb_description": p.product,
            "usb_vid": p.vid,
            "usb_pid": p.pid,
            "usb_manufacturer": p.manufacturer
        })

    tasks = [inspect(p) for p in ports]
    for t in asyncio.as_completed(tasks):
        d = await t
        if eval_match:
            try:
                if not eval(eval_match):
                    continue
            except Exception:
                continue
        yield d

# ─────────────────────────────────────────────
async def listDevices(eval_match):
    async for d in findDevices(eval_match):
        for k, v in d.items():
            print(f"{k}: {v}")
        print("")

# ─────────────────────────────────────────────
async def main():
    import argparse
    parser = argparse.ArgumentParser()

    parser.add_argument('--name', default='Serial2MIDI')
    parser.add_argument('--baud-rate', type=int, default=115200)
    parser.add_argument('--sleep-interval', type=float, default=0.3)
    parser.add_argument('--match', default=None)
    parser.add_argument('--list', action='store_true')

    parser.add_argument(
        '-s', '--serial',
        dest='serial_device',
        default=None,
        help='Manual serial device (/dev/ttyUSB0 or COM3)'
    )

    parser.add_argument('--midi-suffix', default=None)
    parser.add_argument('--force-midi-name', default=None)

    args = parser.parse_args()

    if args.list:
        await listDevices(args.match)
        return

    s2m = Serial2Midi(
        name=args.name,
        baud_rate=args.baud_rate,
        sleep_interval=args.sleep_interval,
        eval_match=args.match,
        manual_device=args.serial_device,
        midi_suffix=args.midi_suffix,
        forced_midi_name=args.force_midi_name
    )

    import signal
    for sig in ('INT', 'TERM', 'HUP'):
        signal.signal(getattr(signal, 'SIG' + sig), lambda *_: s2m.stop())
        

    await s2m.run()

# ─────────────────────────────────────────────
if __name__ == '__main__':
    asyncio.run(main())
