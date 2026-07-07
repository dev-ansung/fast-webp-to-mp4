# Converting animated WebP to MP4: from 22 seconds to 2.6 (and 12.5 for a batch of 8)

We needed to convert animated WebP files to MP4. ffmpeg, which normally handles this kind of thing without a second thought, refuses outright:

```
[webp @ 0x15a2061b0] skipping unsupported chunk: ANIM
[webp @ 0x15a2061b0] skipping unsupported chunk: ANMF
[webp @ 0x15a2061b0] image data not found
Decode error rate 1 exceeds maximum 0.666667
Conversion failed!
```

ffmpeg's own WebP decoder only reads single-frame WebP. Every animated frame past the first is invisible to it. Everything below is about getting those frames out some other way, then making that "some other way" fast, then finding out how much of it was actually correct.

## First pass: extract frames, hand them to ffmpeg as PNGs

Pillow (PIL) wraps `libwebp` directly, so it doesn't care about ffmpeg's decoder gap. The obvious approach: walk every frame, save each as a PNG, point ffmpeg at the resulting image sequence.

```python
with Image.open(path) as im:
    for i in range(im.n_frames):
        im.seek(i)
        im.convert("RGB").save(f"{i:05d}.png")
# then: ffmpeg -framerate 25 -i %05d.png -c:v libx264 out.mp4
```

On our real test file — 301 frames, 1072×1520 — this took **22.3 seconds**. Encoding itself was only 3.4s of that. **19 of the 22 seconds were PIL writing PNGs to disk**, one at a time.

We checked whether reaching for a heavier tool would just be faster out of the box:

| Method | Time |
|---|---|
| PIL → PNGs → ffmpeg | 22.3s |
| PIL → PNGs → moviepy `ImageSequenceClip` | 29.0s |
| `magick input.webp output.mp4` (ImageMagick + `dwebp`) | 28.3–29.7s |

moviepy re-wraps ffmpeg with enough overhead to land slower than calling ffmpeg ourselves. ImageMagick's one-liner is the simplest command of the three, but ties for slowest and gives up all control over per-file behavior — no skip-existing, no hardware encoder choice, no batch handling.

We also considered just letting a browser render the WebP, since browsers support animated WebP natively — why not screenshot it frame by frame? Tested for real with a headless Playwright browser before writing it off: a browser's `<img>` animation is driven by the system clock, not a frame index. There's no `img.seek(i)` equivalent. Sampling as fast as `requestAnimationFrame` allows for 2 real seconds caught exactly **one** frame change. Extracting all 301 frames this way means waiting out the actual ~12 seconds of real-time playback as a hard floor — and you can't just wait exactly that long and stop, since with no reliable per-frame timing you have to oversample to avoid missing a frame boundary, the way our 2-second test did. Add browser launch overhead and per-screenshot cost on top, and the realistic total lands at or past the 22.3s we were already trying to beat, with no frame-accuracy guarantee.

## The real fix: skip the disk entirely

The breakthrough wasn't a smarter library — it was noticing that step 1 never needed to touch disk at all. ffmpeg can read raw, uncompressed frames directly from stdin if you tell it the exact format:

```python
cmd = [
    "ffmpeg", "-y",
    "-f", "rawvideo", "-vcodec", "rawvideo",
    "-s", f"{width}x{height}", "-pix_fmt", "rgb24", "-r", str(fps),
    "-i", "-",
    "-c:v", "libx264", "-pix_fmt", "yuv420p",
    output_path,
]
process = subprocess.Popen(cmd, stdin=subprocess.PIPE)

img.seek(0)
while True:
    frame = img.convert("RGB")
    process.stdin.write(frame.tobytes())
    img.seek(img.tell() + 1)
```

No PNG encoding, no disk write, no disk read back into ffmpeg. Just bytes, straight from PIL's decoder into ffmpeg's stdin, in memory.

**4.5 seconds.** A 5x improvement, stable across repeated runs, verified frame-for-frame correct against the slow method.

## Chasing the last two seconds

4.5s felt beatable, so we instrumented every phase instead of guessing:

```
duration-scan pass:      0.025s
convert+write loop:      4.079s
ffmpeg drain/wait:       0.409s
```

Then split the 4.08s further, isolating PIL conversion from the pipe write itself:

```
convert(RGB) total:  3.129s
tobytes() total:     0.546s
stdin.write() total: 0.680s   <- blocking on ffmpeg's pipe backpressure
```

To find the true floor — how much was unavoidable WebP decoding versus optional conversion work — we timed bare frame decoding with nothing else happening:

```
seek+load only (bare decode floor): 1.917s for 301 frames
```

Roughly 1.9s is `libwebp` doing its job. Everything above that was overhead we'd introduced. Two things stood out.

**Frames decode natively as RGBA, not RGB.** `img.convert('RGB')` was doing real work — dropping an alpha channel we didn't need — 301 times. Skipping it and feeding ffmpeg raw RGBA instead turned out roughly break-even: RGBA is 33% more bytes through the pipe, which ate most of what we saved in Python.

**The write is blocking, and it doesn't have to be.** Every iteration was strictly serial: decode a frame, then sit blocked on `stdin.write()` until ffmpeg's pipe buffer had room. PIL's decode is CPU-bound; the write is I/O-bound waiting on ffmpeg — they don't need to fight over the GIL. A background thread that only writes, fed by a bounded queue, lets the main thread decode frame N+1 while the previous frame is still draining into ffmpeg.

Combining the threaded writer with native RGBA passthrough: **3.79 seconds.**

## A dead end we walked into, and backed out of — twice

Animated WebP can, in principle, store frames as **partial updates** — only the changed region, not a full image — which would need compositing onto a running canvas to avoid ghosted output. We built exactly that: detect a partial frame via `frame.tile`, composite it onto the canvas, otherwise replace the canvas wholesale.

