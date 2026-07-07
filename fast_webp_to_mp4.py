import argparse
import subprocess
import sys
import threading
import queue
from pathlib import Path
from PIL import Image, ImageSequence

def has_videotoolbox():
    """Return True if ffmpeg's h264_videotoolbox encoder is available (Apple Silicon/Intel Mac hardware encode)."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "h264_videotoolbox" in result.stdout

def build_ffmpeg_cmd(width, height, fps, output_path, use_hw):
    """
    >>> build_ffmpeg_cmd(100, 200, 25.0, "out.mp4", use_hw=False)
    ['ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', '100x200', '-pix_fmt', 'rgba', '-r', '25.0', '-i', '-', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', 'out.mp4']
    >>> build_ffmpeg_cmd(100, 200, 25.0, "out.mp4", use_hw=True)
    ['ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', '100x200', '-pix_fmt', 'rgba', '-r', '25.0', '-i', '-', '-c:v', 'h264_videotoolbox', '-b:v', '5000k', '-pix_fmt', 'yuv420p', 'out.mp4']
    """
    encode_args = (
        ["-c:v", "h264_videotoolbox", "-b:v", "5000k"] if use_hw
        else ["-c:v", "libx264", "-preset", "ultrafast"]
    )
    return [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}", "-pix_fmt", "rgba", "-r", str(fps),
        "-i", "-",
        *encode_args,
        "-pix_fmt", "yuv420p",
        output_path,
    ]

def frame_rate(img):
    """
    Average fps across all frames' declared durations, falling back to 25fps
    for frames (or whole files) with no timing metadata.

    >>> class FakeFrame:
    ...     def __init__(self, duration): self.info = {"duration": duration}
    >>> class FakeImg:
    ...     n_frames = 2
    ...     def __init__(self): self._frames = [FakeFrame(40), FakeFrame(40)]
    ...     def seek(self, i): self._cur = self._frames[i]
    ...     @property
    ...     def info(self): return self._cur.info
    >>> frame_rate(FakeImg())
    25.0
    """
    durations = []
    for i in range(getattr(img, "n_frames", 1)):
        img.seek(i)
        durations.append(img.info.get("duration") or 40)
    avg_ms = sum(durations) / len(durations)
    return 1000.0 / avg_ms if avg_ms > 0 else 25.0

def convert_one(src_path, output_path, use_hw):
    """Convert a single animated webp file to mp4, streaming frames to ffmpeg via stdin."""
    img = Image.open(src_path)
    width, height = img.size
    fps = frame_rate(img)

    cmd = build_ffmpeg_cmd(width, height, fps, str(output_path), use_hw)
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)

    q = queue.Queue(maxsize=16)

    def writer():
        while (data := q.get()) is not None:
            process.stdin.write(data)
        process.stdin.close()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()

    # Partial-frame webp stores only the changed region per frame; composite
    # each onto a running canvas so those frames don't come out corrupted.
    # ImageSequence.Iterator (not a bare .seek() loop) is required for
    # frame.tile to be populated correctly.
    canvas = Image.new("RGBA", (width, height))
    for frame in ImageSequence.Iterator(img):
        if frame.tile and frame.tile[0][1][2:] != img.size:
            canvas.paste(frame, (0, 0), frame.convert("RGBA"))
        else:
            canvas = frame.convert("RGBA")
        q.put(canvas.tobytes())

    q.put(None)
    writer_thread.join()
    stderr = process.stderr.read()
    process.wait()

    if process.returncode != 0:
        print(f"  ffmpeg failed: {stderr.decode(errors='replace')}", file=sys.stderr)
        return False
    return True

def should_write(output_path, force, non_interactive):
    """
    Decide whether it's OK to write output_path, per --force / existing-file /
    interactivity rules.

    >>> import tempfile, os
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "missing.mp4"
    ...     should_write(p, force=False, non_interactive=True)
    True
    """
    if not output_path.exists():
        return True
    if force:
        return True
    if non_interactive:
        print(f"  Skipping {output_path.name}: already exists (use --force to overwrite)")
        return False
    answer = input(f"  {output_path.name} already exists. Overwrite? [y/N] ")
    return answer.strip().lower() == "y"

def find_webp_files(input_path):
    """
    A single file is used as-is; a directory is searched recursively for
    *.webp files.

    >>> import tempfile, os
    >>> with tempfile.TemporaryDirectory() as d:
    ...     os.makedirs(f"{d}/sub")
    ...     open(f"{d}/a.webp", "w").close()
    ...     open(f"{d}/sub/b.webp", "w").close()
    ...     open(f"{d}/notes.txt", "w").close()
    ...     [p.name for p in find_webp_files(d)]
    ['a.webp', 'b.webp']
    """
    path = Path(input_path)
    if path.is_dir():
        return sorted(path.rglob("*.webp"))
    if path.is_file():
        return [path]
    return []

def main():
    parser = argparse.ArgumentParser(description="Convert animated webp file(s) to mp4.")
    parser.add_argument("input", help="A .webp file, or a directory (searched recursively for *.webp), or 'test' to run inline tests.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files without prompting")
    args = parser.parse_args()

    if args.input == "test":
        import doctest
        result = doctest.testmod()
        print(f"Doctests run: {result.attempted}, Failed: {result.failed}")
        sys.exit(1 if result.failed else 0)

    sources = find_webp_files(args.input)
    if not sources:
        print(f"Error: no .webp files found at {args.input}", file=sys.stderr)
        sys.exit(1)

    use_hw = has_videotoolbox()
    non_interactive = not sys.stdin.isatty()
    print(f"Found {len(sources)} webp file(s). Hardware encode: {'yes' if use_hw else 'no (falling back to libx264 ultrafast)'}")

    failures = 0
    skipped = 0
    for src in sources:
        output_path = src.with_suffix(".mp4")
        if not should_write(output_path, args.force, non_interactive):
            skipped += 1
            continue
        print(f"Converting {src} -> {output_path}")
        if not convert_one(src, output_path, use_hw):
            failures += 1

    print(f"Done. {len(sources) - failures - skipped} converted, {skipped} skipped, {failures} failed.")
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
