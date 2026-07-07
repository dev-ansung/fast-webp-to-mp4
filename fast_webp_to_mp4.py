import argparse
import subprocess
import sys
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageSequence

CODEC_ENCODERS = {
    "h264": {"hw": "h264_videotoolbox", "sw": "libx264"},
    "hevc": {"hw": "hevc_videotoolbox", "sw": "libx265"},
}

def has_encoder(name):
    """Return True if ffmpeg reports the named encoder as available."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return name in result.stdout

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

def resolve_fps(img, fps_override=None):
    """
    Output fps: the override if given, otherwise derived from the webp's
    own frame timing.

    >>> class FakeImg:
    ...     n_frames = 1
    ...     def seek(self, i): pass
    ...     info = {"duration": 40}
    >>> resolve_fps(FakeImg())
    25.0
    >>> resolve_fps(FakeImg(), fps_override=10.0)
    10.0
    """
    return fps_override if fps_override else frame_rate(img)

def resolve_encoder(use_hw, codec):
    """
    Which ffmpeg encoder name to use for a codec, hardware or software.

    >>> resolve_encoder(use_hw=True, codec="h264")
    'h264_videotoolbox'
    >>> resolve_encoder(use_hw=False, codec="hevc")
    'libx265'
    """
    encoders = CODEC_ENCODERS[codec]
    return encoders["hw"] if use_hw else encoders["sw"]

def encoder_args(encoder, use_hw, bitrate):
    """
    ffmpeg args selecting the encoder: software encoders also get a
    -preset flag for speed, hardware encoders don't take one.

    >>> encoder_args("libx264", use_hw=False, bitrate="5000k")
    ['-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '5000k']
    >>> encoder_args("h264_videotoolbox", use_hw=True, bitrate="5000k")
    ['-c:v', 'h264_videotoolbox', '-b:v', '5000k']
    """
    if use_hw:
        return ["-c:v", encoder, "-b:v", bitrate]
    return ["-c:v", encoder, "-preset", "ultrafast", "-b:v", bitrate]

@dataclass
class EncodingPlan:
    """
    Everything about *how* to encode one file, decided once up front and
    independent of any I/O -- so the choice of fps/encoder can be tested
    and logged without touching ffmpeg or a real webp file. Each decision
    (fps, encoder, encoder args) is resolved by its own standalone
    function above; this just carries the results.
    """
    fps: float
    fps_is_override: bool
    encoder: str
    use_hw: bool
    bitrate: str
    codec: str

    @classmethod
    def resolve(cls, img, use_hw, codec, bitrate, fps_override=None):
        return cls(
            fps=resolve_fps(img, fps_override=fps_override),
            fps_is_override=bool(fps_override),
            encoder=resolve_encoder(use_hw, codec),
            use_hw=use_hw, bitrate=bitrate, codec=codec,
        )

    def describe(self, label):
        """One or more human-readable lines explaining this plan's decisions."""
        fps_reason = "--fps override" if self.fps_is_override else "derived from webp frame timing, or 25 default if it has none"
        hw_reason = "hardware" if self.use_hw else "software"
        return [
            f"  {label}: fps={self.fps:.2f} ({fps_reason})",
            f"  {label}: encoder={self.encoder} ({hw_reason}, codec={self.codec}, bitrate={self.bitrate})",
        ]

    def ffmpeg_cmd(self, width, height, output_path):
        """
        >>> plan = EncodingPlan(25.0, False, "libx264", False, "5000k", "h264")
        >>> plan.ffmpeg_cmd(100, 200, "out.mp4")
        ['ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', '100x200', '-pix_fmt', 'rgba', '-r', '25.0', '-i', '-', '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '5000k', '-pix_fmt', 'yuv420p', 'out.mp4']
        """
        return [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{width}x{height}", "-pix_fmt", "rgba", "-r", str(self.fps),
            "-i", "-",
            *encoder_args(self.encoder, self.use_hw, self.bitrate),
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]

def composited_frames(img):
    """
    Yield each frame of an animated image as RGBA bytes, compositing
    partial-frame updates onto a running canvas so files using region-diff
    frames don't come out ghosted/corrupted.

    ImageSequence.Iterator (not a bare .seek() loop) is required for
    frame.tile to be populated correctly.
    """
    width, height = img.size
    canvas = Image.new("RGBA", (width, height))
    for frame in ImageSequence.Iterator(img):
        if frame.tile and frame.tile[0][1][2:] != img.size:
            canvas.paste(frame, (0, 0), frame.convert("RGBA"))
        else:
            canvas = frame.convert("RGBA")
        yield canvas.tobytes()

class FfmpegPipe:
    """
    Runs one ffmpeg encode, fed by raw frame bytes pushed via write(), on a
    background thread so the caller can decode the next frame while this
    one is still draining into ffmpeg's stdin.
    """
    def __init__(self, cmd):
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)
        self._queue = queue.Queue(maxsize=16)
        self._thread = threading.Thread(target=self._drain)
        self._thread.start()

    def _drain(self):
        while (data := self._queue.get()) is not None:
            self.process.stdin.write(data)
        self.process.stdin.close()

    def write(self, data):
        self._queue.put(data)

    def close(self):
        """Signal no more frames, wait for ffmpeg to finish. Returns (ok, stderr_text)."""
        self._queue.put(None)
        self._thread.join()
        stderr = self.process.stderr.read()
        self.process.wait()
        return self.process.returncode == 0, stderr.decode(errors="replace")

