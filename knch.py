"""
Ambient/lofi channel render script — built for long-term channel health.

Design choices vs. a "mass-produced" pipeline:
  - Real 24fps motion (slow pan/zoom on stills, natural playback on video clips)
    instead of a frozen 1fps frame — this is the single biggest "is this a real
    video" signal on manual review.
  - One fixed-position brand overlay (logo/watermark), same spot every time.
    No randomized timing/position — that pattern reads as duplicate-detection
    evasion, which is worse for you than just not having an overlay at all.
  - Visible on-screen "AI-generated content" label baked into the video, as the
    visual half of disclosure (pair this with YouTube Studio's "Altered or
    synthetic content" toggle at upload time — that's the metadata half, and
    scripts can't set it for you).
  - Every render appends a row to asset_manifest.csv: which song/image was
    used, when, and where it came from — so a Content ID dispute is a
    5-minute copy-paste instead of a guessing game.

You still control curation: this script does NOT auto-fan across every file
in a folder. Point TARGET_MEDIA_NAME at one deliberately chosen source per
run, so each upload is a decision, not a batch output.
"""

import os
import csv
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gdown

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TMP = Path("/tmp/render")
IMAGES_FOLDER = os.environ.get("IMAGES_FOLDER_ID", "")
SONGS_FOLDER = os.environ.get("SONGS_FOLDER_ID", "")

LOGO_PATH = os.environ.get("LOGO_PATH", "")  # local path to your fixed brand logo (png, transparent bg)
CHANNEL_TAG = os.environ.get("CHANNEL_TAG", "")  # short id if you run more than one distinct channel

DURATION = int(os.environ.get("DURATION_SECONDS", str(random.randint(3600, 7200))))
FPS = 24
AUDIO_BITRATE_K = 160
TARGET_SIZE_BYTES = int(float(os.environ.get("TARGET_SIZE_GB", "1.5")) * 1024 * 1024 * 1024)
MIN_SIZE_BYTES = int(1.0 * 1024 * 1024 * 1024)
MAX_SIZE_BYTES = int(1.95 * 1024 * 1024 * 1024)

IMAGE_EXT = (".png", ".jpg", ".jpeg")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi")

TARGET_MEDIA_NAME = os.environ.get("TARGET_MEDIA_NAME")
if not TARGET_MEDIA_NAME:
    raise SystemExit("TARGET_MEDIA_NAME env var not set — pick one source deliberately per run.")

