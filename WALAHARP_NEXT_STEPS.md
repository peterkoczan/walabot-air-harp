# WalaHarp — Implementation Log & Next Steps

## Implemented (2026-04-20)

### Step 0 — Previous session features (committed)
- **Hysteresis (Schmitt trigger)**: release threshold = 75% of trigger threshold, prevents zone-boundary flicker
- **Velocity gate**: only fire when smoothed energy is rising (positive delta), suppresses false triggers from slow drift
- **Two-segment glow**: 0–40% below threshold (proximity cursor), 40–100% when active
- **`_WavStream`**: pre-loaded WAV with fractional-position interpolation for real-time pitch shift
- **Theta pitch expression**: ±20° theta from GetSensorTargets() maps ±50 cents via `_mixer.set_pitch()`
- **MPE MIDI output** (key `m`): virtual port "WalaHarp", ch 0–7 per pad, note-on/off + pitch bend + aftertouch; requires `mido + python-rtmidi`
- **Debug energy overlay** (key `d`): live energy values on each pad
- **`generate_sounds.py`**: PEAK lowered 0.30→0.25; sub-octave harmonic on A3/C4 for added depth

### Step 1 — EMA smoothing
- `EMA_ALPHA = 0.4` per zone per frame applied to raw energies before threshold comparison
- Eliminates single-frame noise spikes; glow and velocity gate both use the smoothed value

### Step 2 — Stereo panning
- `aplay` switched to stereo (`-c 2`)
- Constant-power pan gains from phi zone: outer-left→18°, inner-left→36°, inner-right→54°, outer-right→72°
- `_phi_pan(phi_idx)` → `(pan_l, pan_r)` applied when each note is triggered
- `_Mixer._run()` mixes streams into separate L/R accumulators, interleaves, then applies echo
- Echo buffer widened to stereo (2×CHUNK per entry)
- Fixed `if/elif/else` platform dispatch structure that was accidentally overwriting `_play` on Linux

### Step 3 — Karplus-Strong synthesis (key `k`)
- `_ks_pluck(freq)`: ~35-line NumPy delay-line loop generating a 3 s int16 PCM buffer
- Buffers cached in `_KS_CACHE`; pre-generated during warm-up so first notes have zero latency
- `_KSStream`: stream backed by pre-generated numpy array, same pan/pitch interface as `_WavStream`
- `KS_MODE = True` default; key `k` toggles to WAV fallback (generate_sounds.py)
- Produces genuine plucked-harp timbre that decays naturally

### Step 4 — Tuning system switcher (keys `1`–`4`)
- Four presets: A minor pentatonic (default), Gamelan Slendro, Pythagorean, Blues hexatonic
- `_switch_tuning(n)` updates per-pad frequencies and clears/regenerates KS cache instantly
- Tuning bar label always visible: "Tuning: X  [1-4]  [k] KS/WAV  [l] HOLD/LATCH"
- No restart needed

### Step 5 — Latch-on-release mode (key `l`)
- LATCH mode: note fires on the **falling edge** of zone detection (hand exits zone), not entry
- Player can rest arm in a zone, then pull out to play — eliminates gorilla-arm fatigue
- HOLD mode (default): note fires on rising edge as before
- `pad_was_active` tracks previous frame state for edge detection
- Notes play out naturally (no retrigger in latch mode)

### Step 6 — WebSocket visualizer server
- Background asyncio server on port 8765, starts at app launch
- Broadcasts JSON every 5 frames (~10 Hz): `{zones:[{id, energy, active, theta}], timestamp, tuning, latch}`
- `visualizer.html` in repo: self-contained radar fan view (Canvas, vanilla JS)
  - Opens on phone/iPad at `http://192.168.64.2:8765` (the WS URL)
  - Actually the HTML file is served statically; open `visualizer.html` directly or serve via simple HTTP
  - Auto-reconnects every 2 s; shows tuning name, latch mode, active notes
- Open on phone: copy `visualizer.html` to any web server, set `WS_HOST` to `192.168.64.2`

## Known Limitations & Possible Next Steps

- **Visualizer HTTP serving**: `visualizer.html` needs to be served over HTTP to open on phone. Could add a simple `http.server` thread on port 8080 alongside the WS server.
- **KS sustain loop**: currently notes play once per trigger (3 s decay). Could add a sustain loop that re-triggers KS at end of decay when hand is still present.
- **Theta pitch on KS streams**: `_KSStream` uses integer position (no fractional pitch). The theta→pitch mechanism only affects `_WavStream`. Could add fractional-position to `_KSStream`.
- **Two-hand MIDI**: currently MPE sends per-pad; could integrate pressure/CC from energy level per pad.
- **Reverb quality**: current echo is a single-tap delay (320 ms, gain 0.18). A proper Schroeder/Freeverb algorithm would give richer ambiance.
- **Auto-calibration of thresholds**: per-session baseline calibration rather than fixed slider.
- **Wave-to-KS crossfade**: seamlessly cross-fade from WAV to KS when switching with key `k`.

## File Map

| File | Purpose |
|------|---------|
| `walaharp.py` | Main application |
| `generate_sounds.py` | Generate fallback WAV files (run once) |
| `visualizer.html` | Browser radar visualizer (open on phone) |
| `a3.wav` … `e5.wav` | Pre-generated WAV fallback tones |

## VM Deploy

```bash
# SCP and restart (from macOS):
sshpass -p 'walabot123' scp walaharp.py generate_sounds.py walabot@192.168.64.2:/home/walabot/walabot/WalaHarp/
sshpass -p 'walabot123' ssh -o StrictHostKeyChecking=no walabot@192.168.64.2 \
  'nohup bash ~/launch_walaharp.sh > /tmp/walaharp.log 2>&1 </dev/null & echo launched'
```
