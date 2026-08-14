# 09 — The audio path, and the DSP you need to understand it

This chapter assumes you can read Python and numpy and know nothing about
signal processing. Everything is taught against the app's real code —
`app_core/audio/capture.py` and `app_core/audio/downsample.py` — and every
number in it is either measured in `tests/test_downsample.py`, recorded in
the git log, or reproduced by the snippet at the end.

Why care: this stream is the only thing Deepgram ever hears. Every defect
in it — aliased tones, dropped samples, chunk-boundary seams — lands
directly on transcription accuracy, and the transcript is what the answer
is grounded in. The DSP below is not polish; it is the floor the whole
product stands on.

---

## The path, end to end

**WASAPI loopback, not the microphone.** The interviewer's question arrives
through the call app and out of the user's speakers or headset — it is a
*render* stream. The microphone carries the user's own voice, which is the
one thing we do not want. WASAPI (Windows' audio API) can open a loopback
capture on a render device: you record exactly the bytes Windows is about
to play. `capture.py:48-58` finds the default output device and, if it is
not itself loopback-capable, walks PyAudioWPatch's loopback generator for
the matching loopback twin. The known failure mode: if the call's audio is
routed to a *different* device than the default output, loopback records
silence — which is why the app shows a silence hint after ~5 s of inaudible
recording instead of letting the user discover it at Stop.

**The callback thread.** PortAudio delivers device chunks on its own C
thread. `capture.py:82` asks for `frames_per_buffer=src_rate // 8` — ~125 ms
chunks (6000 samples at 48 kHz). The callback does numpy-only work and
nothing else, because two things go wrong on this thread:

- An exception escaping into PortAudio's C callback kills the stream with
  no Python-visible error — the recording just silently captures nothing.
  `_on_device_chunk` (`capture.py:96-109`) catches everything and drops
  the chunk instead.
- The GIL. Per-sample Python loops here would starve the asyncio loop that
  runs the rest of the app. Everything in `downsample.py` is vectorized;
  the whole resample costs a measured ~1.5% of real time
  (`test_cost_stays_negligible_on_the_callback_thread`).

**The transform.** Each device chunk goes through `downsample_chunk`
(`downsample.py:141`): interleaved little-endian i16 → float32 in [-1, 1]
→ mono (channel mean) → the stateful `StreamingResampler` → 16 kHz i16.
The chunk's samples accumulate in `self._pending`; every time 2048 samples
are available — exactly 128 ms at 16 kHz, the frame size Deepgram receives
(`downsample.py:29`) — a frame is cut, its RMS computed for the level
meter, and both are posted to the asyncio loop with
`loop.call_soon_threadsafe` (`capture.py:101-105`). That call is the only
legal doorway from a foreign thread into the loop.

**To Deepgram.** On the loop, `machine._on_frame` (`machine.py:539-548`)
drops frames from stale or stopping sessions and hands live ones to the
STT client. `client.send` (`stt/client.py:134-140`) queues the frame for
the sender task — or, if the WebSocket has not opened yet, into a
~15-second pre-open deque that flushes in order the instant it does. The
URL (`stt/client.py:52`) declares the contract:
`encoding=linear16&sample_rate=16000&channels=1` — raw 16-bit PCM, 16 kHz,
mono. Why 16 kHz mono at all? Speech ASR conventionally runs on 16 kHz
audio — little above 8 kHz carries speech intelligibility — and the wire
cost drops 6×: 48 kHz stereo i16 is 192 kB/s, 16 kHz mono is 32 kB/s.

That "downsample to 16 kHz" step is where all the DSP lives. Three
distinct bugs shipped in the first version of it, all measured before
being fixed in commit `1536a0c`. The rest of this chapter teaches the
theory each fix needs, then shows the fix.

## Sample rate, Nyquist, and aliasing

A digital signal is just measurements of a waveform taken at a fixed rate.
At 16,000 samples per second you can represent any frequency below
8,000 Hz — half the sample rate, called the **Nyquist frequency** —
because a sine wave needs more than two samples per cycle to be pinned
down. So far, so reasonable.

The dangerous part is what happens to frequencies *above* Nyquist. They do
not disappear and they do not error. A tone above Nyquist produces the
exact same sample values as some tone below it, and once sampled the two
are indistinguishable forever. The impostor's frequency for a tone between
Nyquist and the sample rate is:

    f_alias = sample_rate − f

A 10 kHz tone sampled at 16 kHz yields samples identical to a 6 kHz tone
(16,000 − 10,000). Work one sample to convince yourself: sample n lands at
time n/16000, and sin(2π·10000·n/16000) = sin(2πn·5/8) =
−sin(2πn·3/8) = −sin(2π·6000·n/16000) — the same values as a (flipped)
6 kHz sine. This is the audio version of wagon wheels spinning backwards
on film: the camera's frame rate undersamples the spoke frequency.

Now the shipped bug. The original resampler picked every third sample of
the 48 kHz stream (via interpolation — same effect) with no filtering.
A 48 kHz stream legitimately carries content up to 24 kHz: notification
chimes, hold music, sibilants, codec artifacts. Decimating it to 16 kHz
without first removing everything above 8 kHz **folds all of it into the
speech band**. The measurement (git log, `docs/TESTING.md`, reproduced in
the snippet below): a full-scale 10 kHz tone came through at near-full
strength at ~6 kHz — the middle of the band Deepgram reads for vowels and
consonants. To the model this is not noise-like hiss; it is a loud,
structured tone sitting on top of the speech.

The only fix is to remove the >8 kHz content *before* dropping samples.
That requires a low-pass filter.

## FIR filters and windowed-sinc design

An **FIR filter** (finite impulse response) computes each output sample as
a weighted sum of the last N input samples:

    out[i] = taps[0]·in[i] + taps[1]·in[i−1] + … + taps[N−1]·in[i−N+1]

That is a convolution, and it is all `np.convolve` does. No feedback, so
it cannot go unstable, and with symmetric taps it delays every frequency
equally — it does not smear the waveform's shape. The whole design
question is: what weights?

The ideal low-pass filter's weights are a **sinc** function — sin(πx)/πx —
which is infinitely long. The practical recipe, and what
`design_lowpass` (`downsample.py:39-44`) implements, is: take a finite
slice of the ideal sinc, taper its edges with a window, normalize. Line by
line:

```python
n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
```

Tap indices centered on zero: for 63 taps, −31…31. The sinc must be
sampled symmetrically around its peak, both for correctness and for the
equal-delay property above.

```python
taps = 2.0 * cutoff_norm * np.sinc(2.0 * cutoff_norm * n)
```

The textbook ideal-lowpass impulse response with cutoff `cutoff_norm`
(cutoff frequency as a fraction of the sample rate — here 7200/48000 =
0.15). `np.sinc(x)` is the normalized sinc sin(πx)/(πx), which is exactly
the form the textbook formula wants — no manual π bookkeeping.

```python
taps *= np.hamming(num_taps)
```

Truncating an infinite sinc to 63 taps is multiplying it by a rectangle,
and a rectangular cut rings: frequencies that should be blocked leak
through at only ~21 dB down. The Hamming window tapers the slice's edges
smoothly, trading a slightly wider transition band for a much deeper
stopband. Measured on this filter: the 10 kHz alias lands ~59 dB down —
about a thousandth of its amplitude.

```python
return taps / taps.sum()  # unity DC gain
```

The sum of the taps is the filter's gain for a constant signal (and,
nearly, for the low frequencies where speech energy lives). Dividing by it
pins that gain to exactly 1.0, so the filter removes aliases without
changing loudness. `test_speech_band_content_passes_through_intact` holds
this to <1% at 300, 1000 and 3000 Hz.

