import os
import random
import subprocess
import threading
import time
import urllib.request
import re
from pathlib import Path
import gdown

# ── Config ────────────────────────────────────────────────────────────────────
TMP             = Path("/tmp/redsky")
IMAGES_FOLDER   = "1bbYxw2pNbVS05liS0pObjxevuJ-BdXck"
SONGS_FOLDER    = "1DILwSnl-m4yY2w5J29hIlv19DnzNzVm_"
SUB_FOLDER      = "1mpk5rcZRtVcjrKwrsrSIyfu6TzDWZiTl"
DURATION        = random.randint(18000, 36000)  # 5–10 hrs, size watcher stops it first
VIDEO_KBPS      = 2500
AUDIO_BITRATE_K = 128
MAX_SIZE_BYTES  = int(1.8 * 1024 * 1024 * 1024)  # 1.8 GB hard cap

TARGET_IMAGE_NAME = os.environ.get("TARGET_IMAGE_NAME")
if not TARGET_IMAGE_NAME:
    raise SystemExit("TARGET_IMAGE_NAME env var not set — this script expects "
                     "to be run as one leg of the render matrix.")

# ── Setup dirs ────────────────────────────────────────────────────────────────
TMP.mkdir(exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)
(TMP / "sub").mkdir(exist_ok=True)

# ── Download assets ───────────────────────────────────────────────────────────
print("Downloading images...")
gdown.download_folder(id=IMAGES_FOLDER, output=str(TMP / "images"), quiet=False)

print("Downloading songs...")
gdown.download_folder(id=SONGS_FOLDER, output=str(TMP / "songs"), quiet=False)

print("Downloading subscribe button...")
gdown.download_folder(id=SUB_FOLDER, output=str(TMP / "sub"), quiet=False)

# ── Find the target image ─────────────────────────────────────────────────────
matches = list((TMP / "images").rglob(TARGET_IMAGE_NAME))
if not matches:
    raise SystemExit(f"Target image {TARGET_IMAGE_NAME} not found after download! "
                     f"It may have been renamed/removed in Drive since discovery ran.")

image_path = matches[0]
subs = (list((TMP / "sub").glob("*.mp4")) +
        list((TMP / "sub").glob("*.mov")) +
        list((TMP / "sub").glob("*.webm")))
if not subs:
    raise SystemExit("No subscribe button video found.")
sub_path    = subs[0]
output_path = TMP / f"OUT_{image_path.stem}.mp4"

print(f"Using image : {image_path.name}")
print(f"Using sub   : {sub_path.name}")
print(f"Duration    : {DURATION}s ({DURATION//60}m {DURATION%60}s) — will stop earlier at 1.8 GB")

# ── Try to get Drive file ID for image preview ────────────────────────────────
try:
    req = urllib.request.Request(
        f"https://drive.google.com/drive/folders/{IMAGES_FOLDER}",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    html = urllib.request.urlopen(req).read().decode("utf-8")
    name_id_matches = re.findall(r'"(1[a-zA-Z0-9_-]{25,})"[^}]*?"([^"]+\.(?:jpg|jpeg|png))"', html, re.IGNORECASE)
    file_id = None
    for fid, fname in name_id_matches:
        if fname.lower() == image_path.name.lower():
            file_id = fid
            break
    if file_id:
        (TMP / f"image_id_{image_path.stem}.txt").write_text(file_id)
        print(f">>> Drive file ID: {file_id}")
    else:
        print(">>> Could not extract Drive file ID — summary will show filename only")
except Exception as e:
    print(f">>> Drive ID lookup failed: {e}")

# ── Shuffle songs + concat ────────────────────────────────────────────────────
songs = list((TMP / "songs").glob("*.mp3"))
if not songs:
    raise SystemExit("No songs found!")
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
    t += 900  # 15 minutes
enable_parts = "+".join([f"between(t,{s},{s+4})" for s in intervals])

filter_complex = (
    f"[1:v]scale=220:-1,"
    f"chromakey=0x00ff00:0.3:0.1[sub];"
    f"[0:v][sub]overlay=W-w-30:H-h-30:enable='{enable_parts}',"
    f"format=yuv420p[outv]"
)

# ── FFmpeg ────────────────────────────────────────────────────────────────────
cmd = [
    "ffmpeg", "-y",
    "-loop", "1", "-i", str(image_path),
    "-stream_loop", "-1", "-i", str(sub_path),
    "-f", "concat", "-safe", "0", "-i", str(concat_path),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", "2:a",
    "-c:v", "libx264", "-preset", "ultrafast",
    "-crf", "23",
    "-b:v", f"{VIDEO_KBPS}k", "-maxrate", f"{VIDEO_KBPS}k", "-bufsize", f"{VIDEO_KBPS * 2}k",
    "-profile:v", "high", "-level", "4.1", "-r", "24", "-g", "48",
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "44100",
    "-movflags", "+faststart",
    "-shortest",
    str(output_path),
]

# ── Run FFmpeg with live size watcher ─────────────────────────────────────────
print("\nRunning FFmpeg...")
proc = subprocess.Popen(cmd)

stopped_by_watcher = False

def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(10)
        if output_path.exists():
            size = output_path.stat().st_size
            mb   = size / (1024 * 1024)
            gb   = size / (1024 * 1024 * 1024)
            print(f"[SIZE] {output_path.name} → {mb:.1f} MB ({gb:.3f} GB)", flush=True)
            if size >= MAX_SIZE_BYTES:
                print(f"[SIZE] ⚠️  Hit 1.8 GB cap — stopping FFmpeg cleanly.", flush=True)
                stopped_by_watcher = True
                proc.terminate()
                break

watcher = threading.Thread(target=size_watcher, daemon=True)
watcher.start()
proc.wait()
watcher.join()

if proc.returncode not in (0, -15):
    raise SystemExit("FFmpeg failed — check output above.")

final_size    = output_path.stat().st_size
final_size_mb = final_size / (1024 * 1024)
final_size_gb = final_size / (1024 * 1024 * 1024)
stop_reason   = "capped at 1.8 GB by size watcher" if stopped_by_watcher else "duration reached"

print(f"\nDONE — {output_path}")
print(f"Stop reason : {stop_reason}")
print(f"Size        : {final_size_mb:.1f} MB ({final_size_gb:.3f} GB)")
print(f"Image       : {image_path.name}")

# ── Expose values to workflow ─────────────────────────────────────────────────
github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"image_name={image_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")

  
