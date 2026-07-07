import argparse
import os
import subprocess
import sys
import threading
import time
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

def require_ffmpeg():
    """Exit with a clear message if ffmpeg isn't on PATH, instead of a raw traceback later."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
    except FileNotFoundError:
        print("Error: ffmpeg not found on PATH. Install ffmpeg and try again.", file=sys.stderr)
        sys.exit(1)

def frame_durations_ms(img):
    """
    Each frame's declared duration in milliseconds, or None for any frame
    that has no timing metadata at all. A frame legitimately declaring a
    duration of 0 is kept as 0, distinct from having no timing metadata.

    >>> class FakeFrame:
    ...     def __init__(self, duration): self.info = {"duration": duration}
    >>> class FakeImg:
    ...     n_frames = 2
    ...     def __init__(self): self._frames = [FakeFrame(40), FakeFrame(0)]
    ...     def seek(self, i): self._cur = self._frames[i]
    ...     @property
    ...     def info(self): return self._cur.info
    >>> frame_durations_ms(FakeImg())
    [40, 0]

    >>> class NoDurationKeyImg:
    ...     n_frames = 1
    ...     def seek(self, i): pass
    ...     info = {}
    >>> frame_durations_ms(NoDurationKeyImg())
    [None]
    """
    durations = []
    for i in range(getattr(img, "n_frames", 1)):
        img.seek(i)
        durations.append(img.info.get("duration"))
    return durations

def frame_rate(img):
    """
    Average fps across all frames' declared durations, and whether that
    timing metadata actually existed (vs. every frame missing it, forcing
    a 25fps fallback).

    >>> class FakeFrame:
    ...     def __init__(self, duration): self.info = {"duration": duration}
    >>> class FakeImg:
    ...     n_frames = 2
    ...     def __init__(self): self._frames = [FakeFrame(40), FakeFrame(40)]
    ...     def seek(self, i): self._cur = self._frames[i]
    ...     @property
    ...     def info(self): return self._cur.info
    >>> frame_rate(FakeImg())
    (25.0, True)

    >>> class NoTimingImg:
    ...     n_frames = 2
    ...     def seek(self, i): pass
    ...     info = {}
    >>> frame_rate(NoTimingImg())
    (25.0, False)
    """
    durations = frame_durations_ms(img)
    has_timing = any(d is not None for d in durations)
    avg_ms = sum(40 if d is None else d for d in durations) / len(durations)
    fps = 1000.0 / avg_ms if avg_ms > 0 else 25.0
    return fps, has_timing

def resolve_fps(img, fps_override=None):
    """
    Output fps and whether it came from an override, the webp's own
    timing, or the no-timing-metadata fallback.

    >>> class FakeImg:
    ...     n_frames = 1
    ...     def seek(self, i): pass
    ...     info = {"duration": 40}
    >>> resolve_fps(FakeImg())
    (25.0, 'webp frame timing')
    >>> resolve_fps(FakeImg(), fps_override=10.0)
    (10.0, '--fps override')

    >>> class NoTimingImg:
    ...     n_frames = 1
    ...     def seek(self, i): pass
    ...     info = {}
    >>> resolve_fps(NoTimingImg())
    (25.0, 'default')
    """
    if fps_override:
        return fps_override, "--fps override"
    fps, has_timing = frame_rate(img)
    source = "webp frame timing" if has_timing else "default"
    return fps, source

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
    fps_source: str
    encoder: str
    use_hw: bool
    bitrate: str
    codec: str

    @classmethod
    def resolve(cls, img, use_hw, codec, bitrate, fps_override=None):
        fps, fps_source = resolve_fps(img, fps_override=fps_override)
        return cls(
            fps=fps, fps_source=fps_source,
            encoder=resolve_encoder(use_hw, codec),
            use_hw=use_hw, bitrate=bitrate, codec=codec,
        )

    def describe(self, label):
        """One or more human-readable lines explaining this plan's decisions."""
        hw_reason = "hardware" if self.use_hw else "software"
        return [
            f"  {label}: {self.fps:.2f}fps ({self.fps_source})",
            f"  {label}: encoder={self.encoder} ({hw_reason}, codec={self.codec}, bitrate={self.bitrate})",
        ]

    def ffmpeg_cmd(self, width, height, output_path):
        """
        >>> plan = EncodingPlan(25.0, "webp frame timing", "libx264", False, "5000k", "h264")
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
    Yield each frame of an animated image as RGBA bytes.

    PIL's webp plugin decodes frames via libwebp's WebPAnimDecoder, which
    resolves frame disposal/blending internally and always hands back a
    full, already-composited canvas-sized frame -- never a raw partial
    region. So there is nothing left to composite here; each frame just
    needs decoding. (Verified: frame.tile is always empty by the time a
    frame reaches here, on every frame of every webp file tested.)
    """
    for frame in ImageSequence.Iterator(img):
        yield frame.convert("RGBA").tobytes()

