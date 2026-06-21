import os
import random
import subprocess
import threading
import time
from pathlib import Path
import gdown

# ── Config ────────────────────────────────────────────────────────────────────
TMP             = Path("/tmp/redsky")
IMAGES_FOLDER   = "1bbYxw2pNbVS05liS0pObjxevuJ-BdXck"
SONGS_FOLDER    = "1DILwSnl-m4yY2w5J29hIlv19DnzNzVm_"
SUB_FOLDER      = "1mpk5rcZRtVcjrKwrsrSIyfu6TzDWZiTl"
DURATION        = random.randint(18000, 28800)
AUDIO_BITRATE_K = 128
MAX_SIZE_BYTES  = int(1.8 * 1024 * 1024 * 1024)

TARGET_IMAGE_NAME = os.environ.get("TARGET_IMAGE_NAME")
if not TARGET_IMAGE_NAME:
    raise SystemExit("TARGET_IMAGE_NAME env var not set.")

# ── Timeout helper ────────────────────────────────────────────────────────────
def download_with_timeout(fn, timeout_sec=1800, label="download"):
    result = [None]
    error  = [None]
    def worker():
        try:
            result[0] = fn()
        except Exception as e:
            error[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout_sec}s")
    if error[0]:
        raise error[0]
    return result[0]

# ── Setup dirs ────────────────────────────────────────────────────────────────
TMP.mkdir(exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)
(TMP / "sub").mkdir(exist_ok=True)

# ── Disk space check ──────────────────────────────────────────────────────────
stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free space: {free_gb:.1f} GB")
if free_gb < 4.0:
    raise SystemExit(f"[DISK] Not enough free space ({free_gb:.1f} GB). Need at least 4 GB.")

# ── Download assets ───────────────────────────────────────────────────────────
print("Downloading images folder...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=IMAGES_FOLDER, output=str(TMP / "images"), quiet=False),
            timeout_sec=900, label="images folder"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"Failed to download images folder: {e}")
        time.sleep(30)

print("Downloading songs folder...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=SONGS_FOLDER, output=str(TMP / "songs"), quiet=False),
            timeout_sec=900, label="songs folder"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"Failed to download songs folder: {e}")
        time.sleep(30)

print("Downloading subscribe button folder...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=SUB_FOLDER, output=str(TMP / "sub"), quiet=False),
            timeout_sec=600, label="sub folder"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"Failed to download sub folder: {e}")
        time.sleep(30)

# ── Find the target image ─────────────────────────────────────────────────────
matches = list((TMP / "images").rglob(TARGET_IMAGE_NAME))
if not matches:
    raise SystemExit(f"Target image {TARGET_IMAGE_NAME} not found after download.")

image_path = matches[0]

subs = (list((TMP / "sub").glob("*.mp4")) +
        list((TMP / "sub").glob("*.mov")) +
        list((TMP / "sub").glob("*.webm")))
if not subs:
    raise SystemExit("No subscribe button video found in sub folder.")
sub_path    = subs[0]
output_path = TMP / f"OUT_{image_path.stem}.mp4"

print(f"Using image : {image_path.name}")
print(f"Using sub   : {sub_path.name}")
print(f"Duration cap: {DURATION}s — stops earlier at 1.8 GB")

# ── Disk check after downloads ────────────────────────────────────────────────
stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free after downloads: {free_gb:.1f} GB")
if free_gb < 2.0:
    raise SystemExit(f"[DISK] Not enough space to render ({free_gb:.1f} GB free).")

# ── Shuffle songs + build concat list ────────────────────────────────────────
songs = list((TMP / "songs").glob("*.mp3"))
if not songs:
    raise SystemExit("No songs found in songs folder.")
random.shuffle(songs)
print("Song order:")
for i, s in enumerate(songs):
    print(f"  {i+1}. {s.name}")

concat_path = TMP / f"concat_{image_path.stem}.txt"
estimated_song_len = 200
repeats_needed = max(1, (DURATION // (len(songs) * estimated_song_len)) + 2)
with open(concat_path, "w") as f:
    for _ in range(repeats_needed):
        for s in songs:
            f.write(f"file '{s}'\n")

# ── Subscribe overlay timing (every 15 min, shows 4 sec) ─────────────────────
intervals = []
t = 30
while t < DURATION - 10:
    intervals.append(t)
    t += 900
enable_parts = "+".join([f"between(t,{s},{s+4})" for s in intervals])

# ── Filter graph ──────────────────────────────────────────────────────────────
filter_complex = (
    f"[1:v]scale=220:-1,"
    f"chromakey=0x00ff00:0.3:0.1[sub];"
    f"[0:v][sub]overlay=W-w-30:H-h-30:enable='{enable_parts}',"
    f"scale=1920:1080:force_original_aspect_ratio=decrease,"
    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
    f"format=yuv420p[outv]"
)

# ── FFmpeg command ────────────────────────────────────────────────────────────
cmd = [
    "ffmpeg", "-y",
    "-loop", "1", "-framerate", "1", "-i", str(image_path),
    "-stream_loop", "-1", "-i", str(sub_path),
    "-f", "concat", "-safe", "0", "-i", str(concat_path),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", "2:a",
    "-c:v", "libx264", "-preset", "ultrafast",
    "-tune", "stillimage",
    "-crf", "28",
    "-r", "1", "-g", "2",
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "44100",
    "-movflags", "+faststart",
    str(output_path),
]

# ── Run FFmpeg with live output + size watcher ────────────────────────────────
print("\nRunning FFmpeg...")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

stopped_by_watcher = False

def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(15)
        if output_path.exists():
            size = output_path.stat().st_size
            mb   = size / (1024 * 1024)
            gb   = size / (1024 * 1024 * 1024)
            print(f"[SIZE] {output_path.name} → {mb:.1f} MB ({gb:.3f} GB)", flush=True)
            if size >= MAX_SIZE_BYTES:
                print("[SIZE] ⚠️  Hit 1.8 GB cap — stopping FFmpeg cleanly.", flush=True)
                stopped_by_watcher = True
                proc.terminate()
                break

watcher = threading.Thread(target=size_watcher, daemon=True)
watcher.start()

for line in proc.stdout:
    print(line, end="", flush=True)

proc.wait()
watcher.join()

if not stopped_by_watcher and proc.returncode != 0:
    raise SystemExit(f"FFmpeg failed with exit code {proc.returncode}")

if not output_path.exists() or output_path.stat().st_size == 0:
    raise SystemExit("Output file missing after FFmpeg.")

final_size    = output_path.stat().st_size
final_size_mb = final_size / (1024 * 1024)
final_size_gb = final_size / (1024 * 1024 * 1024)
stop_reason   = "capped at 1.8 GB by size watcher" if stopped_by_watcher else "duration reached"

print(f"\nDONE — {output_path}")
print(f"Stop reason : {stop_reason}")
print(f"Size        : {final_size_mb:.1f} MB ({final_size_gb:.3f} GB)")
print(f"Image       : {image_path.name}")

# ── Write outputs for workflow ────────────────────────────────────────────────
github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"image_name={image_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
