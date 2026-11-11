"""Run the currently open U-campus example sentence list, stopping on any failed readback."""
import importlib.util
import os
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("control", HERE / "ucampus-control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
WORK = control.WORK
STATE = WORK / "followread-results.json"
FAST = os.environ.get("UCAMPUS_SKIP_SCORE") == "1"


def ui(bridge=None):
    for attempt in range(3):
        raw = bridge.read_xml() if bridge else control.read_ui()
        (WORK / "latest-ui.xml").write_bytes(raw)
        root = ET.fromstring(raw)
        if any(n.get("text") == "范例学习" for n in root.iter()):
            return root, {c: n for n in root.iter() for c in n}
        time.sleep(0.5)
    raise RuntimeError("Expected example learning page; stopped.")


def bounds(node):
    return list(map(int, re.findall(r"-?\d+", node.get("bounds", ""))))


def target(root, text):
    return next((n for n in root.iter() if n.get("text") == text), None)


def score(node, parents):
    if node is None:
        return None
    for other in parents[node].iter():
        match = re.fullmatch(r"(\d+)分", other.get("text", ""))
        if match:
            return int(match.group(1))
    return None


def expanded(root, parents, text):
    labels = [n for n in root.iter() if n.get("text") in ("录音", "重录")]
    if len(labels) == 1 and text in [n.get("text") for n in parents[labels[0]].iter()]:
        return labels[0]
    return None


def settled_ui(bridge, text):
    if not bridge:
        time.sleep(0.2)
        return ui()
    root, parents = ui(bridge)
    deadline = time.monotonic() + 3
    previous = None
    while time.monotonic() < deadline:
        node = target(root, text)
        current = bounds(node) if node is not None else None
        if current is None:return root,parents
        if current == previous and current is not None:
            return root, parents
        previous = current
        time.sleep(0.2)
        root, parents = ui(bridge)
    raise RuntimeError("Sentence is still moving after scrolling; no tap was sent.")


def wait_expanded(bridge, text):
    deadline = time.monotonic() + 4
    scrolled = False
    previous = None
    while True:
        root, parents = ui(bridge)
        label = expanded(root, parents, text)
        if label is not None:
            position = bounds(label)
            if not bridge or position == previous:
                return root, parents, label
            previous = position
        else:
            previous = None
        node = target(root, text)
        if label is None and bridge and not scrolled and node is not None and bounds(node)[3] > 2400:
            # The last card may expand below the viewport; scroll it once without repeating the click.
            control.adb("shell", "input", "swipe", "1150", "2400", "1150", "1300", "400")
            scrolled = True
            continue
        if time.monotonic() >= deadline:
            evidence = control.snapshot("sentence-expand-error")
            raise RuntimeError(f"Sentence was not expanded as expected; no tap retried. Inspect {evidence}")
        time.sleep(0.15)


def run(bridge=None, start_at=1, sentences=None):
    # Keep the initial complete inventory: the bridge contains only visible nodes.
    display_id=getattr(getattr(bridge,'client',None),'display_id',0)
    if isinstance(display_id,int) and display_id>0 and sentences is None:
        raise ValueError('Secondary followread requires the inspected complete sentence list')
    if sentences is not None and (not isinstance(sentences,list) or not sentences
            or any(not isinstance(text,str) or not re.fullmatch(r"[A-Za-z].*[.!?][\"'”’]?",text) for text in sentences)
            or len(set(sentences))!=len(sentences)):
        raise ValueError('Use a complete list of distinct inspected English sentences')
    root, parents = ui(bridge) if sentences is not None else ui()
    if sentences is None and target(root, "跟读以下句子。") is None:
        raise RuntimeError("Expected sentence repetition step; stopped.")
    if sentences is None:
        sentences = [n.get("text") for n in root.iter() if re.fullmatch(r"[A-Za-z].*[.!?][\"'”’]?", n.get("text", ""))]
    if not sentences:
        raise RuntimeError("No sentences found.")
    if type(start_at) is not int or not 1 <= start_at <= len(sentences):
        raise ValueError("Start index must refer to the current complete sentence inventory.")
    results = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    print(json.dumps({"sentence_count": len(sentences)}, ensure_ascii=False), flush=True)
    for index, text in enumerate(sentences, 1):
        if index < start_at:
            continue
        node = target(root, text)
        existing = None if FAST else score(node, parents)
        if existing and existing > 0:
            entry = results.get(text, {})
            entry.update({"index": index, "score": existing, "observed_existing": True})
            results[text] = entry
            STATE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        txt = WORK / f"speech-{key}.txt"
        wav = WORK / f"speech-{key}.wav"
        txt.write_text(text, encoding="utf-8")
        if not wav.exists():
            subprocess.run(["pwsh.exe", "-NoProfile", "-File", str(HERE / "make-speech.ps1"), "-TextPath", str(txt), "-WavPath", str(wav)], check=True, stdout=subprocess.DEVNULL)
        for attempt in range(12):
            node = target(root, text)
            b = bounds(node) if node is not None else [0, 0, 0, 0]
            # The final collapsed row cannot always scroll above 2400; its full text still fits below 3000.
            if b[0] < b[2] and 700 < (b[1]+b[3])/2 and b[3] <= 3000:
                root,parents=settled_ui(bridge,text)
                node=target(root,text);b=bounds(node) if node is not None else [0,0,0,0]
                if b[0]<b[2] and 700<(b[1]+b[3])/2 and b[3]<=3000:break
            # Current run traverses the list from top to bottom.
            control.adb("shell", "input", "swipe", "1150", "2400", "1150", "1300", "400")
            root, parents = settled_ui(bridge, text)
        else:
            raise RuntimeError(f"Sentence {index} not visible after bounded scrolling.")
        label = expanded(root, parents, text)
        if label is None:
            if bridge:bridge.open_sentence(text)
            else:control.adb("shell", "input", "tap", str((b[0]+b[2])//2), str((b[1]+b[3])//2))
            root, parents, label = wait_expanded(bridge, text)
        b = bounds(label)
        x, y = (b[0]+b[2])//2, (b[1]+b[3])//2 - 187
        if not (600 < y < 2900 and 0 <= b[0] < b[2] <= 1440 and b[1] < b[3] <= 3000):
            raise RuntimeError("Recording button outside verified viewport; stopped.")
        started = time.monotonic()
        if bridge:
            action = bridge.speak_turn(wav, x, y, text)
        else:
            done = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "ucampus-control.py"), "speak-turn", str(wav), str(x), str(y)], check=True, capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            action = json.loads(done.stdout.strip().splitlines()[-1])
        obtained = None
        for attempt in range(0 if FAST else 8):
            root, parents = ui(bridge)
            obtained = score(target(root, text), parents)
            if obtained is not None:
                break
            time.sleep(1)
        result = {"index": index, "score": obtained, "elapsed_seconds": round(time.monotonic()-started, 2), "screenshot": action.get("screenshot") or control.snapshot(f"sentence-{index:02}-result"), "audio": str(wav), "recording_was_active_at_audio_end": action.get("still_recording_after_playback")}
        result.update({key: action[key] for key in ("platform_acceptance", "digital_audio_seconds", "digital_pcm_bytes", "policy_released", "speaker_playback") if key in action})
        results[text] = result
        STATE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("index", "elapsed_seconds")} if FAST else result, ensure_ascii=False), flush=True)
        if not FAST and (obtained is None or obtained <= 0):
            raise RuntimeError(f"Sentence {index} has no positive score; inspect before continuing.")
        if FAST:
            root, parents = ui(bridge)
    print(f"All sentences recorded; verify next stage. Details: {STATE}" if FAST else "All sentences have positive application scores; next stage remains manual.", flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-at", type=int, default=1, help="First pending sentence, confirmed from current task evidence")
    run(start_at=parser.parse_args().start_at)
