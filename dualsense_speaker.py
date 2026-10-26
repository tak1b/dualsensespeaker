"""
dualsense_speaker.py - route Windows audio to the DualSense's built-in speaker.

100% local: talks to the controller over USB HID only. No network code,
no updater, no background services, nothing installed besides `hidapi`.

Setup (once):
    pip install hidapi

Double-click the file (or run it with no arguments) to open the interactive
console, where you can type commands like:
    volume 70      boost 3      mode both      status      help      quit

Command-line usage also works:
    python dualsense_speaker.py --volume 70     # 0-100
    python dualsense_speaker.py --boost 3       # extra amp gain 0-7 (louder, may distort)
    python dualsense_speaker.py --both          # headphone jack AND speaker
    python dualsense_speaker.py --off           # back to headphone jack, then exit
    python dualsense_speaker.py --once          # apply once and exit (no keep-alive)

Then in Windows: Settings > System > Sound > Output > pick "Wireless Controller"
(or set it per-app in the Volume mixer).

Note: in speaker mode the controller plays only the RIGHT audio channel.
For full sound, turn on Windows' "Mono audio" (Settings > Accessibility > Audio),
which mixes both channels together. Mono audio affects all devices.

Report layout from the open-source duaLib (DualSenseY, MIT) and the Linux
hid-playstation driver: USB output report 0x02, 64 bytes.
"""

import argparse
import sys
import threading
import time


def pause_and_exit(msg, code=1):
    """Show a message and wait for Enter, so a double-clicked window doesn't vanish."""
    print(msg)
    if len(sys.argv) == 1:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)


def load_hid():
    """Import hidapi, with a clear explanation and an offer to fix it if that fails."""
    import subprocess

    problem = None
    try:
        import hid
        if hasattr(hid, "device") and hasattr(hid, "enumerate"):
            return hid
        problem = ("A different package called 'hid' is installed and is hiding 'hidapi'.\n"
                   "They both use the name 'hid', so only one can be installed.")
        fix = [["uninstall", "-y", "hid"], ["install", "--force-reinstall", "hidapi"]]
    except Exception as e:  # ImportError, or OSError if a DLL is missing
        problem = f"Couldn't load hidapi: {type(e).__name__}: {e}"
        if "Unable to load any of the following libraries" in str(e):
            problem += ("\n(That error comes from a different package called 'hid', "
                        "which needs a separate DLL. 'hidapi' doesn't.)")
            fix = [["uninstall", "-y", "hid"], ["install", "--force-reinstall", "hidapi"]]
        else:
            fix = [["install", "hidapi"]]

    py = sys.executable
    print(problem)
    print(f"\nThis script is running with:\n  {py}")
    if fix == [["install", "hidapi"]]:
        print("pip probably installed hidapi into a different Python. The fix for THIS one is:")
    else:
        print("To fix it for this Python:")
    for args in fix:
        print(f'  "{py}" -m pip {" ".join(args)}')

    if len(sys.argv) == 1:  # interactive / double-clicked
        try:
            answer = input("\nRun that now? (y/n) ").strip().lower()
        except EOFError:
            answer = "n"
        if answer.startswith("y"):
            for args in fix:
                subprocess.call([py, "-m", "pip", *args])
            # Restart the script so the freshly installed module is picked up
            print("\nRestarting...\n")
            sys.exit(subprocess.call([py, *sys.argv]))
    pause_and_exit("\nRun the command above, then start this file again.")


hid = load_hid()

SONY_VID = 0x054C
DUALSENSE_PIDS = {0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}

REPORT_ID_USB = 0x02
REPORT_LEN_USB = 64  # report ID + 63 bytes

# Byte offsets within the report *after* the report ID
FLAGS0, FLAGS1 = 0, 1
VOL_HEADPHONES, VOL_SPEAKER = 4, 5
AUDIO_CONTROL = 7
AUDIO_CONTROL2 = 37

# Flag bits
F0_HEADPHONE_VOL = 1 << 4
F0_SPEAKER_VOL = 1 << 5
F0_AUDIO_CONTROL = 1 << 7
F1_AUDIO_CONTROL2 = 1 << 7

# Output path (AudioControl bits 4-5)
PATH_HEADPHONES = 0        # L+R -> headphone jack
PATH_JACK_AND_SPEAKER = 2  # L -> headphone jack, R -> speaker
PATH_SPEAKER = 3           # R -> speaker only

MODES = {"speaker": PATH_SPEAKER, "both": PATH_JACK_AND_SPEAKER, "jack": PATH_HEADPHONES}
MODE_NAMES = {PATH_SPEAKER: "speaker", PATH_JACK_AND_SPEAKER: "both (jack + speaker)",
              PATH_HEADPHONES: "jack (headphones)"}

# The PS5 uses speaker volume values 0x3D-0x64; 0 makes some firmware jump to max.
SPK_MIN, SPK_MAX = 0x3D, 0x64


def build_report(path: int, volume_pct: int, boost: int) -> bytes:
    payload = bytearray(REPORT_LEN_USB - 1)
    payload[FLAGS0] = F0_AUDIO_CONTROL | F0_SPEAKER_VOL | F0_HEADPHONE_VOL
    payload[FLAGS1] = F1_AUDIO_CONTROL2

    volume_pct = max(0, min(100, volume_pct))
    payload[VOL_SPEAKER] = SPK_MIN + round((SPK_MAX - SPK_MIN) * volume_pct / 100)
    payload[VOL_HEADPHONES] = 0x7F if path != PATH_SPEAKER else 0x00

    payload[AUDIO_CONTROL] = (path & 0b11) << 4
    payload[AUDIO_CONTROL2] = max(0, min(7, boost)) & 0b111

    return bytes([REPORT_ID_USB]) + bytes(payload)


