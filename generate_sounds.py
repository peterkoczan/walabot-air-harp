"""
generate_sounds.py — generate WAV tone files for WalaHarp.

Run once before starting the app:
    python3 generate_sounds.py

Produces 8 WAV files in the same directory.
All notes are A minor pentatonic — any combination sounds musical.

Sound design
------------
Envelope: 8 ms linear attack, then exponential decay to ~5 % over 3 s.
          Sounds like a plucked harp string / handpan hit.

Harmonics: each overtone decays at its own rate.
           Higher harmonics fade faster → bright on the hit, warm as it sustains.
           Fundamental: normal rate
           2nd harmonic: 1.5× faster
           3rd harmonic: 2.5× faster
           4th harmonic: 4× faster  (brief attack 'click', then gone)
           This is how real struck / plucked instruments behave.
"""
import math, wave, struct, os

RATE       = 44100
DURATION   = 3.0    # seconds — must match NOTE_DURATION logic in walaharp.py
ATTACK     = 0.008  # 8 ms sharp attack (percussive hit feel)
PEAK       = 0.30   # peak amplitude; ≤4 simultaneous notes stay below clip in practice

# Exponential decay: fundamental falls to exp(-BASE_DECAY) ≈ 5 % at end of DURATION.
# Higher harmonics multiply this rate (see HARM_RATES below).
BASE_DECAY = 3.0

# (frequency_multiple, amplitude_weight, decay_multiplier)
# weights need not sum to 1 — normalised by the sum below
_H = [
    (1.0, 0.70, 1.0),   # fundamental — slowest decay, stays warm
    (2.0, 0.22, 1.5),   # octave — fades 1.5× faster
    (3.0, 0.08, 2.5),   # 5th above octave — fades 2.5× faster
    (4.0, 0.04, 4.0),   # 2nd octave — very fast (bright click at attack only)
]
_TOTAL_WEIGHT = sum(w for _, w, _ in _H)

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
    n         = int(RATE * DURATION)
    h_span    = DURATION - ATTACK   # seconds over which exponential runs

    frames = []
    for i in range(n):
        t   = i / RATE
        h_t = max(0.0, t - ATTACK)   # time elapsed since attack ended

        # Linear attack ramp (all harmonics together)
        attack_factor = min(1.0, t / ATTACK)

        # Sum harmonics with per-harmonic exponential decay
        sample = 0.0
        for mult, weight, decay_mult in _H:
            h_env    = math.exp(-BASE_DECAY * decay_mult * h_t / h_span)
            h_weight = weight / _TOTAL_WEIGHT
            sample  += math.sin(2 * math.pi * freq * mult * t) * h_weight * h_env

        frames.append(int(attack_factor * PEAK * sample * 32767))

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
    print('Generating WalaHarp tones (A minor pentatonic, differential harmonic decay)...')
    for name, freq in NOTES:
        make_tone(name, freq)
    print('Done.')
