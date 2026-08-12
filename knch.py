"""
Ambient/lofi channel render script.

Pipeline:
  - Background image with a continuous oscillating zoom: 7s zoom-in,
    7s zoom-out, forever (period = 14s).
  - Fixed-position brand logo overlay (optional), same spot every render.
  - Visible "AI-generated content" disclosure label baked into the video
    (bottom-left) as the visual half of disclosure. Pair this with
    YouTube Studio's "Altered or synthetic content" toggle at upload time
    -- that's the metadata half, this script can't set it for you.
  - A green-screen "Subscribe" clip keyed and overlaid bottom-right, on a
    fixed position, appearing repeatedly at randomized 3-7 minute gaps.
  - A thumbnail frame pulled from the finished render, so releases are
    identifiable at a glance instead of just a filename+timestamp.
  - asset_manifest.csv logs every asset used per render. You still need to
    actually confirm/own the rights to every image/song/overlay you point
    this at -- the manifest is a paper trail, not a license.

If TARGET_MEDIA_NAME isn't set, one image/video is picked automatically
from the downloaded images folder (uniform random) so this can run
unattended end-to-end.
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
SUB_FILE_ID = os.environ.get("SUB_FILE_ID", "")

LOGO_PATH = os.environ.get("LOGO_PATH", "")
CHANNEL_TAG = os.environ.get("CHANNEL_TAG", "")

DURATION = int(os.environ.get("DURATION_SECONDS") or random.randint(3600, 10800))
FPS = 24

ZOOM_MIN = float(os.environ.get("ZOOM_MIN", "1.0"))
ZOOM_MAX = float(os.environ.get("ZOOM_MAX", "1.15"))
ZOOM_PERIOD_SECONDS = 14.0

AUDIO_BITRATE_K = 192
TARGET_SIZE_GB = float(os.environ.get("TARGET_SIZE_GB", "1.5"))
TARGET_SIZE_BYTES = int(TARGET_SIZE_GB * 1024 * 1024 * 1024)
MIN_SIZE_BYTES = int(1.0 * 1024 * 1024 * 1024)
MAX_SIZE_BYTES = int(1.9 * 1024 * 1024 * 1024)

SUB_GAP_MIN_SEC = 180
SUB_GAP_MAX_SEC = 420
SUB_MAX_SHOW_SECONDS = float(os.environ.get("SUB_MAX_SHOW_SECONDS", "6"))
SUB_CHROMA_COLOR = os.environ.get("SUB_CHROMA_COLOR", "0x00FF00")
SUB_CHROMA_SIMILARITY = os.environ.get("SUB_CHROMA_SIMILARITY", "0.18")
SUB_CHROMA_BLEND = os.environ.get("SUB_CHROMA_BLEND", "0.06")
SUB_SCALE_WIDTH = os.environ.get("SUB_SCALE_WIDTH", "340")

IMAGE_EXT = (".png", ".jpg", ".jpeg")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi")

TARGET_MEDIA_NAME = os.environ.get("TARGET_MEDIA_NAME", "").strip()

TMP.mkdir(parents=True, exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", "asset_manifest.csv"))


def log_manifest(filename: str, kind: str, source_folder_id: str, note: str = ""):
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


def retrying_download_folder(folder_id, out_dir, label, attempts=3):
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


def retrying_download_file(file_id, out_path, label, attempts=3):
    for attempt in range(attempts):
        try:
            download_with_timeout(
                lambda: gdown.download(id=file_id, output=str(out_path), quiet=False, fuzzy=True),
                timeout_sec=600,
                label=label,
            )
            if Path(out_path).exists():
                return
            raise RuntimeError("file not found after download")
        except Exception as e:
            print(f"[{label}] attempt {attempt + 1} failed: {e}")
            if attempt == attempts - 1:
                raise SystemExit(f"{label} download failed: {e}")
            time.sleep(30)


def probe_duration(path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.strip())


# ---------------------------------------------------------------------------
# Disk check
# ---------------------------------------------------------------------------
stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free space: {free_gb:.1f} GB")
if free_gb < 4.0:
    raise SystemExit(f"[DISK] Not enough free space ({free_gb:.1f} GB).")

print("Downloading media...")
retrying_download_folder(IMAGES_FOLDER, TMP / "images", "media")
print("Downloading songs...")
retrying_download_folder(SONGS_FOLDER, TMP / "songs", "songs")

sub_path = None
if SUB_FILE_ID:
    sub_path = TMP / "subscribe_overlay.mp4"
    print("Downloading subscribe overlay clip...")
    retrying_download_file(SUB_FILE_ID, sub_path, "subscribe overlay")
    log_manifest(sub_path.name, "subscribe_overlay", SUB_FILE_ID,
                 "confirm you have rights to use/host this asset")

# ---------------------------------------------------------------------------
# Pick source media -- explicit name if given, otherwise auto-pick so this
# can run fully unattended.
# ---------------------------------------------------------------------------
all_candidates = [
    p for p in (TMP / "images").rglob("*")
    if p.is_file() and p.suffix.lower() in (IMAGE_EXT + VIDEO_EXT)
]
if not all_candidates:
    raise SystemExit("No usable images/videos found in the images folder.")

if TARGET_MEDIA_NAME:
    matches = [p for p in all_candidates if p.name == TARGET_MEDIA_NAME]
    if not matches:
        raise SystemExit(f"Target media {TARGET_MEDIA_NAME} not found.")
    media_path = matches[0]
    pick_note = "explicitly requested"
else:
    media_path = random.choice(all_candidates)
    pick_note = "auto-picked"
print(f"[MEDIA] Using {media_path.name} ({pick_note})")

log_manifest(media_path.name, "background_media", IMAGES_FOLDER,
             f"{pick_note}; confirm original license/rights for this asset before publishing")

is_video = media_path.suffix.lower() in VIDEO_EXT
is_image = media_path.suffix.lower() in IMAGE_EXT

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
# Bitrate math -- hit TARGET_SIZE_GB for this DURATION, hard-capped at
# MAX_SIZE_BYTES (1.9GB) by the size watcher below, floor-checked at 1.0GB.
# preset=slow (vs medium) buys meaningfully better quality per bit at the
# cost of slower encode -- worth it since quality-per-MB is the actual ask.
# ---------------------------------------------------------------------------
target_bits = TARGET_SIZE_BYTES * 8
target_total_kbps = target_bits / 1000 / DURATION
video_bitrate_k = max(800, int(target_total_kbps - AUDIO_BITRATE_K))
print(f"[BITRATE] video={video_bitrate_k}k audio={AUDIO_BITRATE_K}k")

# ---------------------------------------------------------------------------
# Subscribe overlay schedule
# ---------------------------------------------------------------------------
sub_schedule = []
if sub_path:
    sub_clip_duration = probe_duration(sub_path)
    show_len = min(sub_clip_duration, SUB_MAX_SHOW_SECONDS) if SUB_MAX_SHOW_SECONDS else sub_clip_duration
    t = random.uniform(10, 45)
    while t + show_len < DURATION - 5:
        sub_schedule.append((t, t + show_len))
        t += show_len + random.uniform(SUB_GAP_MIN_SEC, SUB_GAP_MAX_SEC)
    print(f"[SUB] {len(sub_schedule)} showings scheduled, each ~{show_len:.1f}s, "
          f"gaps {SUB_GAP_MIN_SEC//60}-{SUB_GAP_MAX_SEC//60} min")

# ---------------------------------------------------------------------------
# Output naming
# ---------------------------------------------------------------------------
label = CHANNEL_TAG or "render"
ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
output_path = TMP / f"{label}_{media_path.stem}_{ts}.mp4"
thumb_path = TMP / f"{label}_{media_path.stem}_{ts}_thumb.jpg"

print(f"\n>>> MEDIA    : {media_path.name} ({'video' if is_video else 'image'})")
print(f">>> DURATION : {DURATION}s ({DURATION // 60}m {DURATION % 60}s) @ {FPS}fps")
print(f">>> ZOOM     : {ZOOM_MIN}-{ZOOM_MAX}, {ZOOM_PERIOD_SECONDS/2:.0f}s in / {ZOOM_PERIOD_SECONDS/2:.0f}s out, looping\n")

# ---------------------------------------------------------------------------
# Filter graph
# ---------------------------------------------------------------------------
if is_image:
    bg_input_args = ["-loop", "1", "-i", str(media_path)]
    zoom_frames = DURATION * FPS
    period_frames = ZOOM_PERIOD_SECONDS * FPS
    amp = (ZOOM_MAX - ZOOM_MIN) / 2
    mid = ZOOM_MIN + amp
    zoom_expr = f"{mid}+{amp}*sin(2*PI*on/{period_frames})"
    bg_filter = (
        f"[0:v]scale=3840:2160:force_original_aspect_ratio=increase,"
        f"crop=3840:2160,"
        f"zoompan=z='{zoom_expr}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={zoom_frames}:s=1920x1080:fps={FPS},"
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
next_input_index = 1

logo_input_args = []
if LOGO_PATH and Path(LOGO_PATH).exists():
    logo_idx = next_input_index
    logo_input_args = ["-i", LOGO_PATH]
    filter_parts.append(f"[{logo_idx}:v]scale=140:-1,format=rgba[logo]")
    filter_parts.append(f"{video_label}[logo]overlay=x=30:y=30:format=auto[bglogo]")
    video_label = "[bglogo]"
    next_input_index += 1

sub_input_args = []
if sub_path and sub_schedule:
    sub_idx = next_input_index
    sub_input_args = ["-an", "-stream_loop", "-1", "-i", str(sub_path)]
    enable_expr = "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in sub_schedule)
    filter_parts.append(
        f"[{sub_idx}:v]chromakey={SUB_CHROMA_COLOR}:{SUB_CHROMA_SIMILARITY}:{SUB_CHROMA_BLEND},"
        f"scale={SUB_SCALE_WIDTH}:-1[subkeyed]"
    )
    filter_parts.append(
        f"{video_label}[subkeyed]overlay=x=main_w-overlay_w-30:y=main_h-overlay_h-30:"
        f"enable='{enable_expr}':format=auto[bgsub]"
    )
    video_label = "[bgsub]"
    next_input_index += 1

disclosure_text = "AI-generated content"
filter_parts.append(
    f"{video_label}drawtext=text='{disclosure_text}':fontcolor=white@0.75:fontsize=22:"
    f"box=1:boxcolor=black@0.35:boxborderw=8:x=24:y=h-th-24[outv]"
)

filter_complex = ";".join(filter_parts)
audio_input_index = next_input_index

cmd = [
    "ffmpeg", "-y",
    *bg_input_args,
    *logo_input_args,
    *sub_input_args,
    "-f", "concat", "-safe", "0", "-i", str(concat_path),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", f"{audio_input_index}:a",
    "-c:v", "libx264", "-preset", "slow", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-b:v", f"{video_bitrate_k}k",
    "-maxrate", f"{int(video_bitrate_k * 1.15)}k",
    "-bufsize", f"{int(video_bitrate_k * 2)}k",
    "-r", str(FPS), "-g", str(FPS * 2),
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "48000",
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
                print("[SIZE] Cap reached (1.9GB) -- stopping.", flush=True)
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
final_size_bytes = output_path.stat().st_size
print(f"\nDONE -- {output_path}")
print(f"Size   : {final_size_mb:.1f} MB")
if final_size_bytes < MIN_SIZE_BYTES:
    print(f"[WARN] Output is under the 1.0GB floor -- consider raising TARGET_SIZE_GB or DURATION.")

# ---------------------------------------------------------------------------
# Thumbnail -- pull a frame from partway through so the release is
# identifiable at a glance instead of just filename+timestamp.
# ---------------------------------------------------------------------------
try:
    actual_duration = probe_duration(output_path)
    grab_at = max(5, min(actual_duration * 0.15, actual_duration - 5))
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(grab_at), "-i", str(output_path),
        "-frames:v", "1", "-q:v", "2", str(thumb_path),
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(f"Thumbnail: {thumb_path}")
except Exception as e:
    print(f"[WARN] thumbnail generation failed: {e}")
    thumb_path = None

print(f"Manifest updated at: {MANIFEST_PATH.resolve()}")
print("\nReminder before publishing:")
print(" 1. In YouTube Studio upload flow, toggle 'Altered or synthetic content' if applicable.")
print(" 2. Write a real, specific title/description/thumbnail for this upload -- no template text.")
print(" 3. Double check every song/image/overlay row in asset_manifest.csv has a real license source noted.")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"thumb_path={thumb_path if thumb_path else ''}\n")
        f.write(f"media_name={media_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