TMP.mkdir(parents=True, exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", "asset_manifest.csv"))


def log_manifest(filename: str, kind: str, source_folder_id: str, note: str = ""):
    """Append one row per asset used in this render. Keep this file forever —
    it's your Content ID dispute evidence and your licensing paper trail."""
    is_new = not MANIFEST_PATH.exists()
    with open(MANIFEST_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp_utc", "filename", "kind", "source_folder_id", "note"])
        w.writerow([datetime.now(timezone.utc).isoformat(), filename, kind, source_folder_id, note])


def download_with_timeout(fn, timeout_sec=1800, label="download"):
    result, error = [None], [None]

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


def retrying_download(folder_id, out_dir, label, attempts=3):
    for attempt in range(attempts):
        try:
            download_with_timeout(
                lambda: gdown.download_folder(id=folder_id, output=str(out_dir), quiet=False),
                timeout_sec=900,
                label=label,
            )
            return
        except Exception as e:
            print(f"[{label}] attempt {attempt + 1} failed: {e}")
            if attempt == attempts - 1:
                raise SystemExit(f"{label} download failed: {e}")
            time.sleep(30)


# ---------------------------------------------------------------------------
# Disk check
# ---------------------------------------------------------------------------
stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free space: {free_gb:.1f} GB")
if free_gb < 4.0:
    raise SystemExit(f"[DISK] Not enough free space ({free_gb:.1f} GB).")

print("Downloading media...")
retrying_download(IMAGES_FOLDER, TMP / "images", "media")
print("Downloading songs...")
retrying_download(SONGS_FOLDER, TMP / "songs", "songs")

matches = list((TMP / "images").rglob(TARGET_MEDIA_NAME))
if not matches:
    raise SystemExit(f"Target media {TARGET_MEDIA_NAME} not found.")
media_path = matches[0]
log_manifest(media_path.name, "background_media", IMAGES_FOLDER,
             "confirm original license/rights for this asset before publishing")

is_video = media_path.suffix.lower() in VIDEO_EXT
is_image = media_path.suffix.lower() in IMAGE_EXT
if not (is_video or is_image):
    raise SystemExit(f"Unsupported media type: {media_path.suffix}")

songs = list((TMP / "songs").glob("*.mp3"))
if not songs:
    raise SystemExit("No songs found.")
random.shuffle(songs)
for s in songs:
    log_manifest(s.name, "music", SONGS_FOLDER,
                 "confirm license (e.g. Pixabay page URL) and record it here manually")

print("Song order:")
for i, s in enumerate(songs):
    print(f"  {i + 1}. {s.name}")

concat_path = TMP / f"concat_{media_path.stem}.txt"
estimated_song_len = 200
repeats = max(1, (DURATION // (len(songs) * estimated_song_len)) + 2)
with open(concat_path, "w") as f:
    for _ in range(repeats):
        batch = songs[:]
        random.shuffle(batch)
        for s in batch:
            f.write(f"file '{s}'\n")

# ---------------------------------------------------------------------------
# Bitrate math — sizing for quality/upload practicality, not for evasion.
# ---------------------------------------------------------------------------
target_bits = TARGET_SIZE_BYTES * 8
target_total_kbps = target_bits / 1000 / DURATION
video_bitrate_k = max(600, int(target_total_kbps - AUDIO_BITRATE_K))
print(f"[BITRATE] video={video_bitrate_k}k audio={AUDIO_BITRATE_K}k")

# ---------------------------------------------------------------------------
# Output naming
# ---------------------------------------------------------------------------
label = CHANNEL_TAG or "render"
ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
output_path = TMP / f"{label}_{media_path.stem}_{ts}.mp4"

print(f"\n>>> MEDIA    : {media_path.name} ({'video' if is_video else 'image'})")
print(f">>> DURATION : {DURATION}s ({DURATION // 60}m {DURATION % 60}s) @ {FPS}fps\n")

# ---------------------------------------------------------------------------
# Filter graph
#   - image source: slow zoompan (real motion, not a frozen frame)
#   - video source: plays at natural speed, looped if shorter than DURATION
#   - fixed-position logo overlay (top-left, subtle, consistent every video)
#   - permanent small "AI-generated content" disclosure label (bottom-right)
# ---------------------------------------------------------------------------
if is_image:
    bg_input_args = ["-loop", "1", "-i", str(media_path)]
    zoom_frames = DURATION * FPS
    bg_filter = (
        f"[0:v]scale=3840:2160:force_original_aspect_ratio=increase,"
        f"crop=3840:2160,"
        f"zoompan=z='min(zoom+0.0006,1.15)':d={zoom_frames}:s=1920x1080:fps={FPS},"
        f"format=yuv420p[bg]"
    )
else:
    bg_input_args = ["-an", "-stream_loop", "-1", "-i", str(media_path)]
    bg_filter = (
        f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p[bg]"
    )

filter_parts = [bg_filter]
video_label = "[bg]"

if LOGO_PATH and Path(LOGO_PATH).exists():
    filter_parts.append("[1:v]scale=140:-1,format=rgba[logo]")
    filter_parts.append(f"{video_label}[logo]overlay=x=30:y=30:format=auto[bglogo]")
    video_label = "[bglogo]"
    logo_input_args = ["-i", LOGO_PATH]
else:
    logo_input_args = []

# Visible AI-content disclosure label, present for the whole video.
disclosure_text = "AI-generated content"
filter_parts.append(
    f"{video_label}drawtext=text='{disclosure_text}':fontcolor=white@0.75:fontsize=22:"
    f"box=1:boxcolor=black@0.35:boxborderw=8:x=w-tw-24:y=h-th-24[outv]"
)

filter_complex = ";".join(filter_parts)

audio_input_index = 1 + (1 if logo_input_args else 0)

cmd = [
    "ffmpeg", "-y",
    *bg_input_args,
    *logo_input_args,
    "-f", "concat", "-safe", "0", "-i", str(concat_path),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", f"{audio_input_index}:a",
    "-c:v", "libx264", "-preset", "medium",
    "-b:v", f"{video_bitrate_k}k",
    "-maxrate", f"{int(video_bitrate_k * 1.2)}k",
    "-bufsize", f"{int(video_bitrate_k * 2)}k",
    "-r", str(FPS), "-g", str(FPS * 2),
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "44100",
    "-movflags", "+faststart",
    str(output_path),
]

print("\nRunning FFmpeg...")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

stopped_by_watcher = False


def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(15)
        if output_path.exists():
            size = output_path.stat().st_size
            print(f"[SIZE] {size / (1024 * 1024):.1f} MB", flush=True)
            if size >= MAX_SIZE_BYTES:
                print("[SIZE] Cap reached — stopping.", flush=True)
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
    raise SystemExit(f"FFmpeg failed: {proc.returncode}")

if not output_path.exists() or output_path.stat().st_size == 0:
    raise SystemExit("No output produced.")

final_size_mb = output_path.stat().st_size / (1024 * 1024)
print(f"\nDONE — {output_path}")
print(f"Size   : {final_size_mb:.1f} MB")
print(f"Manifest updated at: {MANIFEST_PATH.resolve()}")
print("\nReminder before publishing:")
print(" 1. In YouTube Studio upload flow, toggle 'Altered or synthetic content' if applicable.")
print(" 2. Write a real, specific title/description/thumbnail for this upload — no template text.")
print(" 3. Double check every song/image row in asset_manifest.csv has a real license source noted.")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"media_name={media_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
