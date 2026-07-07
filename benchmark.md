# Benchmarks

Test file: real animated WebP, 301 frames, 1072×1520, no per-frame duration
metadata (every frame reports `duration: 0`, so every method falls back to
an assumed frame rate). Machine: Apple Silicon Mac. Each number is a
repeated-run measurement, not a single sample.

## Frame extraction + encode, end to end

| # | Method | Time | Δ vs baseline |
|---|---|---|---|
| 1 | PIL → PNG files → `ffmpeg` image-sequence encode | 22.3s | baseline |
| 2 | PIL → PNG files → `moviepy` `ImageSequenceClip.write_videofile` | 29.0s | −30% (slower) |
| 3 | `magick input.webp output.mp4` (ImageMagick + `dwebp` delegate) | 28.3–29.7s | −29% (slower) |
| 4 | Headless-browser canvas frame capture (Playwright) | not viable | real-time floor ≈ clip duration; see WHY.md |
| 5 | PIL raw-frame → `ffmpeg` stdin pipe (no disk) | 4.5–4.6s | **+80%** |
| 6 | 5 + threaded producer/consumer writer | 3.95s | +82% |
| 7 | 6 + native RGBA passthrough (skip `.convert('RGB')`) | 3.79s | +83% |
| 8 | 7 + `h264_videotoolbox` hardware encode | **2.5–2.7s** | **+88%** |
| 9 | 7 + `libx264 -preset ultrafast` (CPU fallback, no hardware encoder) | ~2.9s | +87% |

This tool implements #8 with automatic fallback to #9 when
`h264_videotoolbox` isn't available.

## Phase breakdown (method #1, the naive baseline)

```
PIL extraction (PNG writes):  18.95s
ffmpeg encode:                 3.38s
                               ------
total:                        22.33s
```

## Phase breakdown (method #7, before hardware encoding)

```
duration-scan pass:      0.025s
convert(RGB) total:      3.129s
tobytes() total:         0.546s
stdin.write() total:     0.680s   (blocking on ffmpeg's pipe backpressure)
ffmpeg drain/wait:       0.409s
                         ------
total:                   ~3.79s (with threading overlapping convert vs. write)
```

## Isolating the true decode floor

```
seek+load only, no conversion, no I/O:  1.917s for 301 frames
```

This is the unavoidable cost of `libwebp` decoding every frame. Everything
above it in any method's total is overhead introduced by extraction,
conversion, or I/O choices — which is what methods #5 through #9 each
attack in turn.

## Correctness checks

Every method's output was verified against the same three properties
before its timing was trusted:

- `ffprobe` duration matches the assumed-fps × frame-count calculation
  (12.04s for this file at 25fps × 301 frames)
- Output dimensions match source (1072×1520)
- Sampled frames at multiple timestamps show genuinely different pixel
  content (not a frozen first frame repeated)

One additional check specific to this tool: a `frame.tile`-based
partial-frame compositing path (meant to handle WebP files that store
frames as region diffs) was tested directly and found to never fire on
any frame of any file, via either a manual `.seek()` loop or
`ImageSequence.Iterator`. PIL's webp plugin decodes via libwebp's
`WebPAnimDecoder`, which resolves frame disposal/blending internally and
always yields a complete, already-composited frame — so `tile` is always
empty by the time Python code can see it. The compositing mechanism was
removed; see WHY.md for the full story.

## Batch parallelism (`--jobs`)

Separate from single-file speed: converting a directory of files with
multiple worker threads, on the same 10-core machine, with 8 real webp
files (each similar to the one benchmarked above):

| `--jobs` | Total time | Per-file time |
|---|---|---|
| 1 | 20.11s | ~2.5s each |
| 4 | 10.93s | ~5.3–5.6s each |

~1.84x faster at `--jobs 4`, not 4x -- per-file time roughly doubles
under contention because PIL's frame decode is CPU-bound and holds the
GIL, so worker threads partly serialize on that step; only the hardware
encode (a separate process per file) truly parallelizes. `--jobs`
defaults to `os.cpu_count()` on the strength of this result, since more
workers than cores can't help further and the ~2x win is real even if
sub-linear.

All 8 outputs verified correct (matching duration/dimensions) in both
runs before trusting the timing.

## Reproducing

```
uv run fast_webp_to_mp4.py test        # correctness (doctests)
time uv run fast_webp_to_mp4.py file.webp   # timing, on your own hardware/file
time uv run fast_webp_to_mp4.py some_dir --jobs N   # batch timing at N workers
```

Absolute numbers will vary by machine and source file (frame count,
resolution, and whether hardware encoding is available all matter); the
*relative* ordering and the reasoning behind each step should hold.
