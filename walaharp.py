"""
walaharp.py — Walabot air harp: wave hands above the sensor to play harmonic tones.

Handpan layout — 8 oval tone fields on a circular drum body.
Notes are A minor pentatonic; any combination sounds musical.

  Left hand:   A3 (near outer-L) · D4 (near inner-L) · G4 (far inner-L) · C5 (far outer-L)
  Right hand:  C4 (near inner-R) · E4 (near outer-R) · A4 (far inner-R) · E5 (far outer-R)

Each wave triggers a percussive pluck that decays naturally (~3 s).
Keep your hand gently moving in a zone to loop the note seamlessly.
Multiple zones play simultaneously — any combination is in tune.

Run generate_sounds.py once first to create the WAV files.
"""
from __future__ import print_function, division
import os, math, subprocess, signal, platform, threading, wave, struct, time, collections
import WalabotAPI as wlbt
try:
    import numpy as _np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False
try:
    import tkinter as tk
except ImportError:
    import Tkinter as tk

# Reap zombie subprocesses on Linux so audio never blocks
if hasattr(signal, 'SIGCHLD'):
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

# ── Audio ─────────────────────────────────────────────────────────────────────
_DIR    = os.path.dirname(os.path.abspath(__file__))
_SYSTEM = platform.system()


def _wav(name):
    return os.path.join(_DIR, name + '.wav')


# ── Karplus-Strong synthesis ──────────────────────────────────────────────────
KS_MODE = True   # True = real-time KS pluck; False = WAV file fallback

# Base frequencies for each pad ID (A minor pentatonic, two octaves)
_NOTE_FREQ = {
    'a3': 220.00, 'c4': 261.63, 'd4': 293.66, 'e4': 329.63,
    'g4': 392.00, 'a4': 440.00, 'c5': 523.25, 'e5': 659.25,
}

_KS_CACHE = {}   # {freq: numpy int16 array of PCM samples}


def _ks_pluck(freq, rate=44100, duration=3.0, peak=0.25):
    """Generate a Karplus-Strong plucked-string PCM buffer (numpy int16)."""
    N   = max(2, round(rate / freq))   # delay line length = one period
    n   = int(rate * duration)          # total output samples
    rng = _np.random.default_rng()
    buf = list(rng.uniform(-1.0, 1.0, N))   # initialise with white noise
    # Decay per sample tuned so the note fades to ~5 % amplitude in `duration` s.
    # With lowpass averaging (0.5 × sum) each pass, decay≈0.996 gives a natural
    # harp pluck that sustains for ~3 s and becomes inaudible by the end.
    decay = 0.996
    out = _np.empty(n, dtype=_np.float64)
    idx = 0
    for i in range(n):
        out[i] = buf[idx]
        ni = (idx + 1) % N
        buf[idx] = 0.5 * decay * (buf[idx] + buf[ni])
        idx = ni
    max_amp = _np.max(_np.abs(out)) or 1.0
    return (out * (peak / max_amp) * 32767).astype(_np.int16)


def _ks_ensure(freq):
    """Pre-generate and cache KS buffer for freq if not already cached."""
    if freq not in _KS_CACHE and _NUMPY_OK:
        _KS_CACHE[freq] = _ks_pluck(freq)


class _KSStream:
    """Plays back a pre-generated Karplus-Strong buffer with pan support."""

    def __init__(self, freq):
        if freq not in _KS_CACHE:
            _ks_ensure(freq)
        self._data  = _KS_CACHE[freq]
        self._total = len(self._data)
        self._pos   = 0
        self.pitch  = 1.0
        self.pan_l  = 0.707
        self.pan_r  = 0.707

    def read(self, n_out):
        pos = self._pos
        if pos >= self._total:
            return b'', True
        end = min(pos + n_out, self._total)
        chunk = self._data[pos:end]
        self._pos = end
        if len(chunk) < n_out:
            return chunk.tobytes(), True
        return chunk.tobytes(), False

    def remaining_ms(self, rate):
        return max(0, self._total - self._pos) / rate * 1000


# Shared frame data for each WAV path — loaded once, reused across _WavStream instances
_WAV_FRAME_CACHE = {}


class _WavStream:
    """Pre-loaded WAV with fractional-position interpolation for real-time pitch shift."""

    def __init__(self, path):
        if path not in _WAV_FRAME_CACHE:
            with wave.open(path, 'rb') as wf:
                n = wf.getnframes()
                _WAV_FRAME_CACHE[path] = struct.unpack('<%dh' % n, wf.readframes(n))
        self._frames = _WAV_FRAME_CACHE[path]
        self._total  = len(self._frames)
        self._pos    = 0.0
        self.pitch   = 1.0   # >1 = faster/higher, <1 = slower/lower
        self.pan_l   = 0.707  # constant-power pan; default = centre
        self.pan_r   = 0.707

    def read(self, n_out):
        """Return (data_bytes, exhausted).  Interpolates at fractional positions."""
        frames = self._frames
        total  = self._total
        pos    = self._pos
        pitch  = self.pitch
        out    = []
        for _ in range(n_out):
            i = int(pos)
            if i >= total - 1:
                self._pos = pos
                return struct.pack('<%dh' % len(out), *out), True
            frac = pos - i
            out.append(int(frames[i] + frac * (frames[i + 1] - frames[i])))
            pos += pitch
        self._pos = pos
        return struct.pack('<%dh' % n_out, *out), False

    def remaining_ms(self, rate):
        frames_left = max(0, self._total - 1 - int(self._pos))
        return frames_left / max(0.01, self.pitch) / rate * 1000


