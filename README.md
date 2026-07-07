# fast-webp-to-mp4

Convert animated WebP to MP4, fast. ffmpeg's own decoder can't read animated
WebP at all (it only supports single-frame WebP), so this decodes frames via
Pillow and streams them straight into ffmpeg over a pipe — no intermediate
PNG files, no disk I/O per frame. Uses `h264_videotoolbox` hardware encoding
on Macs that support it, falling back to `libx264 -preset ultrafast`
otherwise.

**~2.6 seconds** for a 301-frame, 1072×1520 animated WebP — about 8.5x
faster than the naive "extract to PNGs, then encode" approach (22.3s). See
[benchmark.md](benchmark.md) for the full comparison and
[WHY.md](WHY.md) for why this approach wins.

## Requirements

- `ffmpeg` on `PATH` (with `h264_videotoolbox` if you want hardware encode
  on macOS — check with `ffmpeg -encoders | grep videotoolbox`; otherwise
  it falls back automatically)
- Python 3.13+ (handled automatically by `uv`/`uvx`)

## Usage

Run directly with `uvx`, no local install needed:

```
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/file.webp
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/dir
```

A single file converts just that file. A directory is searched recursively
(`**/*.webp`) and every match is converted to a same-named `.mp4` next to
it (`clip.webp` → `clip.mp4`).

### Example

```
$ fast-webp-to-mp4 ./stickers
Found 3 webp file(s). Hardware encode: yes
Converting stickers/wave.webp -> stickers/wave.mp4
Converting stickers/jump.webp -> stickers/jump.mp4
  Skipping party.mp4: already exists (use --force to overwrite)
Done. 2 converted, 1 skipped, 0 failed.
```

### Flags

| Flag | Effect |
|---|---|
| `--force` | Overwrite existing output files without asking |

### Overwrite behavior

If an output `.mp4` already exists:

- **Interactive terminal**: you're asked `Overwrite? [y/N]` per file.
- **Non-interactive** (piped, scripted, most `uvx` invocations): the file
  is skipped and counted in the final summary — it never blocks waiting
  for input that isn't coming.
- **`--force`**: always overwrites, no prompt, no skip, in either mode.

```
uvx --from git+https://github.com/dev-ansung/fast-webp-to-mp4 fast-webp-to-mp4 path/to/dir --force
```

### Exit codes

`0` if every file converted (or was intentionally skipped); `1` if any
file failed to convert, or if no `.webp` files were found at the given
path.

## How it works, briefly

1. Decode frames with Pillow (`ImageSequence.Iterator`), compositing
   partial-frame updates onto a running canvas so files using
   region-diff frames don't come out ghosted/corrupted.
2. Stream raw RGBA bytes for each frame directly into `ffmpeg`'s stdin
   (`-f rawvideo`) from a background writer thread, so the next frame can
   be decoded while the previous one is still draining into ffmpeg.
3. Encode with `h264_videotoolbox` if available, else `libx264 -preset
   ultrafast`.

No intermediate files ever touch disk. See [WHY.md](WHY.md) for the full
reasoning and [benchmark.md](benchmark.md) for the numbers behind each
step.

## Testing

```
uv run fast_webp_to_mp4.py test
```
