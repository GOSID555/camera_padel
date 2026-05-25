import subprocess
import os


class Capture:
    def __init__(self, output_dir: str = "segments", segment_duration: int = 2, device: str = "0"):
        self.output_dir = output_dir
        self.segment_duration = segment_duration
        self.device = device
        self.process = None
        os.makedirs(output_dir, exist_ok=True)

    def start(self):
        seg_dur = str(self.segment_duration)
        cmd = [
            "ffmpeg",
            "-f", "avfoundation",
            "-framerate", "30",
            "-i", f"{self.device}:none",
            # fps filter normalizes webcam's broken timestamps
            "-vf", "scale=1280:720,fps=fps=30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-crf", "28",
            # force keyframe every segment_duration seconds → HLS splits cleanly
            "-force_key_frames", f"expr:gte(t,n_forced*{seg_dur})",
            "-hls_time", seg_dur,
            "-hls_list_size", "20",
            "-hls_flags", "delete_segments+temp_file",
            "-hls_segment_filename", f"{self.output_dir}/seg_%05d.ts",
            f"{self.output_dir}/playlist.m3u8",
            "-y",
        ]
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[Capture] Started — segments in '{self.output_dir}/'")

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("[Capture] Stopped")