The constants (`downsample.py:35-36`): 63 taps, cutoff 7200 Hz. The cutoff
sits below the 8 kHz Nyquist because a real filter needs a transition band
— it cannot pass 7999 Hz and block 8001 Hz — and above the band that
matters for speech intelligibility, so nothing the STT model needs is
touched.

## Overlap-save: no seams at chunk boundaries

Here is the streaming problem: computing output sample i needs the 62
input samples before it. The first sample of a device chunk does not have
them — they are at the end of the *previous* chunk. Filter each chunk
independently and those 62 samples are implicitly zero, which stamps a
small transient into the audio at every chunk boundary — eight times a
second, forever, in the middle of speech.

The fix (`downsample.py:74-79`) is to carry exactly `taps − 1` input
samples of state:

```python
buf = np.concatenate([self._fir_state, samples])
samples = np.convolve(buf, self._fir, mode="valid")
self._fir_state = buf[-(self._fir.size - 1):]
```

Prepend the 62 samples saved from last time, convolve in `"valid"` mode —
which only emits outputs whose full 63-sample window is real data, so the
output length equals the chunk length exactly — then save the last 62
input samples for the next chunk. The result is not approximately the
same as filtering the whole stream in one call; it is *sample-for-sample
identical*, which is what makes the chunk-invariance test below possible.

