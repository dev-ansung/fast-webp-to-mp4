# Why this approach

All numbers below are from repeated runs against the same real 301-frame
animated WebP (1072×1520, no per-frame duration metadata).

## The constraint everyone hits first

ffmpeg's own decoder only reads single-frame WebP. Feed it an animated one
and it fails outright:

```
[webp @ 0x15a2061b0] skipping unsupported chunk: ANIM
[webp @ 0x15a2061b0] skipping unsupported chunk: ANMF
Decode error rate 1 exceeds maximum 0.666667
Conversion failed!
```

Every viable approach has to decode frames some other way and hand the
result to ffmpeg (or an equivalent encoder) itself.

## Approaches measured

| Approach | Time | Why it loses |
|---|---|---|
| PIL → PNG files → ffmpeg image-sequence encode | 22.3s | ~19s of that is PNG compression + disk write, one file per frame — the actual encode is only ~3.4s |
| PIL → PNG files → moviepy `ImageSequenceClip` | 29.0s | Same PNG cost as above, plus moviepy wraps ffmpeg with ~3x more overhead than calling it directly |
| `magick input.webp output.mp4` (ImageMagick + `dwebp`) | 28.3–29.7s | Simplest one-liner, but ties for slowest, and gives up all control over per-file behavior (can't skip existing outputs, no hardware encode selection, no recursive batch handling) |
| Headless-browser canvas frame capture | not viable | `<img>` animation is tied to the system clock — there's no seek-to-frame-N API. Confirmed experimentally: 2 real seconds of `requestAnimationFrame` sampling caught exactly one frame change. Extracting all frames this way means waiting out the full real-time playback *plus* an oversampling margin to avoid missing frame boundaries, on top of launching a browser — already at or past 22s before accounting for accuracy risk |
| **PIL raw-frame stdin pipe → ffmpeg (this tool)** | **~2.6s** | See below |

## What actually makes this fast

**1. Skip the disk entirely.** ffmpeg can read raw, uncompressed frames
directly from stdin if you tell it the exact format:

```
ffmpeg -f rawvideo -pix_fmt rgba -s WxH -r FPS -i - -c:v ... out.mp4
```

No PNG encoding, no per-frame file write, no ffmpeg re-reading those files
back off disk. Bytes go from PIL's decoder straight into ffmpeg's stdin,
in memory. This alone took the naive PNG approach from 22.3s to 4.5s.

**2. Overlap decode with the pipe write.** A synchronous loop is: decode a
frame, then block on `stdin.write()` until ffmpeg's pipe buffer has room,
repeat. PIL's decode is CPU-bound; the write is I/O-bound waiting on
ffmpeg. A background thread that only writes, fed by a bounded queue, lets
the main thread start decoding the next frame while the previous one is
still draining into ffmpeg. This brought it to 3.79s.

**3. Use hardware encoding when available.** `h264_videotoolbox` moves the
encode step off the CPU entirely, onto Apple's media engine, so it stops
contending with Python for the same cores. Confirmed stable at ~2.5–2.7s
across repeated runs. On machines without it, `libx264 -preset ultrafast`
gets most of the same win (~2.9s) by skipping compression search steps
that don't matter for a short clip — this tool auto-detects and falls back.

## What it gets right that a naive rewrite of the above wouldn't

Some animated WebP files store frames as **partial updates** — only the
changed region, not a full image. Decoding frames in isolation without
compositing each partial frame onto the previous full frame produces
ghosted, corrupted output. This is easy to get wrong two ways:

- Skip the check entirely (most quick scripts do this) — silently
  corrupts any file using partial frames.
- Check for it with `getattr(img, "tile", None)` on a manually `.seek()`'d
  image — looks reasonable, but `tile` isn't populated correctly outside
  of `ImageSequence.Iterator`'s frame-walking order, so the check silently
  never fires. We hit this exact bug during development and verified it
  directly before it shipped.

This tool walks frames via `ImageSequence.Iterator` (the API that actually
populates `frame.tile` correctly) and composites partial frames onto a
running canvas, so both full-frame and partial-frame WebP convert
correctly — not just the common case.

## Net result

~8.5x faster than the naive PNG-per-frame approach (22.3s → ~2.6s),
arrived at by profiling each phase rather than assuming — and correct on
a class of file (partial-frame WebP) that a faster-but-naive version of
this same pipe-based approach would silently mishandle.
