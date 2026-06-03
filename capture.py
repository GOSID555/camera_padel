import platform
import subprocess
import os


def get_platform() -> str:
    s = platform.system()
    if s == "Darwin":  return "mac"
    if s == "Windows": return "windows"
    return "linux"


class Capture:
    def __init__(self, output_dir: str = "segments", segment_duration: int = 2,
                 device: str = "0", framerate: int = 30,
                 width: int = 1280, height: int = 720):
        self.output_dir = output_dir
        self.segment_duration = segment_duration
        self.device = device
        self.framerate = framerate
        self.width = width
        self.height = height
        self.process = None
        self.platform = get_platform()
        os.makedirs(output_dir, exist_ok=True)

    def _input_args(self) -> list[str]:
        """คืนค่า ffmpeg input flags ตาม platform"""
        fps = str(self.framerate)
        if self.platform == "mac":
            return ["-f", "avfoundation", "-framerate", fps, "-i", f"{self.device}:none"]
        if self.platform == "windows":
            # device คือชื่อกล้อง เช่น "USB Camera"
            return ["-f", "dshow", "-framerate", fps, "-i", f"video={self.device}"]
        # linux / raspberry pi
        return ["-f", "v4l2", "-framerate", fps, "-i", f"/dev/video{self.device}"]

    def start(self):
        seg_dur = str(self.segment_duration)
        fps = str(self.framerate)
        cmd = [
            "ffmpeg",
            *self._input_args(),
            "-vf", f"scale={self.width}:{self.height},fps=fps={fps}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-crf", "28",
            "-force_key_frames", f"expr:gte(t,n_forced*{seg_dur})",
            "-hls_time", seg_dur,
            "-hls_list_size", "20",
            "-hls_flags", "delete_segments+temp_file",
            "-hls_segment_filename", f"{self.output_dir}/seg_%05d.ts",
            f"{self.output_dir}/playlist.m3u8",
            "-y",
        ]
        # เก็บ stderr ไว้ตรวจว่า ffmpeg เปิดกล้องได้จริง (ไม่ตายเงียบ)
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        print(f"[Capture] {self.platform} | device={self.device} | "
              f"{self.width}x{self.height}@{fps}fps → '{self.output_dir}/'")

    def wait_until_ready(self, timeout: float = 6.0) -> bool:
        """รอจน ffmpeg เขียน playlist สำเร็จ หรือ คืน False ถ้าตายก่อน"""
        import time
        playlist = os.path.join(self.output_dir, "playlist.m3u8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                err = self.process.stderr.read().decode()[-500:] if self.process.stderr else ""
                print(f"[Capture] ffmpeg ตายระหว่างเริ่ม:\n{err}")
                return False
            if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
                return True
            time.sleep(0.2)
        return False

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("[Capture] Stopped")
