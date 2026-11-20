# Bundled demo sounds — licence and provenance

This directory ships with the package and is copied next to the user's sound
library at startup, so this file is user-visible. It records the source and
licence of every audio file here. There are two provenances, and both are
dedicated to the public domain.

## Licences

**CC0 1.0 Universal (public domain dedication).**

- `demo_ding.wav`, `demo_dong.mp3`, `demo_chime.ogg` and `demo_alert.flac` are
  generated in-house for this project with `ffmpeg` from a plain sine-wave
  source. They are our own work, so there is nothing to attribute.
- `demo_doorbell.ogg` is a **third-party recording**, used under the same
  dedication:

  > "Door Bell" by Amada44, Wikimedia Commons, CC0

  Source: <https://commons.wikimedia.org/wiki/File:Sound_Effect_-_Door_Bell.ogg>
  (direct: <https://upload.wikimedia.org/wikipedia/commons/3/34/Sound_Effect_-_Door_Bell.ogg>).
  Author Amada44, own work. The CC0 / Public Domain Dedication was read from
  the Wikimedia Commons API, not assumed from the file name.

All five files may be copied, modified and redistributed for any purpose
without permission or attribution.

## Files

| File | Purpose | Tone |
| --- | --- | --- |
| `demo_ding.wav` | rising doorbell chime | 880 Hz sine, 0.5 s |
| `demo_dong.mp3` | low doorbell chime | 440 Hz sine, 0.5 s |
| `demo_chime.ogg` | mid chime | 660 Hz sine, 0.5 s |
| `demo_alert.flac` | alert tone | 330 Hz sine, 0.5 s |
| `demo_doorbell.ogg` | classic two-tone ding-dong doorbell | ≈740 Hz then ≈575 Hz, 2.97 s |

The WAV is 8 kHz mono; the others are 44.1 kHz mono.

## Documented loudness target

Every demo is normalised to **−16.0 LUFS integrated, with a −1.0 dBTP
true-peak ceiling**.

Peak normalisation was rejected for this material. A bell strike with a fast
decay peaks near full scale while measuring far quieter in LUFS, so matching
peaks would leave the bell much louder than the tones to a listener. Integrated
loudness (ITU-R BS.1770 / EBU R128) is what makes the set *perceptually equal*:
the four sines sit near −12.2 dBFS peak and the bell near −1.0 dBFS peak, yet
all five measure about −16 LUFS. That spread is the expected result, not a
mistake — do not "fix" it by matching peaks.

## Measured loudness

Measured on the development host with `ffmpeg` n9.0.2. One file (the WAV) has a
lower sample rate; the rest are 44.1 kHz mono.

| File | Codec | Rate | Sample peak dBFS | True peak dBTP | Integrated LUFS | LRA | Duration | Bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `demo_ding.wav` | pcm_s16le | 8000 | −12.71 | −12.53 | −15.95 | 0.00 | 0.500 s | 8078 |
| `demo_dong.mp3` | mp3 64k | 44100 | −12.22 | −12.22 | −15.95 | 0.00 | 0.500 s | 4640 |
| `demo_chime.ogg` | vorbis q0 | 44100 | −12.06 | −12.05 | −16.05 | 0.00 | 0.500 s | 4356 |
| `demo_alert.flac` | flac | 44100 | −12.21 | −12.21 | −16.05 | 0.00 | 0.500 s | 39480 |
| `demo_doorbell.ogg` | vorbis q6 | 44100 | −1.04 | −0.94 | −16.16 | 0.00 | 2.973 s | 38100 |

For the record, the previous generation peaked at ≈ −17.7…−18.5 dBFS and
measured ≈ −21.4…−22.3 LUFS, so all five files were both quieter and uneven.

## Three things that will trip up the next maintainer

1. **The four sines are not re-encoded.** Each is regenerated from its `sine=`
   source with a static `volume=<gain>dB` filter, so there is no generation
   loss and the shipped file is bit-fresh from the same generator. The gain is
   `target_LUFS − measured_LUFS`; because these tones have LRA 0.00 the
   relationship is exactly linear and a single static gain lands within 0.1 dB.
2. **`demo_doorbell.ogg` is processed from the untrimmed 3.700 s original, with
   `atrim` *after* `loudnorm` in the same filtergraph.** `loudnorm`'s dynamic
   mode has a 3 s look-ahead; trimming to 2.97 s first makes it fall back to a
   true-peak-limited static gain, which lands at only −20.75 LUFS regardless of
   the parameters given. Loudnorm-then-atrim also needs only one encode.
3. **The doorbell's first strike is about 2 dB quieter than the second.** The
   dynamic gain ramps in over roughly the first second, so the first strike
   receives less gain. The two-tone ding-dong, its pitch, order and decay are
   unchanged; this is an inherent property of the reference command below, not a
   defect in the file.

## Regenerate with

Run from the repository root. Every invocation is bounded with `timeout` so a
broken `ffmpeg` cannot hang a build or a test run.

```bash
out=src/chime_deck/assets/sounds

timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f lavfi \
  -i "sine=frequency=880:duration=0.5" -af "volume=5.35dB" \
  -ar 8000 -ac 1 -c:a pcm_s16le "$out/demo_ding.wav"

timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f lavfi \
  -i "sine=frequency=440:duration=0.5" -af "volume=6.25dB" \
  -ar 44100 -ac 1 -c:a libmp3lame -b:a 64k "$out/demo_dong.mp3"

timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f lavfi \
  -i "sine=frequency=660:duration=0.5" -af "volume=5.55dB" \
  -ar 44100 -ac 1 -c:a libvorbis -q:a 0 "$out/demo_chime.ogg"

timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y -f lavfi \
  -i "sine=frequency=330:duration=0.5" -af "volume=5.85dB" \
  -ar 44100 -ac 1 -c:a flac "$out/demo_alert.flac"

# demo_doorbell.ogg: pass 1 measures the UNTRIMMED source.
timeout 30 ffmpeg -nostdin -hide_banner \
  -i Project_state/assets-incoming/doorbell_original.ogg \
  -af "loudnorm=I=-16:TP=-1.0:LRA=11:print_format=json" -f null -
# -> input_i=-25.10 input_tp=-5.37 input_lra=1.20 input_thresh=-36.26 target_offset=1.12

# Pass 2 applies those measured values; loudnorm MUST come before atrim.
timeout 60 ffmpeg -nostdin -hide_banner -loglevel error -y \
  -i Project_state/assets-incoming/doorbell_original.ogg \
  -af "loudnorm=I=-16:TP=-1.0:LRA=11:measured_I=-25.10:measured_TP=-5.37:measured_LRA=1.20:measured_thresh=-36.26:offset=1.12,atrim=start=0.31:end=3.28" \
  -ac 1 -ar 44100 -c:a libvorbis -q:a 6 "$out/demo_doorbell.ogg"
```
