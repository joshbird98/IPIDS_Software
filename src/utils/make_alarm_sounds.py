"""Write the HMI's alarm sounds (src/gui/control/assets/sounds/*.wav): one distinct pattern per
alarm priority, as ISA-18.2 suggests, so the operator can tell from the sound alone how urgent
it is. Plain sine tones, 16-bit mono; the HMI loops them until the alarm is acknowledged or
silenced. Run again to regenerate: python src/utils/make_alarm_sounds.py"""
import math
import os
import struct
import wave

RATE = 22050
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "gui", "control", "assets", "sounds"))

# name -> [(frequency Hz or 0 for silence, seconds), ...]
PATTERNS = {
    # critical: fast two-tone (high-low), urgent
    "alarm_critical.wav": [(1400, 0.18), (950, 0.18)] * 4 + [(0, 0.2)],
    # high: three beeps, pause
    "alarm_high.wav": [(1050, 0.15), (0, 0.1)] * 3 + [(0, 0.9)],
    # medium: one soft two-note chime, long pause
    "alarm_medium.wav": [(880, 0.25), (660, 0.35), (0, 1.6)],
}


def tone(freq: float, seconds: float, volume: float = 0.5):
    n = int(RATE * seconds)
    fade = int(RATE * 0.01)                 # 10 ms ramps: no clicks
    for i in range(n):
        if not freq:
            yield 0
            continue
        env = min(1.0, i / fade, (n - i) / fade) if fade else 1.0
        yield int(32767 * volume * env * math.sin(2 * math.pi * freq * i / RATE))


def write(path: str, pattern):
    samples = [s for freq, seconds in pattern for s in tone(freq, seconds)]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def main(out_dir: str = OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    for name, pattern in PATTERNS.items():
        write(os.path.join(out_dir, name), pattern)
    return sorted(PATTERNS)


if __name__ == "__main__":
    for name in main():
        print(f"wrote {os.path.join(OUT_DIR, name)}")