class FfmpegPipe:
    """
    Runs one ffmpeg encode, fed by raw frame bytes pushed via write(), on a
    background thread so the caller can decode the next frame while this
    one is still draining into ffmpeg's stdin. stderr is drained on its own
    thread too -- ffmpeg can otherwise block writing to a full stderr pipe
    while the stdin writer is blocked waiting for ffmpeg to keep reading,
    deadlocking both sides.
    """
    def __init__(self, cmd):
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)
        self._queue = queue.Queue(maxsize=16)
        self._write_error = None
        self._stdin_thread = threading.Thread(target=self._drain_stdin)
        self._stdin_thread.start()
        self._stderr_chunks = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr)
        self._stderr_thread.start()

    def _drain_stdin(self):
        try:
            while (data := self._queue.get()) is not None:
                self.process.stdin.write(data)
        except (BrokenPipeError, OSError) as e:
            self._write_error = e
        finally:
            self.process.stdin.close()

    def _drain_stderr(self):
        self._stderr_chunks.append(self.process.stderr.read())

    def write(self, data):
        self._queue.put(data)

    def close(self):
        """Signal no more frames, wait for ffmpeg to finish. Returns (ok, stderr_text)."""
        self._queue.put(None)
        self._stdin_thread.join()
        self._stderr_thread.join()
        self.process.wait()
        stderr = b"".join(self._stderr_chunks).decode(errors="replace")
        if self._write_error and self.process.returncode == 0:
            # ffmpeg exited 0 but our write to it failed earlier -- treat as a failure
            return False, f"{stderr}\n(writer thread error: {self._write_error})"
        return self.process.returncode == 0, stderr

def convert_one(src_path, output_path, use_hw, fps_override=None, bitrate="5000k", codec="h264"):
    """
    Convert a single animated webp file to mp4, streaming frames to ffmpeg via
    stdin. Never raises -- any failure (corrupt source file, ffmpeg error) is
    reported and returns False, so one bad file in a batch doesn't take down
    every other conversion (especially under --jobs, where an uncaught
    exception in one worker discards the results of all the others).
    """
    start = time.monotonic()
    try:
        with Image.open(src_path) as img:
            width, height = img.size

            plan = EncodingPlan.resolve(img, use_hw, codec, bitrate, fps_override=fps_override)
            for line in plan.describe(src_path.name):
                print(line)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            pipe = FfmpegPipe(plan.ffmpeg_cmd(width, height, output_path))

            for frame_bytes in composited_frames(img):
                pipe.write(frame_bytes)

            ok, stderr = pipe.close()
    except Exception as e:
        ok, stderr = False, f"{type(e).__name__}: {e}"

    elapsed = time.monotonic() - start
    if ok:
        print(f"  {src_path.name}: done in {elapsed:.2f}s")
    else:
        print(f"  {src_path.name}: failed after {elapsed:.2f}s: {stderr}", file=sys.stderr)
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
    input_root when --output-dir is given. src is always expected to be
    under input_root (every caller derives both from the same scan).

    No output_dir: written next to the source.

    >>> output_path_for(Path("/a/b/c.webp"), Path("/a"), None)
    PosixPath('/a/b/c.mp4')

    With output_dir: relative structure under input_root is preserved.

    >>> output_path_for(Path("/a/b/c.webp"), Path("/a"), Path("/out"))
    PosixPath('/out/b/c.mp4')
    """
    if output_dir is None:
        return src.with_suffix(".mp4")
    return (output_dir / src.relative_to(input_root)).with_suffix(".mp4")

def parse_args():
    parser = argparse.ArgumentParser(description="Convert animated webp file(s) to mp4.")
    parser.add_argument("input", help="A .webp file, or a directory (searched recursively for *.webp), or 'test' to run inline tests.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files without prompting")
    parser.add_argument("--fps", type=float, default=None, help="Output frame rate (default: derived from the webp's own frame timing, or 25 if it has none)")
    parser.add_argument("--bitrate", default="5000k", help="Output video bitrate, e.g. 5000k or 8M (default: 5000k)")
    parser.add_argument("--codec", choices=list(CODEC_ENCODERS), default="h264", help="Output video codec (default: h264)")
    parser.add_argument("--output-dir", type=Path, default=None, help="Write all .mp4 files here (preserving directory structure) instead of next to each source file")
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 1, help="Convert this many files in parallel (default: number of CPUs)")
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

    require_ffmpeg()

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

    batch_start = time.monotonic()
    to_convert, skipped = plan_batch(sources, input_root, args)
    results = run_batch(to_convert, use_hw, args)
    batch_elapsed = time.monotonic() - batch_start

    failures = sum(1 for ok in results if not ok)
    print(f"Done in {batch_elapsed:.2f}s. {len(results) - failures} converted, {skipped} skipped, {failures} failed.")
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
