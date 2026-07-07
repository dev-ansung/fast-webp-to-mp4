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

## A dead end we walked into, and backed out of

Animated WebP can, in principle, store frames as **partial updates** —
only the changed region, not a full image — which would need compositing
onto a running canvas to avoid ghosted output. We built exactly that:
detect a partial frame via `frame.tile`, composite it, else replace the
canvas wholesale.

Two versions of that check shipped, in order:

- `getattr(img, "tile", None)` on a manually `.seek()`'d image — looked
  reasonable, but `tile` isn't populated that way at all, so the check
  silently never fired.
- The "fix": walk frames via `ImageSequence.Iterator` instead, which
  looked like it should populate `frame.tile` correctly.

Neither actually worked, and a later, closer look at PIL's own webp
plugin source explains why: PIL decodes animated WebP through libwebp's
`WebPAnimDecoder`, which resolves frame disposal and blending **inside
libwebp itself** and always hands PIL a complete, already-composited,
canvas-sized frame — never a raw partial region. `tile` gets set
internally during `load()` and cleared again immediately after, so by
the time any Python code inspects it, it's always empty. Confirmed on
every single frame of the real 301-frame test file: the "partial" branch
fired exactly 0 times.

The compositing code wasn't a subtle bug fix that mostly worked — it was
dead code that never ran, on any file, the entire time. The fix wasn't
a smarter check; it was deleting the mechanism, because the problem it
existed to solve was already solved one layer down, and we hadn't looked
there yet.

## Batches: parallel, but not linearly so

Everything above is about converting *one* file as fast as possible. For a
directory of files, the obvious next question is whether converting
several at once is worth it. Tested on the real 8-file batch this tool
was built for:

| `--jobs` | Total time | Per-file time |
|---|---|---|
| 1 (sequential) | 20.11s | ~2.5s each |
| 4 | 10.93s | ~5.3–5.6s each |

`--jobs 4` is ~1.84x faster overall, not 4x. The reason is visible in the
per-file time doubling under contention: PIL's frame decode is CPU-bound
Python/C-extension work that holds the GIL, so N worker threads doing
that concurrently partly serialize on it rather than truly running in
parallel. Only the hardware encode step (a separate OS process per file)
genuinely parallelizes across threads. Since more workers than there are
CPU cores to actually run them can't help and only adds contention,
`--jobs` now defaults to the machine's CPU count rather than 1 — a
worthwhile default given the real, if sub-linear, speedup measured above,
but not a promise of linear scaling with job count.

## Net result

~8.5x faster than the naive PNG-per-frame approach (22.3s → ~2.6s) for a
single file, and close to another 2x on top of that for a batch of many
files processed in parallel — arrived at by profiling each phase rather
than assuming, and one fewer mechanism to maintain after confirming,
rather than assuming, that the partial-frame compositing code was never
doing anything.
