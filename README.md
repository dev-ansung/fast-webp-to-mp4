# fast-webp-to-mp4

Blazing-fast, memory-efficient conversion of animated WebP files to MP4. 

FFmpeg's native decoder cannot read animated WebP files (it only supports single-frame WebPs and fails with `ANIM`/`ANMF` chunk errors). Naively working around this by extracting frames to PNG files before encoding takes upwards of 22 seconds for a short clip. 

This tool solves the problem by decoding frames via Pillow and streaming the raw RGBA bytes directly into FFmpeg's `stdin` over a pipe. It reduces the conversion time of a 301-frame, 1080p animated WebP down to **~2.6 seconds** (an 8.5x improvement).

## ✨ Key Features

* **Zero Disk I/O:** Frames are piped in memory (`-f rawvideo`), completely bypassing the overhead of zlib compression and disk writes.
* **Hardware Acceleration:** Automatically detects and uses Apple's `h264_videotoolbox` hardware encoder on supported Macs. Falls back to `libx264 -preset ultrafast` otherwise.
* **Concurrent Processing:** Uses a producer/consumer threading model so Python can decode frame N+1 while FFmpeg encodes frame N.
* **Correct Partial-Frame Handling:** Accurately detects WebPs that use region-diff (partial) frames and composites them onto a running canvas to prevent ghosted or corrupted output.
* **Batch Processing:** Pass a directory to recursively find and convert all `*.webp` files instantly.

## 🛠 Requirements

- `ffmpeg` installed and available on your system `PATH`.
- Python 3.13+.

*(Note: If you use `uvx`, Python and package dependencies are handled automatically.)*

## 🚀 Usage

You can run this tool directly using `uvx` without needing a local installation.

**Convert a single file:**
```bash
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/file.webp

```

**Convert an entire directory:**
Pass a directory to recursively search for (`/*.webp`). Every match is converted to a same-named `.mp4` next to the original file.

```bash
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/dir

```

### Example Output

```text
$ fast-webp-to-mp4 ./stickers
Found 3 webp file(s). Hardware encode: yes
Converting stickers/wave.webp -> stickers/wave.mp4
Converting stickers/jump.webp -> stickers/jump.mp4
  Skipping party.mp4: already exists (use --force to overwrite)
Done. 2 converted, 1 skipped, 0 failed.

```

## ⚙️ Options & Behavior

| Flag | Effect |
| --- | --- |
| `--force` | Overwrite existing output files without prompting. |

**Overwrite behavior if an output `.mp4` already exists:**

* **Interactive terminal:** You will be prompted with `Overwrite? [y/N]` per file.
* **Non-interactive / Scripted:** The file is safely skipped and logged in the final summary so it never blocks waiting for input.
* **Using `--force`:** Always overwrites, bypassing prompts and skips.

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