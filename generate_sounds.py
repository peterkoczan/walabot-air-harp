"""
generate_sounds.py — generate WAV tone files for WalaHarp.

Run once before starting the app:
    python3 generate_sounds.py

Produces 8 WAV files in the same directory.
All notes are A minor pentatonic — any combination sounds musical.
"""
import math, wave, struct, os

RATE      = 44100
DURATION  = 3.0    # seconds — WAV length; NOTE_DURATION in walaharp.py controls looping
ATTACK    = 0.008  # 8 ms sharp linear attack — percussive pluck feel
PEAK      = 0.30   # peak amplitude; 4 simultaneous notes sum to ≤1.0 in practice
# Exponential decay: note falls to exp(-DECAY_RATE) ≈ 5% at end of DURATION
# This gives a natural harp/handpan pluck: loud on hit, fades smoothly over ~1-2 s
DECAY_RATE = 3.0

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
    n          = int(RATE * DURATION)
    decay_span = DURATION - ATTACK   # seconds over which exponential runs

    frames = []
    for i in range(n):
        t = i / RATE

        # Envelope: sharp attack, then natural exponential decay.
        # Sounds like a plucked harp string: bright on the hit, decays over ~1-2 s.
        if t < ATTACK:
            env = t / ATTACK                                          # 0 → 1 ramp
        else:
            env = math.exp(-DECAY_RATE * (t - ATTACK) / decay_span)  # 1 → ~0.05

        env *= PEAK

        # Warm harmonic tone: fundamental + 2nd/3rd overtones
        # No sub-octave — at high notes it falls in the audible range and distorts
        sample = (
            math.sin(2 * math.pi * freq     * t) * 0.70 +
            math.sin(2 * math.pi * freq * 2 * t) * 0.22 +
            math.sin(2 * math.pi * freq * 3 * t) * 0.08
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
    print('  {:<5}  {:>7.2f} Hz  ({:.1f}s decay)  → {}'.format(
        name, freq, DURATION, os.path.basename(path)))


if __name__ == '__main__':
    print('Generating WalaHarp tones (A minor pentatonic, exponential decay)...')
    for name, freq in NOTES:
        make_tone(name, freq)
    print('Done.')
