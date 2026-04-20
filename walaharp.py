"""
walaharp.py — Walabot air harp: wave hands above the sensor to play harmonic tones.

Handpan layout — 8 oval tone fields on a circular drum body.
Notes are A minor pentatonic; any combination sounds musical.

  Left hand:   A3 (near outer-L) · D4 (near inner-L) · G4 (far inner-L) · C5 (far outer-L)
  Right hand:  C4 (near inner-R) · E4 (near outer-R) · A4 (far inner-R) · E5 (far outer-R)

Notes sustain while your hand stays in a zone, then decay naturally when
you move away.  Multiple zones can play simultaneously.

Run generate_sounds.py once first to create the WAV files.
"""
from __future__ import print_function, division
import os, math, subprocess, signal, platform, threading, wave, struct, time
import WalabotAPI as wlbt
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


class _Mixer:
    """Single persistent aplay + in-process PCM mixer for simultaneous tones."""
    RATE  = 44100
    CHUNK = 256   # ~6 ms per chunk

    # Limiter state: gain is reduced instantly on a peak, then recovers slowly.
    # RELEASE_COEF: gain recovered per chunk (~0.5 s to go from 0.75→1.0).
    RELEASE_COEF = 0.003

    def __init__(self):
        self._streams = []
        self._lock    = threading.Lock()
        self._gain    = 1.0   # current output gain (1.0 = unity)
        self._proc    = subprocess.Popen(
            ['aplay', '-q', '-t', 'raw', '-f', 'S16_LE',
             '-r', str(self.RATE), '-c', '1', '-'],
            stdin=subprocess.PIPE,
            bufsize=0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._run, daemon=True).start()

    def play(self, path):
        """Open WAV and add to mix.  Returns the wave object so the caller
        can stop it later via stop()."""
        try:
            wf = wave.open(path, 'rb')
            with self._lock:
                self._streams.append(wf)
            return wf
        except Exception:
            return None

    def stop(self, wf):
        """Remove a specific wave stream from the mix immediately.
        Safe to call with None or an already-finished stream."""
        if wf is None:
            return
        with self._lock:
            try:
                self._streams.remove(wf)
                wf.close()
            except (ValueError, Exception):
                pass   # already finished naturally — no-op

    def _run(self):
        """Real-time paced loop: keeps the OS pipe near-empty so audio plays
        within ~10 ms of a hit instead of waiting for buffered silence to drain."""
        silence  = b'\x00' * self.CHUNK * 2
        # Target slightly under one chunk duration (85%) so we stay just ahead
        # of aplay without ever pre-filling more than ~1 chunk of pipe buffer.
        interval = self.CHUNK / self.RATE * 0.85
        while True:
            t0 = time.monotonic()

            with self._lock:
                alive, chunks = [], []
                for wf in self._streams:
                    data = wf.readframes(self.CHUNK)
                    if data:
                        chunks.append(data)
                        alive.append(wf)
                    else:
                        wf.close()
                self._streams = alive

            if chunks:
                out = [0] * self.CHUNK
                for data in chunks:
                    for i, s in enumerate(
                            struct.unpack('<%dh' % (len(data) // 2), data)):
                        out[i] += s
                # Slow-release peak limiter.
                # Fast attack: if the mix exceeds ±32767 the gain is reduced
                #   immediately to just fit — no hard clipping.
                # Slow release: gain recovers by RELEASE_COEF per chunk
                #   (~0.5 s to go from 0.75 back to 1.0) so there is no
                #   per-chunk pumping artifact (the cause of the buzz).
                peak = max(abs(s) for s in out)
                if peak == 0:
                    peak = 1
                target = min(1.0, 32767.0 / peak)
                if target < self._gain:
                    self._gain = target                          # fast attack
                else:
                    self._gain = min(1.0, self._gain + self.RELEASE_COEF)  # slow release
                if self._gain < 1.0:
                    out = [int(s * self._gain) for s in out]
                buf = struct.pack('<%dh' % self.CHUNK,
                                  *[max(-32768, min(32767, s)) for s in out])
            else:
                buf = silence

            try:
                self._proc.stdin.write(buf)
            except (BrokenPipeError, OSError):
                break

            # Pace the loop: sleep for the remaining fraction of a chunk period.
            # This keeps the OS pipe almost empty so the next note plays
            # immediately without draining a backlog of buffered silence.
            elapsed   = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)


if _SYSTEM == 'Linux':
    _mixer = _Mixer()
    def _play(path):  return _mixer.play(path)   # returns wave obj
    def _stop(wf):    _mixer.stop(wf)
elif _SYSTEM == 'Darwin':
    def _play(path):
        subprocess.Popen(['afplay', path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    def _stop(wf):    pass
else:
    def _play(path):  return None
    def _stop(wf):    pass

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

# ── Detection / sustain constants ─────────────────────────────────────────────
ENERGY_THRESHOLD = 300   # adjustable via slider
BAR_MAX          = 1500  # energy level that maxes out the glow

# Far zone boost — signal attenuates ~R^4; hands at 90 cm return far less energy
FAR_BOOST        = 5.0

# Note sustain: each WAV is NOTE_DURATION seconds long.
# While a hand is present the note retriggered whenever it nears its end,
# creating a seamless loop.  After the hand leaves the note plays out gracefully.
NOTE_DURATION    = 2.5          # seconds — must match generate_sounds.py DURATION
LOOP_MS          = 20           # ms between Walabot frames (~50 fps)
NOTE_FRAMES      = int(NOTE_DURATION * 1000 / LOOP_MS)   # 125 frames
# Retrigger when 120 ms remain — old note is at ~15% amplitude in its release
# tail, barely audible.  Combined with stop-on-retrigger this gives a clean
# seamless loop with zero phase-overlap distortion.
RETRIGGER_FRAMES = int(0.12     * 1000 / LOOP_MS)        #   6  frames (last 120ms)

# ── Walabot arena ─────────────────────────────────────────────────────────────
R_MIN, R_MAX, R_RES             = 20, 90, 5   # 20–90 cm: hands low → hands raised high
PHI_MIN, PHI_MAX, PHI_RES       = -60, 60, 3
THETA_MIN, THETA_MAX, THETA_RES = -1, 1, 1

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

        self.statusVar = tk.StringVar(value='Connecting...')
        tk.Label(self, textvariable=self.statusVar, font='TkFixedFont 9',
                 bg='#0a0a0a', fg='#888888', anchor=tk.W
                 ).pack(fill=tk.X, padx=6, pady=(4, 0))

        self.canvas = tk.Canvas(self, width=CW, height=CH,
                                bg='#0a0a0a', highlightthickness=0)
        self.canvas.pack(padx=8, pady=4)

        self._build_canvas()
        self._build_controls()

        self._init_walabot()
        self.after(200, self.start_scan)

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

        # Hand placement guides
        c.create_text(DCX - DRX + 12, DCY,
                      text='LEFT\nHAND',
                      fill='#1e1e1e', font='TkFixedFont 7',
                      anchor=tk.W, justify=tk.CENTER)
        c.create_text(DCX + DRX - 12, DCY,
                      text='RIGHT\nHAND',
                      fill='#1e1e1e', font='TkFixedFont 7',
                      anchor=tk.E, justify=tk.CENTER)

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

    def _build_controls(self):
        bar = tk.Frame(self, bg='#0a0a0a')
        bar.pack(fill=tk.X, padx=8, pady=(0, 6))

        tk.Label(bar, text='SENSITIVITY', font='TkFixedFont 7',
                 bg='#0a0a0a', fg='#555').pack(side=tk.LEFT, padx=(0, 4))
        self.threshVar = tk.IntVar(value=ENERGY_THRESHOLD)
        self.threshVar.trace_add('write', self._on_threshold_change)
        tk.Scale(bar, from_=50, to=1000, orient=tk.HORIZONTAL,
                 variable=self.threshVar, showvalue=True,
                 bg='#0a0a0a', fg='#888888', troughcolor='#1a1a1a',
                 activebackground='#444', highlightthickness=0,
                 font='TkFixedFont 7', length=440, sliderlength=12,
                 bd=0).pack(side=tk.LEFT)

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

    def _reset(self):
        for pid in self.pad_hits:
            self.pad_hits[pid] = 0

    def _init_walabot(self):
        wlbt.Init()
        wlbt.SetSettingsFolder()
        wlbt.ConnectAny()
        wlbt.SetProfile(wlbt.PROF_SENSOR)
        wlbt.SetArenaR(R_MIN, R_MAX, R_RES)
        wlbt.SetArenaPhi(PHI_MIN, PHI_MAX, PHI_RES)
        wlbt.SetArenaTheta(THETA_MIN, THETA_MAX, THETA_RES)
        wlbt.SetDynamicImageFilter(wlbt.FILTER_TYPE_MTI)
        wlbt.SetThreshold(35)
        wlbt.Start()

    def start_scan(self):
        self.statusVar.set('Warming up...')
        for _ in range(5):
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

        self.statusVar.set('Ready — wave hands above the sensor')
        self.cycleId = self.after(LOOP_MS, self.loop)

    def loop(self):
        wlbt.Trigger()
        res    = wlbt.GetRawImageSlice()
        img    = res[0]
        thresh = self.threshold

        playing = []

        for pid, label, r_idx, phi_idx, col_idle, col_active, wav, boost in PADS:
            r_rng   = self.r_ranges[r_idx]
            phi_rng = self.phi_ranges[phi_idx]
            energy  = sum(img[i][j] for i in r_rng for j in phi_rng)
            if r_idx == 1:
                energy *= FAR_BOOST
            energy *= boost   # per-pad outer-angle compensation

            # ── Glow ──────────────────────────────────────────────────────────
            # Primary glow tracks live energy.
            # Secondary "decay glow" tracks countdown (note still sounding after
            # hand leaves) at 35% brightness so the listener can see it.
            self.pad_countdown[pid] = max(0, self.pad_countdown[pid] - 1)
            ratio_live  = min(1.0, energy / BAR_MAX)
            ratio_decay = (self.pad_countdown[pid] / NOTE_FRAMES) * 0.35
            ratio = max(ratio_live, ratio_decay)

            poly_id, col_idle_c, col_active_c = self.poly_ids[pid]
            self.canvas.itemconfig(poly_id, fill=_blend(col_idle_c, col_active_c, ratio))

            # ── Sustain / retrigger ───────────────────────────────────────────
            if energy > thresh:
                playing.append(label)
                if self.pad_countdown[pid] <= RETRIGGER_FRAMES:
                    # Stop old instance first — prevents two copies of the same
                    # frequency running simultaneously (phase-overlap distortion)
                    _stop(self.pad_wavobj[pid])
                    self.pad_wavobj[pid] = _play(wav)
                    self.pad_hits[pid]  += 1
                    self.pad_countdown[pid] = NOTE_FRAMES
            # If energy gone: note plays out its remaining countdown naturally
            # (pad_wavobj is left alone — the wave drains to silence by itself)

        # Status bar shows currently sounding notes
        if playing:
            self.statusVar.set('\u266a  ' + '   '.join(playing))
        else:
            self.statusVar.set('Ready \u2014 wave hands above the sensor')

        self.cycleId = self.after(LOOP_MS, self.loop)


def main():
    root = tk.Tk()
    root.title('WalaHarp')
    root.configure(bg='#0a0a0a')
    root.resizable(False, False)
    app = HarpApp(root)
    app.pack()
    root.update_idletasks()
    root.mainloop()


if __name__ == '__main__':
    main()
