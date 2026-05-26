import hashlib
import subprocess
from pathlib import Path

def calculate_sha256(file_path: Path) -> str:
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_video_duration(file_path: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nocrekey=1", str(file_path)
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return int(float(result.stdout.strip()))
    except Exception as e:
        print(f"Ошибка при работе с ffprobe: {e}")
        return 0

def process_and_compress_video(input_path: Path, output_path: Path) -> bool:
    cmd = [
        r"ffmpeg", "-y", "-i", str(input_path),
        "-vf", "scale='min(1920,iw)':'-2'", 
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        str(output_path)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Ошибка FFmpeg: {e.stderr.decode('utf-8', errors='ignore')}")
        return False
