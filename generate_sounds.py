"""
generate_sounds.py — generate WAV tone files for WalaHarp.

Run once before starting the app:
    python3 generate_sounds.py

Produces 8 WAV files in the same directory.
All notes are A minor pentatonic — any combination sounds musical.
"""
import math, wave, struct, os

RATE     = 44100
DURATION = 2.5    # seconds — must match NOTE_DURATION in walaharp.py
ATTACK   = 0.06   # 60 ms linear attack
RELEASE  = 0.80   # 800 ms release
PEAK     = 0.82   # peak amplitude (keeps headroom for mixing)

# A minor pentatonic — two octaves, left (low) → right (high)
NOTES = [
    ('a3', 220.00),
    ('c4', 261.63),
    ('d4', 293.66),
    ('e4', 329.63),
    ('g4', 392.00),
    ('a4', 440.00),
    ('c5', 523.25),
    ('e5', 659.25),
]

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def make_tone(name, freq):
    n = int(RATE * DURATION)
    sustain_end = DURATION - RELEASE

    frames = []
    for i in range(n):
        t = i / RATE

        # Envelope
        if t < ATTACK:
            env = t / ATTACK
        elif t > sustain_end:
            env = max(0.0, (DURATION - t) / RELEASE)
        else:
            env = 1.0
        env *= PEAK

        # Warm harmonic tone: fundamental + overtones + soft sub-octave
        sample = (
            math.sin(2 * math.pi * freq       * t) * 0.60 +
            math.sin(2 * math.pi * freq * 2   * t) * 0.20 +
            math.sin(2 * math.pi * freq * 3   * t) * 0.10 +
            math.sin(2 * math.pi * freq * 0.5 * t) * 0.10   # sub-octave warmth
        )
        frames.append(int(env * sample * 32767))

    path = os.path.join(OUT_DIR, name + '.wav')
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(struct.pack(
            '<%dh' % n,
            *[max(-32768, min(32767, f)) for f in frames]
        ))
    print('  {:<5}  {:>7.2f} Hz  ({:.1f}s)  → {}'.format(
        name, freq, DURATION, os.path.basename(path)))


if __name__ == '__main__':
    print('Generating WalaHarp tones (A minor pentatonic)...')
    for name, freq in NOTES:
        make_tone(name, freq)
    print('Done.')