## Phase continuity: one time base across chunks

After filtering, the resampler still has to read the 48 kHz stream at
16 kHz — one output every `step = src_rate / 16000` input samples (3.0
for 48 kHz, 2.75625 for 44.1 kHz). The original code did, per chunk:

```python
np.interp(np.linspace(0, n - 1, out_len), np.arange(n), chunk)
```

Two independent bugs live in that one line:

- **`linspace` pins both endpoints.** It always includes sample 0 and
  sample n−1, so the effective step is `(n−1)/(out_len−1)` — which
  depends on the chunk length and is never exactly 3. Every chunk
  stretches its time base slightly, each chunk size differently, and a
  sine's phase walks off. Measured: up to **2.0** of waveform error
  against resampling the same signal whole — the worst a unit-amplitude
  sine can be wrong by, i.e. perfectly out of phase.
- **`out_len` was rounded per chunk** — `round(n * 16000 / src_rate)` —
  so the fractional remainder of every chunk was discarded: 341 outputs
  per 1024-sample chunk where 341.33 were owed, i.e. 15,985 output
  samples per second where 16,000 were owed. Fifteen samples a second of
  the interviewer's voice, silently deleted.

And even with both fixed, restarting the read position at 0 each chunk
would still be wrong: if the last chunk's final read landed at position
5998.5, the next chunk's first read belongs at 1.5 *into the new data*,
not at 0.

`StreamingResampler.process` (`downsample.py:83-98`) fixes all three with
one idea: a single read position that lives across chunks, kept as a
float.

```python
count = int(np.floor((last - self._pos) / self._step)) + 1
positions = self._pos + np.arange(count, dtype=np.float64) * self._step
out = np.interp(positions, np.arange(buf.size, dtype=np.float64), buf)
next_pos = self._pos + count * self._step
keep_from = min(int(np.floor(next_pos)), buf.size)
self._tail = buf[keep_from:]
self._pos = next_pos - keep_from
```

Read positions advance by exactly `step` from wherever the last chunk
left off; only as many outputs are taken as genuinely fit; the unconsumed
input samples are kept in `_tail` and — the crucial part — the leftover
*fraction* of the read position is carried in `_pos`. Nothing is pinned,
nothing is discarded, nothing restarts.

The property this buys is the strongest kind of correctness claim in the
audio code: feeding a signal through one resampler in *any* chunk sizes
produces byte-identical output to feeding it whole.
`test_output_is_independent_of_how_the_device_chunks_the_audio` asserts
exact equality at chunk sizes 480, 1024, 6000 and 7777 — the device's
chunking is no longer observable in the output at all. The flip side is
also asserted: the same chunk pushed twice through one resampler
deliberately does *not* produce identical output
(`test_downsample_chunk_threads_the_resampler_through`), because the
second call continues the filter and phase state instead of restarting —
statefulness you can see.

## Verify it yourself

