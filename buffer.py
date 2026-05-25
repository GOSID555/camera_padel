import os
import subprocess
from pathlib import Path


class Buffer:
    def __init__(self, segments_dir: str = "segments", segment_duration: int = 2):
        self.segments_dir = Path(segments_dir).resolve()
        self.segment_duration = segment_duration

    def _get_segments(self) -> list[Path]:
        playlist = self.segments_dir / "playlist.m3u8"
        if not playlist.exists():
            return []
        segments = []
        for line in playlist.read_text().splitlines():
            line = line.strip()
            if line.endswith(".ts"):
                p = self.segments_dir / line
                if p.exists():
                    segments.append(p)
        return segments

    def extract_clip(self, duration: int = 20, output_path: str = "web/replay.mp4") -> str | None:
        segments = self._get_segments()
        if not segments:
            print("[Buffer] No segments yet")
            return None

        n_needed = (duration // self.segment_duration) + 1
        clip_segments = segments[-n_needed:]

        concat_path = self.segments_dir / "_concat.txt"
        concat_path.write_text(
            "\n".join(f"file '{s}'" for s in clip_segments) + "\n"
        )

        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(out) + ".tmp")

        cmd = [
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", str(concat_path),
            "-c", "copy",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(tmp), "-y",
        ]
        result = subprocess.run(cmd, capture_output=True)
        concat_path.unlink(missing_ok=True)

        if result.returncode == 0:
            tmp.replace(out)  # atomic rename — no race condition with server
            print(f"[Buffer] Clip ready ({len(clip_segments)} segments, {duration}s)")
            return str(out)

        print(f"[Buffer] ffmpeg error:\n{result.stderr.decode()[-400:]}")
        return None
