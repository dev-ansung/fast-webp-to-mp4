# fast-webp-to-mp4

Blazing-fast, memory-efficient conversion of animated WebP files to MP4. 

FFmpeg's native decoder cannot read animated WebP files (it only supports single-frame WebPs and fails with `ANIM`/`ANMF` chunk errors). Naively working around this by extracting frames to PNG files before encoding takes upwards of 22 seconds for a short clip. 

This tool solves the problem by decoding frames via Pillow and streaming the raw RGBA bytes directly into FFmpeg's `stdin` over a pipe. It reduces the conversion time of a 301-frame, 1080p animated WebP down to **~2.6 seconds** (an 8.5x improvement).

## ✨ Key Features

* **Zero Disk I/O:** Frames are piped in memory (`-f rawvideo`), completely bypassing the overhead of zlib compression and disk writes.
* **Hardware Acceleration:** Automatically detects and uses Apple's `h264_videotoolbox`/`hevc_videotoolbox` hardware encoders on supported Macs. Falls back to `libx264`/`libx265 -preset ultrafast` otherwise.
* **Concurrent Processing:** Uses a producer/consumer threading model so Python can decode frame N+1 while FFmpeg encodes frame N. Optionally converts multiple files in parallel with `--jobs`.
* **Correct Partial-Frame Handling:** Accurately detects WebPs that use region-diff (partial) frames and composites them onto a running canvas to prevent ghosted or corrupted output.
* **Batch Processing:** Pass a directory to recursively find and convert all `*.webp` files instantly.
* **Transparent Decisions:** Every choice the tool makes — hardware vs. software encoder, resolved fps, output location — is printed as it happens, not just at the end.

## 🛠 Requirements

- `ffmpeg` installed and available on your system `PATH` (with `h264_videotoolbox`/`hevc_videotoolbox` for hardware encode on macOS — check with `ffmpeg -encoders | grep videotoolbox`; otherwise it falls back automatically).
- Python 3.13+.

*(Note: If you use `uvx`, Python and package dependencies are handled automatically.)*

## 🚀 Usage

You can run this tool directly using `uvx` without needing a local installation.

**Convert a single file:**
```bash
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/file.webp
```

**Convert an entire directory:**
Pass a directory to recursively search for `*.webp`. Every match is converted to a same-named `.mp4` next to the original file (or under `--output-dir`, see below).

```bash
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/dir
```

### Example Output

```text
$ fast-webp-to-mp4 ./stickers
Found 2 webp file(s).
Codec: h264. Hardware encoder (h264_videotoolbox) available -> using hardware encoding.
Converting stickers/wave.webp -> stickers/wave.mp4
  wave.webp: fps=25.00 (derived from webp frame timing, or 25 default if it has none)
  wave.webp: encoder=h264_videotoolbox (hardware, codec=h264, bitrate=5000k)
  Skipping jump.mp4: already exists (use --force to overwrite)
Done. 1 converted, 1 skipped, 0 failed.
```

## ⚙️ Options & Behavior

| Flag | Effect |
| --- | --- |
| `--force` | Overwrite existing output files without prompting. |
| `--fps FLOAT` | Output frame rate. Default: derived from the webp's own frame timing, or 25 if it has none. |
| `--bitrate STR` | Output video bitrate, e.g. `5000k` or `8M`. Default: `5000k`. Applies to both hardware and software encoders. |
| `--codec {h264,hevc}` | Output video codec. Default: `h264`. `hevc` gives smaller files at similar quality if your player supports it. |
| `--output-dir PATH` | Write all `.mp4` files here instead of next to each source, preserving the source directory structure underneath it. |
| `--jobs, -j INT` | Convert this many files in parallel. Default: `1`. |

**Overwrite behavior if an output `.mp4` already exists:**

* **Interactive terminal:** You will be prompted with `Overwrite? [y/N]` per file.
* **Non-interactive / Scripted:** The file is safely skipped and logged in the final summary so it never blocks waiting for input.
* **Using `--force`:** Always overwrites, bypassing prompts and skips.

Overwrite decisions are always resolved sequentially before any conversion starts (even with `--jobs > 1`), so an interactive prompt never races with a background conversion.

**Exit Codes:**

* `0`: Success (all files converted or intentionally skipped).
* `1`: Failure (one or more files failed, or no `.webp` files were found).

## 📖 Under the Hood

Want to know exactly how we shaved 19 seconds off the conversion time?

* Read [WHY.md](WHY.md) for a deep dive into avoiding disk I/O, overcoming GIL bottlenecks with threaded pipes, and the dangers of naive partial-frame extraction.
* See [benchmark.md](benchmark.md) for the detailed phase-by-phase performance breakdown and isolated `libwebp` decode floors.

## 🧪 Testing

To run the inline correctness and doctests locally:

```bash
uv run fast_webp_to_mp4.py test
```