Save this as `verify_alias.py` in the repo root and run it with
`.venv\Scripts\python verify_alias.py`. It reproduces the aliasing
measurement from the git log against the real `StreamingResampler`:

```python
import numpy as np
from app_core.audio.downsample import StreamingResampler, TARGET_RATE

SRC = 48_000
t = np.arange(SRC, dtype=np.float64) / SRC                  # 1 second
tone = np.sin(2 * np.pi * 10_000 * t).astype(np.float32)    # 10 kHz

def band_rms(sig, rate, lo, hi):
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.size)))
    freqs = np.fft.rfftfreq(sig.size, 1.0 / rate)
    return float(np.sqrt(np.sum(spec[(freqs >= lo) & (freqs <= hi)] ** 2))
                 / sig.size)

# The old way: interpolate straight onto the new grid, no filter.
naive = np.interp(np.linspace(0, tone.size - 1, tone.size // 3),
                  np.arange(tone.size), tone)

filtered = StreamingResampler(SRC).process(tone)

n = band_rms(naive,    TARGET_RATE, 5_500, 6_500)
f = band_rms(filtered, TARGET_RATE, 5_500, 6_500)
ref = band_rms(np.sin(2 * np.pi * 6_000 * np.arange(TARGET_RATE)
                      / TARGET_RATE).astype(np.float32),
               TARGET_RATE, 5_500, 6_500)
print(f"6 kHz energy, real 6 kHz tone:    {ref:.6f}")
print(f"6 kHz energy, naive decimation:   {n:.6f}")
print(f"6 kHz energy, StreamingResampler: {f:.6f}")
print(f"suppression: {20 * np.log10(n / f):.1f} dB")
```

Output on this repo:

```
6 kHz energy, real 6 kHz tone:    0.306177
6 kHz energy, naive decimation:   0.270056
6 kHz energy, StreamingResampler: 0.000301
suppression: 59.1 dB
```

Read it as: the naive path delivers a 10 kHz tone to the 6 kHz band at 88%
of the strength of a genuine 6 kHz tone; the shipping resampler delivers
it 59 dB down. The math is deterministic, so your numbers will match.

## Honest limits

**Linear interpolation is not sinc interpolation.** After the FIR, output
samples at fractional positions are placed with `np.interp` — a straight
line between neighbors. The proper tool is a windowed-sinc *interpolator*
(a polyphase filter bank, what libraries like soxr implement). At 48 kHz
this is moot: the step is exactly 3.0, every read position lands on a
whole sample, and `np.interp` degenerates into exact sample-picking. At
44.1 kHz (step 2.75625) genuinely fractional reads occur and linear
interpolation slightly attenuates the top of the band and adds a small
error a polyphase design would not. No test bounds that error — the one
44.1 kHz test checks the sample count, not fidelity — and measured it
costs ~1.6% of amplitude at 3 kHz and more above that. Tolerable for
16 kHz speech into an STT model; for music or for listeners, it would not
be good enough.

**The filter is fixed: 63 taps, Hamming, 7200 Hz cutoff.** There are no
tunables, deliberately — the constants are sized to the one job (speech
into Deepgram, ~1.5% of real time on the callback thread, ~59 dB
suppression) and a knob nobody turns is a bug surface. If you needed
more: more taps buys a sharper transition at linear cost, but not a
deeper stopband — with the window fixed, that depth barely moves however
long the filter; a Kaiser window lets you *specify* the stopband depth
instead of taking what Hamming gives; past a few hundred taps you would
switch the convolution to FFT-based overlap-save; and for arbitrary rate
ratios done properly you would reach for a polyphase resampler rather
than filter + interp. None of that is warranted here — the measured
numbers above are the argument.

**The no-downsample paths are trusting.** A source already at 16 kHz
passes through the FIR-less branch, and a source *below* 16 kHz would be
upsampled by plain linear interpolation (`downsample.py:65-68` — correct
that upsampling cannot alias, but linear upsampling does leave faint
spectral images). In practice Windows render devices default to 44.1 or
48 kHz, so the branch exists for correctness, not for hardware anyone is
likely to have.