class _Mixer:
    """Single persistent aplay + in-process PCM mixer for simultaneous tones."""
    RATE              = 44100
    CHUNK             = 256   # ~6 ms per chunk
    ECHO_DELAY_CHUNKS = 55    # ~320 ms reverb delay
    ECHO_GAIN         = 0.18  # echo amplitude (single-tap delay, no buildup)

    def __init__(self):
        self._streams  = []
        self._lock     = threading.Lock()
        # Echo buffer stores stereo interleaved samples (2×CHUNK per entry)
        self._echo_buf = collections.deque(
            ([0] * (self.CHUNK * 2) for _ in range(self.ECHO_DELAY_CHUNKS)),
            maxlen=self.ECHO_DELAY_CHUNKS)
        self._proc    = subprocess.Popen(
            ['aplay', '-q', '-t', 'raw', '-f', 'S16_LE',
             '-r', str(self.RATE), '-c', '2', '-'],
            stdin=subprocess.PIPE,
            bufsize=0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._run, daemon=True).start()

    def play(self, path, pan_l=0.707, pan_r=0.707):
        """Create a _WavStream for path, add to mix, return the stream object."""
        try:
            stream = _WavStream(path)
            stream.pan_l = pan_l
            stream.pan_r = pan_r
            with self._lock:
                self._streams.append(stream)
            return stream
        except Exception:
            return None

    def play_ks(self, freq, pan_l=0.707, pan_r=0.707):
        """Create a _KSStream for freq, add to mix, return stream."""
        try:
            stream = _KSStream(freq)
            stream.pan_l = pan_l
            stream.pan_r = pan_r
            with self._lock:
                self._streams.append(stream)
            return stream
        except Exception:
            return None

    def set_pan(self, stream, pan_l, pan_r):
        """Update stereo pan for a live stream (thread-safe)."""
        if stream is None:
            return
        with self._lock:
            if stream in self._streams:
                stream.pan_l = pan_l
                stream.pan_r = pan_r

    def stop(self, stream):
        """Remove a stream from the mix immediately (safe if already finished)."""
        if stream is None:
            return
        with self._lock:
            try:
                self._streams.remove(stream)
            except ValueError:
                pass

    def remaining_ms(self, stream):
        """Milliseconds of audio left in stream at its current pitch rate."""
        if stream is None:
            return 0
        with self._lock:
            try:
                return stream.remaining_ms(self.RATE) if stream in self._streams else 0
            except Exception:
                return 0

    def set_pitch(self, stream, factor):
        """Update the playback-rate factor for a live stream (thread-safe)."""
        if stream is None:
            return
        with self._lock:
            if stream in self._streams:
                stream.pitch = max(0.5, min(2.0, factor))

    def _run(self):
        """Real-time paced stereo loop."""
        silence  = b'\x00' * self.CHUNK * 4   # CHUNK stereo S16_LE pairs
        interval = self.CHUNK / self.RATE
        while True:
            t0 = time.monotonic()

            with self._lock:
                alive, items = [], []
                for stream in self._streams:
                    data, done = stream.read(self.CHUNK)
                    if data:
                        items.append((data, stream.pan_l, stream.pan_r))
                    if not done:
                        alive.append(stream)
                self._streams = alive

            # Mix streams into separate L/R accumulators
            out_l = [0] * self.CHUNK
            out_r = [0] * self.CHUNK
            for data, pan_l, pan_r in items:
                for i, s in enumerate(
                        struct.unpack('<%dh' % (len(data) // 2), data)):
                    out_l[i] += int(s * pan_l)
                    out_r[i] += int(s * pan_r)

            # Interleave L/R, then apply echo on stereo output
            interleaved = [x for pair in zip(out_l, out_r) for x in pair]
            echo_frame  = self._echo_buf[0]
            self._echo_buf.append(list(interleaved))
            for i in range(self.CHUNK * 2):
                interleaved[i] += int(echo_frame[i] * self.ECHO_GAIN)

            if any(interleaved):
                peak = max(abs(s) for s in interleaved)
                if peak > 32767:
                    factor = 32767.0 / peak
                    interleaved = [int(s * factor) for s in interleaved]
                buf = struct.pack('<%dh' % (self.CHUNK * 2),
                                  *[max(-32768, min(32767, s)) for s in interleaved])
            else:
                buf = silence

            try:
                self._proc.stdin.write(buf)
            except (BrokenPipeError, OSError):
                break

            elapsed   = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)


# ── Optional MIDI output ───────────────────────────────────────────────────────
# Requires mido + python-rtmidi.  Gracefully disabled if unavailable.
_MIDI_OK   = False
_midi_port = None
try:
    import mido as _mido
    _midi_port = _mido.open_output('WalaHarp', virtual=True)
    _MIDI_OK   = True
except Exception:
    pass

# MIDI note number and channel (0-indexed) per pad — MPE style (ch 0-7 = MIDI 1-8)
_PAD_MIDI_NOTE = {
    'a3': 57, 'd4': 62, 'g4': 67, 'c5': 72,   # left hand
    'c4': 60, 'e4': 64, 'a4': 69, 'e5': 76,   # right hand
}
_PAD_MIDI_CH = {pid: idx for idx, pid in enumerate(
    ['a3', 'd4', 'g4', 'c5', 'c4', 'e4', 'a4', 'e5'])}  # ch 0-7

# ── Platform audio dispatch ───────────────────────────────────────────────────
if _SYSTEM == 'Linux':
    _mixer = _Mixer()
    def _play(path, pan_l=0.707, pan_r=0.707):    return _mixer.play(path, pan_l, pan_r)
    def _play_ks(freq, pan_l=0.707, pan_r=0.707): return _mixer.play_ks(freq, pan_l, pan_r)
    def _stop(wf):                                 _mixer.stop(wf)
    def _remaining_ms(wf):                         return _mixer.remaining_ms(wf)
elif _SYSTEM == 'Darwin':
    def _play(path, pan_l=0.707, pan_r=0.707):
        subprocess.Popen(['afplay', path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    def _play_ks(freq, pan_l=0.707, pan_r=0.707): return None
    def _stop(wf):                                 pass
    def _remaining_ms(wf):                         return 0
else:
    def _play(path, pan_l=0.707, pan_r=0.707):    return None
    def _play_ks(freq, pan_l=0.707, pan_r=0.707): return None
    def _stop(wf):                                 pass
    def _remaining_ms(wf):                         return 0

# ── Note / pad definitions ────────────────────────────────────────────────────
# Handpan alternating-hand layout (ascending L→R across the scale):
#
#   LEFT  hand: A3 (outer-L near) · D4 (inner-L near) · G4 (inner-L far) · C5 (outer-L far)
#   RIGHT hand: C4 (inner-R near) · E4 (outer-R near) · A4 (inner-R far) · E5 (outer-R far)
#
# Outer phi zones (phi_idx 0 and 3) sit at ±60° where the antenna pattern
# has less gain.  boost compensates so all pads feel equally responsive.
# FAR_BOOST handles the R^4 depth penalty separately.
#
# (id, label, r_idx, phi_idx, idle_color, active_color, wav, boost)
PADS = [
    # Left hand — ascending near→far: A3 · D4 · G4 · C5
    ('a3', 'A3', 0, 0, '#18082e', '#8844cc', _wav('a3'), 1.8),  # outer-left  near
    ('d4', 'D4', 0, 1, '#082818', '#22aa66', _wav('d4'), 1.0),  # inner-left  near
    ('g4', 'G4', 1, 1, '#281800', '#cc7700', _wav('g4'), 1.0),  # inner-left  far  (moved from outer — fixes over-boost)
    ('c5', 'C5', 1, 0, '#002828', '#22cccc', _wav('c5'), 1.8),  # outer-left  far

    # Right hand — ascending near→far: C4 · E4 · A4 · E5
    ('c4', 'C4', 0, 2, '#081828', '#2288bb', _wav('c4'), 1.0),  # inner-right near
    ('e4', 'E4', 0, 3, '#182808', '#88cc22', _wav('e4'), 1.8),  # outer-right near
    ('a4', 'A4', 1, 2, '#280018', '#dd3388', _wav('a4'), 1.0),  # inner-right far
    ('e5', 'E5', 1, 3, '#1c1800', '#ddcc22', _wav('e5'), 2.5),  # outer-right far
]

# ── Stereo pan: constant-power L/R gains per phi zone ────────────────────────
# Angles in [0°,90°] where 0°=full-left, 90°=full-right.
# outer-left→18°, inner-left→36°, inner-right→54°, outer-right→72°
_PHI_PAN_DEG = [18.0, 36.0, 54.0, 72.0]


def _phi_pan(phi_idx):
    a = math.radians(_PHI_PAN_DEG[phi_idx])
    return math.cos(a), math.sin(a)   # (pan_l, pan_r)


# ── Detection / sustain constants ─────────────────────────────────────────────
ENERGY_THRESHOLD        = 200   # adjustable via slider (lower = more sensitive)
RELEASE_THRESHOLD_RATIO = 0.75  # hysteresis: release at 75% of trigger threshold
BAR_MAX                 = 1200  # energy level that maxes out the glow
EMA_ALPHA               = 0.4   # EMA smoothing factor per zone per frame

# Far zone boost — signal attenuates ~R^4; hands at 90 cm return far less energy
FAR_BOOST        = 5.0

# Note sustain / retrigger:
# WAV files are DURATION=3.0 s long (exponential decay — see generate_sounds.py).
# NOTE_DURATION controls the retrigger window: a note is eligible to loop only if
# the pad was detected within the last NOTE_DURATION seconds.
# NOTE_DURATION < WAV DURATION so a single brief wave plays the note ONCE (3 s),
# then fades — it will NOT loop.  Continuous hand movement keeps pad_last_active
# fresh → note seamlessly retriggers at the end → sustained note.
NOTE_DURATION    = 2.5   # seconds — retrigger window (< WAV 3.0 s prevents auto-loop)
LOOP_MS          = 20    # ms between Walabot frames (~50 fps)
NOTE_FRAMES      = 150   # visual glow frames  (3.0 s ÷ 20 ms)
RETRIGGER_FRAMES = 6     # safety fallback (unused when rem_ms path works)


# ── Walabot arena ─────────────────────────────────────────────────────────────
R_MIN, R_MAX, R_RES             = 20, 90, 5   # 20–90 cm: hands low → hands raised high
PHI_MIN, PHI_MAX, PHI_RES       = -60, 60, 3
THETA_MIN, THETA_MAX, THETA_RES = -20, 20, 5   # wide range for theta expression

# ── Canvas geometry ───────────────────────────────────────────────────────────
CW, CH = 700, 470

# Drum body centre and semi-axes
DCX, DCY = CW // 2, CH // 2 - 15   # 350, 220
DRX, DRY = 290, 185

# Oval tone-field radii and sizes
# Two concentric rings: near (inner) and far (outer)
_R_NEAR = 105   # px from drum centre to near-ring oval centre
_R_FAR  = 188   # px from drum centre to far-ring oval centre
_NRX, _NRY = 34, 21   # near oval semi-axes
_FRX, _FRY = 43, 26   # far  oval semi-axes

# Angle (deg, standard math: 0°=right, +ve counterclockwise) for each note
# Near ring: 4 ovals at ±60° / ±120°
# Far ring:  4 ovals at ±30° / ±150°  (interleaved with near ring)
_OVAL_ANGLE = {
    'a3': 240,   # near outer-left  (bottom-left)
    'd4': 120,   # near inner-left  (top-left)
    'c4':  60,   # near inner-right (top-right)
    'e4': 300,   # near outer-right (bottom-right)
    'g4': 150,   # far  inner-left  (upper-left outer)
    'c5': 210,   # far  outer-left  (lower-left outer)
    'a4':  30,   # far  inner-right (upper-right outer)
    'e5': 330,   # far  outer-right (lower-right outer)
}


def _oval_centre(pid, r_idx):
    """Return screen (x, y) for the centre of a tone-field oval."""
    r   = _R_FAR if r_idx == 1 else _R_NEAR
    ang = math.radians(_OVAL_ANGLE[pid])
    return (int(DCX + r * math.cos(ang)),
            int(DCY - r * math.sin(ang)))


def _blend(c1, c2, t):
    """Linearly blend two '#rrggbb' colours by factor t (0=c1, 1=c2)."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return '#%02x%02x%02x' % (
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


# ── App ────────────────────────────────────────────────────────────────────────
class HarpApp(tk.Frame):

    def __init__(self, master):
        tk.Frame.__init__(self, master, bg='#0a0a0a')
        # Per-pad: countdown in frames until the current note ends naturally
        self.pad_countdown = {p[0]: 0    for p in PADS}
        self.pad_hits      = {p[0]: 0    for p in PADS}
        # Track the current wave object per pad so we can stop it cleanly
        # before retriggering (prevents two copies of the same freq interfering)
        self.pad_wavobj    = {p[0]: None for p in PADS}
        self.cycleId       = None
        self.r_ranges      = None
        self.phi_ranges    = None
        self.threshold     = ENERGY_THRESHOLD
        self.mode          = '2-HAND'  # '1-HAND' or '2-HAND'
        # Timestamp of last frame where each pad was above threshold.
        # Retrigger is allowed for NOTE_DURATION seconds after last detection,
        # making notes immune to any duration of radar interference from a
        # second hand — no hold-window expiry, just a real-time clock check.
        self.pad_last_active = {p[0]: 0.0 for p in PADS}
        # Schmitt trigger state: True while pad is considered "active" (hysteresis)
        self.pad_is_active   = {p[0]: False for p in PADS}
        # Energy from the previous frame — used for velocity gate
        self.prev_energies   = {p[0]: 0.0   for p in PADS}
        # EMA-smoothed energies (alpha=EMA_ALPHA) — reduces single-frame jitter
        self.ema_energies    = {p[0]: 0.0   for p in PADS}
        # Per-pad elevation angle from GetSensorTargets() — drives pitch shift
        self.pad_theta       = {p[0]: 0.0   for p in PADS}
        # MIDI state
        self.midi_enabled    = False
        self.midi_note_on    = {p[0]: False for p in PADS}
        self.midi_throttle   = 0   # frame counter — send CC every 4 frames (~80 ms)
        self.debug_mode = False
        self.energy_ids = {}

        self.statusVar = tk.StringVar(value='Connecting...')
        tk.Label(self, textvariable=self.statusVar, font='TkFixedFont 9',
                 bg='#0a0a0a', fg='#888888', anchor=tk.W
                 ).pack(fill=tk.X, padx=6, pady=(4, 0))

        self.canvas = tk.Canvas(self, width=CW, height=CH,
                                bg='#0a0a0a', highlightthickness=0)
        self.canvas.pack(padx=8, pady=4)

        self._build_canvas()
        self._build_controls()

        self.after(200, self._connect_walabot)

    # ── Canvas ────────────────────────────────────────────────────────────────

    def _build_canvas(self):
        c = self.canvas
        self.poly_ids  = {}
        self.label_ids = {}

        # ── Drum body ──────────────────────────────────────────────────────────
        # Outer glow ring
        c.create_oval(DCX - DRX - 8, DCY - DRY - 8,
                      DCX + DRX + 8, DCY + DRY + 8,
                      fill='#080808', outline='#1a1a1a', width=4)
        # Main drum shell
        c.create_oval(DCX - DRX, DCY - DRY,
                      DCX + DRX, DCY + DRY,
                      fill='#0c0c0c', outline='#2e2e2e', width=2)
        # Inner decorative ring (simulates the central "ding" area of a handpan)
        c.create_oval(DCX - 58, DCY - 40,
                      DCX + 58, DCY + 40,
                      fill='#101010', outline='#1e1e1e', width=1)

        # Title
        c.create_text(DCX, DCY + DRY + 22,
                      text='W A L A H A R P',
                      fill='#303030', font='TkFixedFont 9 bold', anchor=tk.CENTER)

        # ── Hand indicator lights (update dynamically in loop()) ──────────────
        # Left hand — glows white when left side has energy
        lhx, lhy = DCX - DRX + 22, DCY
        c.create_oval(lhx - 14, lhy - 22, lhx + 14, lhy + 22,
                      fill='#0f0f0f', outline='#2a2a2a', width=1)
        self.hand_left_id = c.create_text(
            lhx, lhy, text='LEFT\nHAND', fill='#252525',
            font='TkFixedFont 7 bold', anchor=tk.CENTER, justify=tk.CENTER)

        # Right hand
        rhx, rhy = DCX + DRX - 22, DCY
        c.create_oval(rhx - 14, rhy - 22, rhx + 14, rhy + 22,
                      fill='#0f0f0f', outline='#2a2a2a', width=1)
        self.hand_right_id = c.create_text(
            rhx, rhy, text='RIGHT\nHAND', fill='#252525',
            font='TkFixedFont 7 bold', anchor=tk.CENTER, justify=tk.CENTER)

        # Depth labels (near / far)
        c.create_text(DCX, DCY - _R_NEAR + 6,
                      text='· near ·', fill='#1a1a1a',
                      font='TkFixedFont 7', anchor=tk.CENTER)
        c.create_text(DCX, DCY - _R_FAR + 8,
                      text='· far ·', fill='#1a1a1a',
                      font='TkFixedFont 7', anchor=tk.CENTER)

        # ── Oval tone fields ───────────────────────────────────────────────────
        for pid, label, r_idx, phi_idx, col_idle, col_active, wav, boost in PADS:
            cx, cy = _oval_centre(pid, r_idx)
            rx = _FRX if r_idx == 1 else _NRX
            ry = _FRY if r_idx == 1 else _NRY

            # Rim: slightly larger oval gives a raised-dome look
            c.create_oval(cx - rx - 3, cy - ry - 3,
                          cx + rx + 3, cy + ry + 3,
                          fill='#151515', outline='#2a2a2a', width=1)
            # Main tone field — fill updated each frame by loop()
            oval_id = c.create_oval(cx - rx, cy - ry,
                                    cx + rx, cy + ry,
                                    fill=col_idle, outline='#484848', width=1)
            self.poly_ids[pid] = (oval_id, col_idle, col_active)

            # Note label
            self.label_ids[pid] = c.create_text(
                cx, cy, text=label, fill='#aaaaaa',
                font='TkFixedFont 10 bold', anchor=tk.CENTER)
            # Debug energy readout — hidden until 'd' key pressed
            self.energy_ids[pid] = c.create_text(
                cx, cy + 12, text='', fill='#ffcc44',
                font='TkFixedFont 7', anchor=tk.CENTER)

    def _build_controls(self):
        bar = tk.Frame(self, bg='#0a0a0a')
        bar.pack(fill=tk.X, padx=8, pady=(0, 6))

        tk.Label(bar, text='SENSITIVITY', font='TkFixedFont 7',
                 bg='#0a0a0a', fg='#555').pack(side=tk.LEFT, padx=(0, 4))
        self.threshVar = tk.IntVar(value=ENERGY_THRESHOLD)
        self.threshVar.trace_add('write', self._on_threshold_change)
        tk.Scale(bar, from_=30, to=800, orient=tk.HORIZONTAL,
                 variable=self.threshVar, showvalue=True,
                 bg='#0a0a0a', fg='#888888', troughcolor='#1a1a1a',
                 activebackground='#444', highlightthickness=0,
                 font='TkFixedFont 7', length=440, sliderlength=12,
                 bd=0).pack(side=tk.LEFT)

        # MIDI toggle button — always visible; greyed if mido unavailable
        self.midiVar = tk.StringVar(value='MIDI OFF')
        self.midiBtn = tk.Button(bar, textvariable=self.midiVar,
                  font='TkFixedFont 8 bold',
                  bg='#1a1a1a', fg='#555555',
                  activebackground='#2a2a2a', activeforeground='#888888',
                  relief=tk.FLAT, bd=1, padx=6,
                  state=tk.NORMAL if _MIDI_OK else tk.DISABLED,
                  command=self._toggle_midi)
        self.midiBtn.pack(side=tk.RIGHT, padx=(4, 0))

        self.modeVar = tk.StringVar(value='2-HAND')
        self.modeBtn = tk.Button(bar, textvariable=self.modeVar,
                  font='TkFixedFont 8 bold',
                  bg='#1a2a1a', fg='#44cc44', activebackground='#2a3a2a',
                  activeforeground='#66ff66', relief=tk.FLAT, bd=1,
                  padx=8, command=self._toggle_mode)
        self.modeBtn.pack(side=tk.RIGHT, padx=(4, 0))

        tk.Button(bar, text='RESET', font='TkFixedFont 8',
                  bg='#1a1a1a', fg='#888888', activebackground='#333',
                  activeforeground='#ffffff', relief=tk.FLAT, bd=1,
                  padx=8, command=self._reset).pack(side=tk.RIGHT, padx=(8, 0))

    # ── Walabot ───────────────────────────────────────────────────────────────

    def _on_threshold_change(self, *args):
        try:
            self.threshold = self.threshVar.get()
        except Exception:
            pass

    def _toggle_mode(self):
        if self.mode == '2-HAND':
            self.mode = '1-HAND'
            self.modeVar.set('1-HAND')
            self.modeBtn.config(bg='#1a1a2a', fg='#4488cc',
                                activebackground='#2a2a3a',
                                activeforeground='#66aaff')
        else:
            self.mode = '2-HAND'
            self.modeVar.set('2-HAND')
            self.modeBtn.config(bg='#1a2a1a', fg='#44cc44',
                                activebackground='#2a3a2a',
                                activeforeground='#66ff66')

    def _reset(self):
        for pid in self.pad_hits:
            self.pad_hits[pid] = 0

    def _toggle_debug(self, _event=None):
        self.debug_mode = not self.debug_mode
        if not self.debug_mode:
            for pid in self.energy_ids:
                self.canvas.itemconfig(self.energy_ids[pid], text='')

    def _toggle_midi(self, _event=None):
        if not _MIDI_OK:
            return
        self.midi_enabled = not self.midi_enabled
        if self.midi_enabled:
            self.midiVar.set('MIDI ON')
            self.midiBtn.config(bg='#1a2a1a', fg='#44cc44',
                                activebackground='#2a3a2a', activeforeground='#66ff66')
        else:
            self.midiVar.set('MIDI OFF')
            self.midiBtn.config(bg='#1a1a1a', fg='#555555',
                                activebackground='#2a2a2a', activeforeground='#888888')
            # Send NoteOff for any sustained MIDI notes
            for pid in self.midi_note_on:
                if self.midi_note_on[pid]:
                    ch = _PAD_MIDI_CH[pid]
                    _midi_port.send(_mido.Message('note_off', channel=ch,
                                                  note=_PAD_MIDI_NOTE[pid], velocity=0))
                    self.midi_note_on[pid] = False

    def _toggle_ks(self, _event=None):
        global KS_MODE
        KS_MODE = not KS_MODE
        label = 'KS' if KS_MODE else 'WAV'
        self.statusVar.set('Synthesis: {} mode'.format(label))

    def _init_walabot(self):
        wlbt.Init()
        wlbt.SetSettingsFolder()
        wlbt.ConnectAny()
        wlbt.SetProfile(wlbt.PROF_SENSOR)
        wlbt.SetArenaR(R_MIN, R_MAX, R_RES)
        wlbt.SetArenaPhi(PHI_MIN, PHI_MAX, PHI_RES)
        wlbt.SetArenaTheta(THETA_MIN, THETA_MAX, THETA_RES)
        # MTI (Moving Target Indicator) = temporal derivative filter.
        # Detects MOVEMENT, not static presence — a still hand fades to zero.
        # This is correct for the harp: wave your hands through zones to play;
        # keep hands gently moving to sustain; stop moving → note fades out.
        wlbt.SetDynamicImageFilter(wlbt.FILTER_TYPE_MTI)
        wlbt.SetThreshold(35)
        wlbt.Start()

    def _connect_walabot(self):
        """Try to connect; on failure schedule a retry rather than crashing."""
        try:
            self._init_walabot()
            self.after(200, self.start_scan)
        except wlbt.WalabotError as e:
            self.statusVar.set(
                'Walabot not found (code {}) — retrying in 3 s…'.format(e.code))
            self.after(3000, self._connect_walabot)

    def start_scan(self):
        self.statusVar.set('Warming up — keep hands away from sensor...')
        # Pre-generate KS buffers while warming up (8 notes, ~100 ms each)
        if KS_MODE and _NUMPY_OK:
            self.statusVar.set('Warming up — generating KS buffers...')
            for pid, _, _, _, _, _, _, _ in PADS:
                _ks_ensure(_NOTE_FREQ[pid])
        # 30 warm-up triggers so MTI builds a stable background reference
        for _ in range(30):
            wlbt.Trigger()
        wlbt.Trigger()
        res = wlbt.GetRawImageSlice()
        sX, sY = res[1], res[2]

        # R zones: skip 1 bin at near/far boundary as dead zone
        mid = sX // 2
        self.r_ranges = [range(0, mid), range(mid + 1, sX)]

        # Phi zones: 4 equal quarters, 1-2 bins dead zone between each
        q = sY // 4
        self.phi_ranges = [
            range(0,         q - 1),
            range(q + 1,   2*q - 1),
            range(2*q + 1, 3*q - 1),
            range(3*q + 1, sY),       # full outer-right (no roll strip)
        ]

        self.statusVar.set('Ready — wave hands through zones to play  ·  keep moving to sustain')
        self.prev_energies = {p[0]: 0.0 for p in PADS}
        self.ema_energies  = {p[0]: 0.0 for p in PADS}
        self.cycleId = self.after(LOOP_MS, self.loop)

    def loop(self):
        wlbt.Trigger()
        res    = wlbt.GetRawImageSlice()
        img    = res[0]
        thresh = self.threshold

        # ── Theta via target tracking ─────────────────────────────────────────
        # GetSensorTargets() may return 0 targets with narrow theta — graceful fallback.
        try:
            for t in wlbt.GetSensorTargets():
                # t.yPosCm = phi (deg), t.xPosCm = R (cm), t.zPosCm = theta (deg)
                phi_norm = (t.yPosCm - PHI_MIN) / max(1, PHI_MAX - PHI_MIN)
                phi_idx  = max(0, min(3, int(phi_norm * 4)))
                r_idx    = 1 if t.xPosCm > (R_MIN + R_MAX) * 0.5 else 0
                for p in PADS:
                    if p[2] == r_idx and p[3] == phi_idx:
                        self.pad_theta[p[0]] = float(t.zPosCm)
        except Exception:
            pass

        # ── Compute energies ──────────────────────────────────────────────────
        energies = {}
        for pid, label, r_idx, phi_idx, col_idle, col_active, wav, boost in PADS:
            r_rng   = self.r_ranges[r_idx]
            phi_rng = self.phi_ranges[phi_idx]
            e       = sum(img[i][j] for i in r_rng for j in phi_rng)
            if r_idx == 1:
                e *= FAR_BOOST
            e *= boost
            energies[pid] = e

        # ── EMA smoothing: eliminate single-frame jitter before threshold test ──
        for pid in self.ema_energies:
            self.ema_energies[pid] = (EMA_ALPHA * energies[pid]
                                      + (1.0 - EMA_ALPHA) * self.ema_energies[pid])

        now = time.monotonic()

        # ── Hysteresis (Schmitt trigger) on smoothed energy ───────────────────
        # Trigger at thresh, release at thresh × RELEASE_THRESHOLD_RATIO.
        # Prevents flicker when a hand hovers at the detection boundary.
        release_thresh = thresh * RELEASE_THRESHOLD_RATIO
        for pid in self.pad_is_active:
            e = self.ema_energies[pid]
            if self.pad_is_active[pid]:
                if e < release_thresh:
                    self.pad_is_active[pid] = False
            else:
                # Velocity gate: only trigger when smoothed energy is rising.
                if e > thresh and e > self.prev_energies[pid]:
                    self.pad_is_active[pid] = True

        # ── 1-HAND vs 2-HAND mode ─────────────────────────────────────────────
        if self.mode == '1-HAND':
            best_pid = max(energies, key=lambda p: energies[p])
            active = {best_pid} if self.pad_is_active[best_pid] else set()
            for pid in list(self.pad_is_active):
                if pid != best_pid:
                    self.pad_is_active[pid] = False
        else:
            active = {pid for pid, v in self.pad_is_active.items() if v}

        playing      = []
        left_active  = False
        right_active = False

        for pid, label, r_idx, phi_idx, col_idle, col_active, wav, boost in PADS:
            energy = self.ema_energies[pid]   # use EMA-smoothed value for glow + display

            # ── Sustain timestamp ─────────────────────────────────────────────
            # Record the last time this pad was above threshold.
            # Retrigger stays eligible for NOTE_DURATION seconds after the last
            # good detection — immune to any duration of radar interference from
            # a second hand without causing infinite sustain.
            if pid in active:
                self.pad_last_active[pid] = now

            # ── Glow ──────────────────────────────────────────────────────────
            self.pad_countdown[pid] = max(0, self.pad_countdown[pid] - 1)
            # Two-segment brightness: below thresh → 0-40% (proximity cursor),
            # above thresh → 40-100% (active play). Hands are always visible.
            if energy < thresh:
                ratio_live = energy / thresh * 0.4
            else:
                ratio_live = 0.4 + min(0.6, (energy - thresh) / max(1, BAR_MAX - thresh) * 0.6)
            # Decay glow: 100% at trigger → 0% over NOTE_FRAMES (full colour flash)
            ratio_decay = self.pad_countdown[pid] / NOTE_FRAMES
            ratio = max(ratio_live, ratio_decay)

            poly_id, col_idle_c, col_active_c = self.poly_ids[pid]
            self.canvas.itemconfig(poly_id, fill=_blend(col_idle_c, col_active_c, ratio))
            # Label glows from dim grey → bright white with pad
            self.canvas.itemconfig(self.label_ids[pid],
                                   fill=_blend('#333333', '#ffffff', ratio))
            # Debug energy overlay
            if self.debug_mode:
                self.canvas.itemconfig(self.energy_ids[pid],
                                       text=str(int(energy)))

            # ── Sustain / retrigger ───────────────────────────────────────────
            if pid in active:
                playing.append(label)
                if phi_idx <= 1:
                    left_active  = True
                else:
                    right_active = True

            # Retrigger when wav is nearly finished AND the pad was detected
            # within the last NOTE_DURATION seconds.  No _stop() — the last
            # 50 ms at < 3% amplitude drains silently alongside the new wav.
            rem_ms          = _remaining_ms(self.pad_wavobj[pid])
            near_end        = rem_ms < 50
            recently_active = (now - self.pad_last_active[pid]) < NOTE_DURATION
            if near_end and recently_active:
                pan_l, pan_r = _phi_pan(phi_idx)
                if KS_MODE and _NUMPY_OK and pid in _NOTE_FREQ:
                    self.pad_wavobj[pid] = _play_ks(_NOTE_FREQ[pid], pan_l, pan_r)
                else:
                    self.pad_wavobj[pid] = _play(wav, pan_l, pan_r)
                self.pad_hits[pid]  += 1
                self.pad_countdown[pid] = NOTE_FRAMES
            # elif near_end and not recently_active: note plays out naturally

            # Theta → pitch: continuously update playback rate while note lives.
            # ±20° theta maps to ±50 cents (factor = 2^(±50/1200)).
            if _SYSTEM == 'Linux' and self.pad_wavobj[pid] is not None:
                theta  = self.pad_theta[pid]
                factor = 2.0 ** (theta * 50.0 / (1200.0 * max(1, abs(THETA_MAX))))
                _mixer.set_pitch(self.pad_wavobj[pid], factor)

        # ── Hand indicator lights ─────────────────────────────────────────────
        self.canvas.itemconfig(self.hand_left_id,
                               fill='#cccccc' if left_active else '#252525')
        self.canvas.itemconfig(self.hand_right_id,
                               fill='#cccccc' if right_active else '#252525')

        # ── MIDI (MPE) ────────────────────────────────────────────────────────
        if self.midi_enabled and _MIDI_OK:
            self.midi_throttle = (self.midi_throttle + 1) % 4
            for pid, label, r_idx, phi_idx, col_idle, col_active, wav, boost in PADS:
                ch         = _PAD_MIDI_CH[pid]
                note       = _PAD_MIDI_NOTE[pid]
                was_on     = self.midi_note_on[pid]
                now_active = pid in active
                energy     = energies[pid]

                if now_active and not was_on:
                    _midi_port.send(_mido.Message('note_on', channel=ch,
                                                  note=note, velocity=100))
                    self.midi_note_on[pid] = True
                elif not now_active and was_on:
                    _midi_port.send(_mido.Message('note_off', channel=ch,
                                                  note=note, velocity=0))
                    self.midi_note_on[pid] = False

                # Continuous expression: pitch bend (theta) + channel pressure (energy)
                # Throttled to every 4 frames (~80 ms) to avoid MIDI flooding
                if was_on and self.midi_throttle == 0:
                    theta    = self.pad_theta[pid]
                    bend_val = int(theta / max(1, abs(THETA_MAX)) * 2048)
                    pressure = min(127, max(0, int(energy / BAR_MAX * 200)))
                    _midi_port.send(_mido.Message('pitchwheel', channel=ch,
                                                  pitch=bend_val))
                    _midi_port.send(_mido.Message('aftertouch', channel=ch,
                                                  value=pressure))

        # ── Status bar ────────────────────────────────────────────────────────
        if playing:
            hands = ('TWO HANDS' if left_active and right_active
                     else 'LEFT HAND' if left_active else 'RIGHT HAND')
            self.statusVar.set('\u266a  {}  \u2014  {}'.format(
                '   '.join(playing), hands))
        else:
            self.statusVar.set('Ready \u2014 wave through zones to play  \u00b7  keep moving to sustain')

        self.prev_energies = dict(self.ema_energies)
        self.cycleId = self.after(LOOP_MS, self.loop)


def main():
    root = tk.Tk()
    root.title('WalaHarp')
    root.configure(bg='#0a0a0a')
    root.resizable(False, False)
    app = HarpApp(root)
    app.pack()
    root.bind('d', app._toggle_debug)
    root.bind('m', app._toggle_midi)
    root.bind('k', app._toggle_ks)
    root.update_idletasks()

    def _on_close():
        if app.cycleId:
            root.after_cancel(app.cycleId)
        if _MIDI_OK and app.midi_enabled:
            for pid in app.midi_note_on:
                if app.midi_note_on[pid]:
                    _midi_port.send(_mido.Message('note_off', channel=_PAD_MIDI_CH[pid],
                                                  note=_PAD_MIDI_NOTE[pid], velocity=0))
        try:
            wlbt.Stop()
            wlbt.Disconnect()
        except Exception:
            pass
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', _on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
