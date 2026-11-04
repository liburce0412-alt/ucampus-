"""Small, explicit ADB and Windows speech controls for the supervised walkthrough."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time
import wave
import io
from PIL import Image
import xml.etree.ElementTree as ET

from runtime import WORK, adb, assert_recording_profile, audio_device
WORK.mkdir(parents=True, exist_ok=True)


def play_audio(wav):
    """Honor the selected endpoint instead of the mutable Windows default."""
    import numpy as np
    import sounddevice as sd
    with wave.open(str(wav), 'rb') as f:
        if f.getsampwidth() != 2:
            raise ValueError('Expected PCM16 WAV')
        rate = f.getframerate()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').reshape(-1, f.getnchannels())
    sd.play(data, rate, device=audio_device('OUTPUT'), blocking=True)


def snapshot(label, png=None):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = WORK / f"{stamp}-{label}.png"
    path.write_bytes(png if png is not None else adb("exec-out", "screencap", "-p"))
    return str(path)


def read_ui():
    result = adb("shell", "uiautomator", "dump", "/sdcard/ucampus-ui.xml")
    if b"dumped to" not in result:
        raise RuntimeError("UI dump did not complete; refusing to read a stale XML file. Use a screenshot while audio is playing.")
    return adb("shell", "cat", "/sdcard/ucampus-ui.xml")


def recording_at(png, x, y):
    # Observed U-campus recording button: purple centre with a green timer ring.
    # Rejects the idle blue microphone and the red retry button.
    crop = Image.open(io.BytesIO(png)).convert("RGB").crop((x-85, y-90, x+85, y+90))
    pixels = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
    purple = green = 0
    for r, g, b in pixels:
        purple += 120 < r < 230 and g < 165 and b > 180
        green += r < 150 and g > 170 and b < 210
    return purple > 1000 and green > 200


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("inspect")
    sub.add_parser("launch")
    tap = sub.add_parser("tap")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    swipe = sub.add_parser("swipe")
    for name in ("x1", "y1", "x2", "y2"):
        swipe.add_argument(name, type=int)
    sub.add_parser("back")
    play = sub.add_parser("play")
    play.add_argument("wav", type=Path)
    speak = sub.add_parser("speak-turn")
    speak.add_argument("wav", type=Path)
    speak.add_argument("x", type=int)
    speak.add_argument("y", type=int)
    hold = sub.add_parser("hold-turn")
    hold.add_argument("wav", type=Path)
    hold.add_argument("x", type=int)
    hold.add_argument("y", type=int)
    args = parser.parse_args()
    started = time.monotonic()
    event = {"time": datetime.datetime.now().astimezone().isoformat(), **vars(args)}
    try:
        if args.action in ('play', 'speak-turn', 'hold-turn'):
            import sounddevice as sd
            output = audio_device('OUTPUT')
            with wave.open(str(args.wav.resolve(strict=True)), 'rb') as f:
                sd.check_output_settings(device=output, channels=f.getnchannels(), samplerate=f.getframerate(), dtype='int16')
            event['audio_output'] = {'index': output, 'name': sd.query_devices(output)['name']}
        if args.action == "inspect":
            raw = read_ui()
            (WORK / "latest-ui.xml").write_bytes(raw)
            event["nodes"] = [{"text": n.get("text"), "bounds": n.get("bounds")} for n in ET.fromstring(raw).iter() if n.get("text")]
        elif args.action == "launch":
            component = adb("shell", "cmd", "package", "resolve-activity", "--brief", "cn.unipus.cloud").decode().strip().splitlines()[-1]
            if "/" not in component:
                raise RuntimeError("U-campus launcher activity was not resolved.")
            adb("shell", "am", "start", "-n", component)
            time.sleep(1.5)
        elif args.action == "tap":
            adb("shell", "input", "tap", str(args.x), str(args.y))
            time.sleep(0.5)
        elif args.action == "swipe":
            adb("shell", "input", "swipe", *[str(getattr(args, n)) for n in ("x1", "y1", "x2", "y2")], "450")
            time.sleep(0.5)
        elif args.action == "back":
            adb("shell", "input", "keyevent", "4")
            time.sleep(0.5)
        elif args.action == "play":
            wav = args.wav.resolve(strict=True)
            play_audio(wav)
        elif args.action == "hold-turn":
            wav = args.wav.resolve(strict=True)
            event["before"] = snapshot("before-hold")
            adb("shell", "input", "motionevent", "DOWN", str(args.x), str(args.y))
            try:
                time.sleep(0.4)
                raw = read_ui()
                texts = [n.get("text", "") for n in ET.fromstring(raw).iter()]
                if not any("松开" in t for t in texts):
                    raise RuntimeError("Hold-to-record state was not detected; did not play audio.")
                play_audio(wav)
                time.sleep(0.2)
            finally:
                adb("shell", "input", "motionevent", "UP", str(args.x), str(args.y))
        elif args.action == "speak-turn":
            before_png = adb("exec-out", "screencap", "-p")
            assert_recording_profile(before_png)
            wav = args.wav.resolve(strict=True)
            event["before"] = snapshot("before-speech", before_png)
            adb("shell", "input", "tap", str(args.x), str(args.y))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                png = adb("exec-out", "screencap", "-p")
                recording_xy = next(((x,y) for x,y in [(args.x,args.y),(Image.open(io.BytesIO(png)).width//2,args.y)] if recording_at(png,x,y)), None)
                if recording_xy:
                    ready = WORK / f"recording-{time.time_ns()}.png"
                    ready.write_bytes(png)
                    event["recording_ready"] = str(ready)
                    event["recording_xy"] = recording_xy
                    break
                time.sleep(0.15)
            else:
                raise RuntimeError("Recording state was not detected; audio was not played.")
            play_audio(wav)
            time.sleep(0.2)
            png = adb("exec-out", "screencap", "-p")
            event["still_recording_after_playback"] = recording_at(png, *recording_xy)
            if event["still_recording_after_playback"]:
                adb("shell", "input", "tap", *map(str,recording_xy))
        event["screenshot"] = snapshot(args.action)
        event["status"] = "executed-unverified"
    except Exception as error:
        event["status"] = "error"
        event["error"] = str(error)
        raise
    finally:
        event["elapsed_seconds"] = round(time.monotonic() - started, 3)
        with (WORK / "actions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        print(json.dumps(event, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