_bt_warned = False


def find_usb_dualsense():
    global _bt_warned
    for dev in hid.enumerate(SONY_VID, 0):
        name = DUALSENSE_PIDS.get(dev["product_id"])
        if not name:
            continue
        bus = dev.get("bus_type")  # newer hidapi: 1 = USB, 2 = Bluetooth
        if bus is not None and bus != 1:
            if not _bt_warned:
                print(f"Found {name} over Bluetooth - the speaker only works over USB. Plug in a data cable.")
                _bt_warned = True
            continue
        return dev, name
    return None, None


def apply(path, volume, boost, quiet=False) -> bool:
    info, name = find_usb_dualsense()
    if not info:
        return False
    dev = hid.device()
    try:
        dev.open_path(info["path"])
        if dev.write(build_report(path, volume, boost)) < 0:
            raise OSError("write failed")
        if not quiet:
            print(f"{name}: audio -> {MODE_NAMES[path]}, volume {volume}%, boost {boost}")
        return True
    except OSError as e:
        if not quiet:
            print(f"Couldn't talk to the controller: {e}")
        return False
    finally:
        dev.close()


# ---------------------------------------------------------------- interactive

HELP = """
Commands:
  volume <0-100>          speaker volume            e.g.  volume 70
  boost <0-7>             extra loudness (may distort)    boost 3
  mode speaker|both|jack  where the sound goes            mode both
  status                  show current settings
  help                    show this list
  quit                    switch back to headphone jack and close
"""


class Keeper:
    """Re-applies the current settings every 2 seconds in the background."""

    def __init__(self):
        self.lock = threading.Lock()
        self.path, self.volume, self.boost = PATH_SPEAKER, 100, 0
        self.connected = False
        self.stop = threading.Event()
        self.wake = threading.Event()

    def settings(self):
        with self.lock:
            return self.path, self.volume, self.boost

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
        self.wake.set()  # apply right away instead of waiting

    def run(self):
        waiting_shown = False
        while not self.stop.is_set():
            path, vol, boost = self.settings()
            ok = apply(path, vol, boost, quiet=True)
            if ok and not self.connected:
                print("\n[controller connected - settings applied]\n> ", end="", flush=True)
                waiting_shown = False
            elif not ok and not waiting_shown:
                msg = "controller disconnected" if self.connected else "waiting for a DualSense on USB"
                print(f"\n[{msg}...]\n> ", end="", flush=True)
                waiting_shown = True
            self.connected = ok
            self.wake.wait(2)
            self.wake.clear()


def interactive():
    print("DualSense speaker (local only). Type 'help' for commands.")
    keeper = Keeper()
    threading.Thread(target=keeper.run, daemon=True).start()

    while True:
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            line = "quit"
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()

        if cmd in ("quit", "exit", "q"):
            keeper.stop.set()
            keeper.wake.set()
            apply(PATH_HEADPHONES, keeper.volume, 0, quiet=True)
            print("Switched back to the headphone jack. Bye!")
            return
        elif cmd in ("help", "?", "h"):
            print(HELP)
        elif cmd in ("status", "s"):
            path, vol, boost = keeper.settings()
            state = "connected" if keeper.connected else "not connected"
            print(f"Mode: {MODE_NAMES[path]} | Volume: {vol}% | Boost: {boost} | Controller: {state}")
        elif cmd in ("volume", "vol", "v"):
            if arg.isdigit() and 0 <= int(arg) <= 100:
                keeper.update(volume=int(arg))
                print(f"Volume set to {arg}%")
            else:
                print("Usage: volume <0-100>")
        elif cmd in ("boost", "b"):
            if arg.isdigit() and 0 <= int(arg) <= 7:
                keeper.update(boost=int(arg))
                print(f"Boost set to {arg}")
            else:
                print("Usage: boost <0-7>")
        elif cmd in ("mode", "m"):
            if arg in MODES:
                keeper.update(path=MODES[arg])
                print(f"Mode set to {MODE_NAMES[MODES[arg]]}")
            else:
                print("Usage: mode speaker | both | jack")
        else:
            print(f"Unknown command '{cmd}'. Type 'help' for the list.")


# ---------------------------------------------------------------- command line

def main():
    if len(sys.argv) == 1:
        interactive()
        return

    ap = argparse.ArgumentParser(description="Send audio to the DualSense speaker (USB, local only).")
    ap.add_argument("--volume", type=int, default=100, help="speaker volume 0-100 (default 100)")
    ap.add_argument("--boost", type=int, default=0, help="extra speaker gain 0-7 (default 0)")
    ap.add_argument("--both", action="store_true", help="play through headphone jack and speaker")
    ap.add_argument("--off", action="store_true", help="switch back to headphone jack and exit")
    ap.add_argument("--once", action="store_true", help="apply once and exit")
    args = ap.parse_args()

    if args.off:
        sys.exit(0 if apply(PATH_HEADPHONES, args.volume, 0) else "No DualSense found on USB.")

    path = PATH_JACK_AND_SPEAKER if args.both else PATH_SPEAKER
    if args.once:
        sys.exit(0 if apply(path, args.volume, args.boost) else "No DualSense found on USB.")

    keeper = Keeper()
    keeper.path, keeper.volume, keeper.boost = path, args.volume, args.boost
    print("Keeping speaker mode on. Press Ctrl+C to stop and switch back to the headphone jack.")
    try:
        keeper.run()
    except KeyboardInterrupt:
        apply(PATH_HEADPHONES, args.volume, 0, quiet=True)
        print("\nSwitched back to the headphone jack.")


if __name__ == "__main__":
    main()