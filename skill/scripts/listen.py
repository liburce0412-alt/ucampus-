"""Bounded microphone capture around one explicit phone playback action, then local transcription."""
import argparse
import json
import queue
from pathlib import Path
import subprocess
import time
import wave

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from runtime import WORK, adb, audio_device


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="action", required=True)
    transcribe = s.add_parser("transcribe")
    transcribe.add_argument("wav", type=Path)
    record = s.add_parser("capture")
    record.add_argument("seconds", type=float)
    record.add_argument("x", type=int)
    record.add_argument("y", type=int)
    continuous = s.add_parser("record", help="Save continuously without tapping; create same-stem .stop to finish.")
    continuous.add_argument("wav", type=Path)
    continuous.add_argument("--seconds", type=float, default=600)
    a = p.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if a.action == "record":
        if not 1 <= a.seconds <= 900:
            raise ValueError("Recording must be bounded to 1..900 seconds.")
        wav = a.wav.resolve()
        wav.parent.mkdir(parents=True, exist_ok=True)
        stop = wav.with_suffix(".stop")
        ready = wav.with_suffix(".ready.json")
        if wav.exists() or stop.exists() or ready.exists():
            raise ValueError("Use a fresh recording path to preserve previous evidence.")
        chunks = queue.Queue()
        def on_audio(data, frames, timing, status):
            chunks.put((bytes(data), str(status) if status else None))
        sd.check_input_settings(device=audio_device("INPUT"), channels=1, dtype="int16", samplerate=16000)
        started = time.monotonic()
        byte_count = 0
        peak = 0.0
        errors = []
        with wav.open("xb") as disk, wave.open(disk, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            with sd.RawInputStream(device=audio_device("INPUT"), samplerate=16000, channels=1, dtype="int16", callback=on_audio):
                while time.monotonic()-started < a.seconds and not stop.exists():
                    raw, status = chunks.get(timeout=3)
                    writer.writeframes(raw)
                    disk.flush()
                    byte_count += len(raw)
                    peak = max(peak, float(np.max(np.abs(np.frombuffer(raw, dtype=np.int16).astype(np.float32))))/32768)
                    if status:
                        errors.append(status)
                    if byte_count >= 16000 and not ready.exists():
                        if errors:
                            raise RuntimeError(f"Capture health check failed: {errors}")
                        info = {"ready": True, "audio": str(wav), "bytes_saved": byte_count, "device": audio_device("INPUT"), "stop_file": str(stop)}
                        ready.write_text(json.dumps(info), encoding="utf-8")
                        print(json.dumps(info), flush=True)
        result = {"audio": str(wav), "seconds": byte_count/32000, "peak": peak, "capture_errors": errors, "stopped_by_marker": stop.exists()}
        wav.with_suffix(".capture.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        return
    if a.action == "capture":
        if not 1 <= a.seconds <= 180:
            raise ValueError("Capture must be between 1 and 180 seconds.")
        wav = WORK / f"listening-{time.time_ns()}.wav"
        chunks = []
        def callback(data, frames, timing, status):
            if status:
                print(str(status), flush=True)
            chunks.append(bytes(data))
        sd.check_input_settings(device=audio_device("INPUT"), channels=1, dtype="int16", samplerate=16000)
        with sd.RawInputStream(device=audio_device("INPUT"), samplerate=16000, channels=1, dtype="int16", callback=callback):
            time.sleep(0.3)
            adb("shell", "input", "tap", str(a.x), str(a.y))
            time.sleep(a.seconds)
        raw = b"".join(chunks)
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(raw)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        print(json.dumps({"audio": str(wav), "seconds": len(data)/16000, "rms": float(np.sqrt(np.mean(data*data))), "peak": float(np.abs(data).max())}), flush=True)
    else:
        wav = a.wav.resolve(strict=True)
    started = time.monotonic()
    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    segments, info = model.transcribe(str(wav), language="en", beam_size=3, vad_filter=True, condition_on_previous_text=False)
    result = {"audio": str(wav), "segments": [{"start": t.start, "end": t.end, "text": t.text, "no_speech_prob": t.no_speech_prob} for t in segments]}
    result["transcription_seconds"] = round(time.monotonic()-started, 2)
    wav.with_suffix(".transcript.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
