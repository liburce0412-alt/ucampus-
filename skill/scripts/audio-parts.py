"""Transcribe saved or growing PCM16 WAV chunks with one resident local model.

Read-only to the source WAV and phone. --watch exits on its stop marker or deadline.
Overlapping segments retain original timestamps and are not deduplicated.
"""
import argparse
import json
import time
import wave
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel


def read_part(path, start, seconds):
    with wave.open(str(path), 'rb') as f:
        rate, channels = f.getframerate(), f.getnchannels()
        if f.getsampwidth() != 2:
            raise ValueError('Expected PCM16 WAV; use scrcpy --audio-codec=raw')
        f.setpos(int(start * rate))
        raw = f.readframes(int(seconds * rate))
    frame_bytes = 2 * channels
    raw = raw[:len(raw) // frame_bytes * frame_bytes]
    if not raw:
        return np.empty(0, dtype=np.float32)
    data = np.frombuffer(raw, dtype='<i2').astype(np.float32).reshape(-1, channels).mean(axis=1) / 32768
    if rate != 16000:
        data = np.interp(np.arange(0, len(data), rate / 16000), np.arange(len(data)), data).astype(np.float32)
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('wav', type=Path)
    p.add_argument('--start', type=float, default=0)
    p.add_argument('--seconds', type=float, default=25, help='Chunk length, 5..90 seconds')
    p.add_argument('--overlap', type=float, default=3)
    p.add_argument('--watch', action='store_true')
    p.add_argument('--stop-file', type=Path, help='Default: same stem .asr-stop; stop recorder separately')
    p.add_argument('--max-seconds', type=float, default=900)
    p.add_argument('--output', type=Path, help='Fresh JSONL; default same stem .parts-TIMESTAMP.jsonl')
    a = p.parse_args()
    if not (a.start >= 0 and 5 <= a.seconds <= 90 and 0 <= a.overlap < a.seconds / 2 and 1 <= a.max_seconds <= 900):
        p.error('Invalid interval or deadline')
    source = a.wav.resolve(strict=True)
    output = a.output or source.with_name(f'{source.stem}.parts-{time.time_ns()}.jsonl')
    stop = a.stop_file or source.with_suffix('.asr-stop')
    started = time.monotonic()
    model = WhisperModel('base', device='cpu', compute_type='int8', local_files_only=True)
    position = a.start
    emitted_end = a.start
    with output.open('x', encoding='utf-8') as log:
        while True:
            finish = not a.watch or stop.exists() or time.monotonic() - started >= a.max_seconds
            data = read_part(source, position, a.seconds)
            duration = len(data) / 16000
            if a.watch and duration < a.seconds - .01 and not finish:
                time.sleep(.5)
                continue
            if position + duration <= emitted_end + .1:
                break
            segments, _ = model.transcribe(data, language='en', beam_size=3, vad_filter=True, condition_on_previous_text=False)
            row = {'start': position, 'seconds': duration, 'segments': [
                {'start': position+s.start, 'end': position+s.end, 'text': s.text} for s in segments
            ]}
            line = json.dumps(row, ensure_ascii=False)
            log.write(line + '\n'); log.flush()
            print(line, flush=True)
            emitted_end = position + duration
            if not a.watch or (finish and duration < a.seconds - .01):
                break
            position += a.seconds - a.overlap
    print(json.dumps({'transcript': str(output), 'audio_through': emitted_end, 'elapsed_seconds': round(time.monotonic()-started, 2)}), flush=True)


if __name__ == '__main__':
    main()