Two versions of that detection shipped, in order:

- `getattr(img, "tile", None)` on a manually `.seek()`'d image — looked reasonable, but `tile` isn't populated that way at all, so the check silently never fired.
- The "fix": walk frames via `ImageSequence.Iterator` instead, which should populate `tile` at the right point in the decode lifecycle.

Neither actually worked. A later, closer look — during a full code review, not the original benchmarking — traced it into PIL's own WebP plugin source: PIL decodes animated WebP through libwebp's `WebPAnimDecoder`, which resolves frame disposal and blending **inside libwebp itself** and always hands PIL a complete, already-composited, canvas-sized frame — never a raw partial region. `tile` gets set internally during `load()` and cleared again immediately after, regardless of which walking API is used, so by the time any Python code inspects it, it's always empty. Confirmed on every single frame of the real 301-frame test file: the "partial" branch fired exactly 0 times, the entire time this code existed.

The compositing logic wasn't a subtle bug fix that mostly worked — it was dead code that never ran, on any file, ever. The real fix wasn't a smarter check. It was deleting the whole mechanism, because the problem it existed to solve was already solved one layer down, in libwebp, before our code ever saw a frame.

## Round two: encoding is now the bottleneck

3.79s left roughly 1.9s as the unavoidable decode floor — meaning encoding and pipe overhead still made up about half the total. The next round of profiling went after that half:

| Change | Time | Verdict |
|---|---|---|
| `-c:v h264_videotoolbox -b:v 5000k` (Apple Silicon hardware encoder) | **~2.5–2.7s** | Real win, stable across repeated runs, correct output |
| `-c:v libx264 -preset ultrafast` (CPU-only fallback) | ~2.9s | Also real — the default `libx264` preset was doing more compression work than a short clip needs |
| Pillow-SIMD | Untested | Skipped: unmaintained for years, needs a compiler toolchain — and the bottleneck (libwebp's C decoder) isn't the Python-level PIL ops Pillow-SIMD accelerates |

`h264_videotoolbox` moves the encode step onto Apple's dedicated media engine, so it stops contending with Python for the same CPU cores while the pipe drains. `-preset ultrafast` gets most of the same win on any machine, by telling `libx264` to skip compression search steps that don't matter for a short clip. The tool auto-detects hardware support and falls back automatically.

## What a full code review actually found

Once the happy-path timing looked good, a full review pass over the whole file — not just the hot loop — turned up problems none of the benchmarking runs ever surfaced, because none of them ever hit the unhappy path:

- **A stderr deadlock, reproduced directly.** stdin was drained on a background thread, but stderr was only read *after* that thread finished. Feed ffmpeg enough verbose log output and it blocks writing to a full stderr pipe, stops consuming stdin, and the writer thread hangs forever. A repro script proved the hang, then proved the fix: drain stderr concurrently on its own thread, and the same script finishes in 0.1s instead of hanging.
- **One corrupt file could destroy an entire parallel batch.** There was no exception handling around decode/encode at all. A single unreadable `.webp` raised uncaught, and under threaded batch processing that exception surfaces when the thread pool's results are collected — discarding the results of every *other* file that had already finished successfully. Fixed by catching per-file failures and reporting them individually, so 7 good files converting alongside 1 bad one still produce 7 good outputs.
- **A leaked file handle** on every conversion, and **no check for ffmpeg being missing entirely** (a bare `FileNotFoundError` traceback instead of a clear message), rounded out the list.

None of this changed the timing numbers above. All of it changed whether the tool actually survives contact with real, messy input.

## Batches: parallel, but not linearly so

Everything above is about converting one file as fast as possible. For a directory of many, the obvious next question is whether converting several at once is worth it. Tested on a real batch of 8 files, on a 10-core machine:

| `--jobs` | Total time | Per-file time |
|---|---|---|
| 1 (sequential) | 20.11s | ~2.5s each |
| 4 (parallel) | 10.93s | ~5.3–5.6s each |

`--jobs 4` is about 1.84x faster overall — a real win, but nowhere near the 4x a naive read of "4 workers" suggests. The per-file time roughly doubling under contention is the tell: PIL's frame decode is CPU-bound Python/C-extension work that holds the GIL, so multiple worker threads doing that concurrently partly serialize on it instead of truly running in parallel. Only the hardware encode step — a separate OS process per file — genuinely parallelizes. Since more workers than there are cores to run them can't help further, `--jobs` now defaults to the machine's CPU count instead of running one file at a time by default: a real, if sub-linear, win with no downside for anyone who doesn't override it.

## Where we landed

| Step | Time | Change |
|---|---|---|
| PIL → PNGs → ffmpeg | 22.3s | Baseline |
| Raw stdin pipe (no disk) | 4.5s | −80% |
| + threaded writer, raw RGBA | 3.79s | −16% more |
| + hardware encode (`h264_videotoolbox`) | ~2.6s | −31% more |
| + `--jobs` default = CPU count, batch of 8 | ~1.84x on top, batch-wide | |

**Total: ~8.5x faster than where we started** for a single file, and close to another 2x on top of that for a batch — every step earned its place by measurement, not by folklore. Disk I/O was 85% of the original cost. Of what was left, half was one avoidable color conversion and a serialized blocking write. Of what was left after *that*, most was the CPU encoder contending with Python for cycles. One proposed "optimization" along the way — the tile-detection compositing — turned out to be a bug that never activated once, in either of its two forms, which is exactly the kind of thing you only catch by testing the claim instead of accepting it. And a review pass after the speed work was done found real correctness bugs that no amount of timing benchmarks would ever have surfaced, because they only show up when something goes wrong.