def convert_one(src_path, output_path, use_hw, fps_override=None, bitrate="5000k", codec="h264"):
    """Convert a single animated webp file to mp4, streaming frames to ffmpeg via stdin."""
    img = Image.open(src_path)
    width, height = img.size

    plan = EncodingPlan.resolve(img, use_hw, codec, bitrate, fps_override=fps_override)
    for line in plan.describe(src_path.name):
        print(line)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pipe = FfmpegPipe(plan.ffmpeg_cmd(width, height, output_path))

    for frame_bytes in composited_frames(img):
        pipe.write(frame_bytes)

    ok, stderr = pipe.close()
    if not ok:
        print(f"  ffmpeg failed: {stderr}", file=sys.stderr)
    return ok

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

def output_path_for(src, input_root, output_dir):
    """
    Where a source file's .mp4 should be written: next to the source by
    default, or under output_dir preserving the path relative to
    input_root when --output-dir is given.

    No output_dir: written next to the source.

    >>> output_path_for(Path("/a/b/c.webp"), Path("/a"), None)
    PosixPath('/a/b/c.mp4')

    With output_dir: relative structure under input_root is preserved.

    >>> output_path_for(Path("/a/b/c.webp"), Path("/a"), Path("/out"))
    PosixPath('/out/b/c.mp4')
    """
    if output_dir is None:
        return src.with_suffix(".mp4")
    rel = src.relative_to(input_root) if src.is_relative_to(input_root) else src.name
    return (output_dir / rel).with_suffix(".mp4")

def parse_args():
    parser = argparse.ArgumentParser(description="Convert animated webp file(s) to mp4.")
    parser.add_argument("input", help="A .webp file, or a directory (searched recursively for *.webp), or 'test' to run inline tests.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files without prompting")
    parser.add_argument("--fps", type=float, default=None, help="Output frame rate (default: derived from the webp's own frame timing, or 25 if it has none)")
    parser.add_argument("--bitrate", default="5000k", help="Output video bitrate, e.g. 5000k or 8M (default: 5000k)")
    parser.add_argument("--codec", choices=list(CODEC_ENCODERS), default="h264", help="Output video codec (default: h264)")
    parser.add_argument("--output-dir", type=Path, default=None, help="Write all .mp4 files here (preserving directory structure) instead of next to each source file")
    parser.add_argument("--jobs", "-j", type=int, default=1, help="Convert this many files in parallel (default: 1)")
    return parser.parse_args()

def plan_batch(sources, input_root, args):
    """
    Resolve every output path and overwrite decision up front, sequentially
    -- can't have multiple threads racing to call input() if --jobs > 1.
    Returns (pairs_to_convert, skipped_count).
    """
    non_interactive = not sys.stdin.isatty()
    to_convert = []
    skipped = 0
    for src in sources:
        output_path = output_path_for(src, input_root, args.output_dir)
        if should_write(output_path, args.force, non_interactive):
            to_convert.append((src, output_path))
        else:
            skipped += 1
    return to_convert, skipped

def run_batch(pairs, use_hw, args):
    def run(pair):
        src, output_path = pair
        print(f"Converting {src} -> {output_path}")
        return convert_one(src, output_path, use_hw, fps_override=args.fps, bitrate=args.bitrate, codec=args.codec)

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            return list(pool.map(run, pairs))
    return [run(pair) for pair in pairs]

def main():
    args = parse_args()

    if args.input == "test":
        import doctest
        result = doctest.testmod()
        print(f"Doctests run: {result.attempted}, Failed: {result.failed}")
        sys.exit(1 if result.failed else 0)

    input_path = Path(args.input)
    sources = find_webp_files(input_path)
    if not sources:
        print(f"Error: no .webp files found at {args.input}", file=sys.stderr)
        sys.exit(1)

    input_root = input_path if input_path.is_dir() else input_path.parent
    hw_encoder = CODEC_ENCODERS[args.codec]["hw"]
    use_hw = has_encoder(hw_encoder)
    print(f"Found {len(sources)} webp file(s).")
    print(f"Codec: {args.codec}. Hardware encoder ({hw_encoder}) {'available' if use_hw else 'not available'} "
          f"-> using {'hardware' if use_hw else 'software (libx264/libx265 ultrafast)'} encoding.")
    if args.output_dir:
        print(f"Output directory: {args.output_dir} (source structure preserved under it)")
    if args.jobs > 1:
        print(f"Running up to {args.jobs} conversions in parallel.")

    to_convert, skipped = plan_batch(sources, input_root, args)
    results = run_batch(to_convert, use_hw, args)

    failures = sum(1 for ok in results if not ok)
    print(f"Done. {len(results) - failures} converted, {skipped} skipped, {failures} failed.")
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
