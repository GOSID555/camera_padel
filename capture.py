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
        self.encoder = self._resolve_encoder()
        os.makedirs(output_dir, exist_ok=True)

    def _resolve_encoder(self) -> str:
        """เลือก H.264 encoder ของ output HLS ตามฮาร์ดแวร์ — เก็บไว้ที่เดียว
        เพราะ _input_args() ต้องรู้ด้วยว่าจะเปิด GPU decode (NVDEC) ได้ไหม"""
        enc = os.environ.get("REPLAY_ENCODER")
        if enc:
            return enc
        if self.platform == "windows":  return "h264_nvenc"        # GTX 1660 Ti ฯลฯ
        if self.platform == "mac":      return "h264_videotoolbox"  # GPU บน Apple Silicon/Intel
        return "libx264"

    def _use_cuda_frames(self) -> bool:
        """RTSP + NVENC = มี NVIDIA GPU → เดินสายทั้งหมดบน GPU (NVDEC→scale_cuda→NVENC)"""
        return (self.device.startswith(("rtsp://", "rtsps://"))
                and self.encoder == "h264_nvenc")

    def _input_args(self) -> list[str]:
        """คืนค่า ffmpeg input flags ตาม platform / ชนิดกล้อง"""
        fps = str(self.framerate)
        # กล้อง IP / CCTV — device เป็น RTSP URL (ใช้ได้ทุก OS, ไม่ผูกกับ dshow/avfoundation)
        # ไม่ใส่ -framerate/-video_size (เป็น flag ของ capture device) — ปล่อยตาม stream ต้นทาง
        # แล้วค่อย scale/fps ที่ฝั่ง output เหมือนกล้อง USB
        if self.device.startswith(("rtsp://", "rtsps://")):
            # NVENC = มี NVIDIA GPU → decode บน GPU (NVDEC) แล้ว "คงเฟรมไว้บน GPU"
            # ด้วย -hwaccel_output_format cuda → scale_cuda + nvenc ทำงานครบสายบน GPU
            # ไม่ download เฟรม (เช่นกล้อง 4K) กลับ CPU เลย = ตัด decode+scale ออกจาก CPU
            # ทั้งหมด (ทำได้เพราะตอนนี้เหลือ output เดียว ไม่มี MJPEG preview ตัวที่ 2)
            if self._use_cuda_frames():
                return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                        "-rtsp_transport", "tcp", "-i", self.device]
            return ["-rtsp_transport", "tcp", "-i", self.device]
        if self.platform == "mac":
            return ["-f", "avfoundation", "-framerate", fps, "-i", f"{self.device}:none"]
        if self.platform == "windows":
            # device คือชื่อกล้อง เช่น "USB Camera"
            # USB webcam ส่วนใหญ่ต้องระบุ video_size + vcodec mjpeg ไม่งั้น dshow เปิดไม่ได้
            return ["-f", "dshow", "-framerate", fps,
                    "-video_size", f"{self.width}x{self.height}",
                    "-vcodec", "mjpeg",
                    "-i", f"video={self.device}"]
        # linux / raspberry pi
        return ["-f", "v4l2", "-framerate", fps, "-i", f"/dev/video{self.device}"]

    def _video_codec_args(self) -> list[str]:
        """เลือก encoder ของ output HLS ตามฮาร์ดแวร์ที่มี
        สำคัญมาก: ใช้ GPU encode (NVENC/VideoToolbox) แทน libx264 ที่กิน CPU 100%
        ทุกคอร์ — หลายคอร์ทพร้อมกันบนโน้ตบุ๊กทำให้ CPU ร้อนจนเครื่อง thermal-shutdown
        override ได้ด้วย env REPLAY_ENCODER=libx264|h264_nvenc|h264_videotoolbox|h264_amf"""
        enc = self.encoder
        # GOP = 1 keyframe ต่อ 1 segment — สำคัญสำหรับ HLS: GPU encoder ไม่แทรก
        # keyframe ตาม -force_key_frames เหมือน libx264 ถ้าไม่บังคับ -g/-forced-idr
        # segment จะไม่ปิด → playlist ไม่ถูกเขียน → wait_until_ready ล้มเหลว
        gop = str(self.framerate * self.segment_duration)
        if enc == "h264_nvenc":
            # tune ll=low-latency, ไม่มี B-frame, คุมคุณภาพด้วย cq, IDR ทุก GOP
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                    "-rc", "vbr", "-cq", "28", "-b:v", "0", "-bf", "0",
                    "-g", gop, "-forced-idr", "1"]
        if enc == "h264_amf":
            return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                    "-qp_i", "28", "-qp_p", "28", "-bf", "0", "-g", gop]
        if enc == "h264_videotoolbox":
            return ["-c:v", "h264_videotoolbox", "-q:v", "55", "-realtime", "1",
                    "-g", gop]
        # software fallback (กิน CPU — ใช้เมื่อไม่มี GPU encoder)
        return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "28"]

    def start(self):
        seg_dur = str(self.segment_duration)
        fps = str(self.framerate)
        cmd = [
            "ffmpeg",
            # ลด stderr ให้เหลือเฉพาะ error — กัน PIPE buffer เต็มจน ffmpeg block
            # (stderr=PIPE ไม่ถูกอ่านจนกว่า process จะตาย ใน wait_until_ready)
            "-hide_banner", "-loglevel", "error",
            *self._input_args(),
            # ── output เดียว: HLS segments → ทั้งตัดคลิป replay และพรีวิวสดบน dashboard ──
            # dashboard เล่น playlist.m3u8 นี้ตรงๆ ผ่าน hls.js → server ไม่ต้อง encode
            # preview แยก (เดิมมี output MJPEG ตัวที่ 2 กิน CPU ตลอดเวลา — ถอดออกแล้ว)
            # scale_cuda = ย่อบน GPU (เมื่อเฟรมอยู่บน GPU แล้ว), ไม่งั้น scale บน CPU
            "-vf", f"{'scale_cuda' if self._use_cuda_frames() else 'scale'}="
                   f"{self.width}:{self.height},fps=fps={fps}",
            "-an",                       # ตัดเสียง (กล้อง IP บางตัวมี audio — output ไม่ต้องใช้)
            *self._video_codec_args(),
            "-force_key_frames", f"expr:gte(t,n_forced*{seg_dur})",
            "-hls_time", seg_dur,
            "-hls_list_size", "20",
            "-hls_flags", "delete_segments+temp_file",
            "-hls_segment_filename", f"{self.output_dir}/seg_%05d.ts",
            f"{self.output_dir}/playlist.m3u8",
            "-y",
        ]
        # stderr = เก็บไว้ตรวจว่า ffmpeg เปิดกล้องได้จริง (ไม่ตายเงียบ); stdout ไม่ใช้แล้ว
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
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
