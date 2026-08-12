"""
Ambient/lofi channel render script -- fully self-contained.

Everything (folder IDs, channel tag, size/zoom/sub settings) is hardcoded
below. The workflow that runs this has zero inputs -- click "Run workflow"
and it goes: download -> auto-pick media -> render -> thumbnail -> release.

Pipeline:
  - Background image with a continuous oscillating zoom: 7s zoom-in,
    7s zoom-out, forever.
  - Fixed-position brand logo overlay (optional).
  - Visible "AI-generated content" disclosure label (bottom-left).
    Pair with YouTube Studio's "Altered or synthetic content" toggle at
    upload time -- that's the metadata half, this script can't set it.
  - Green-screen "Subscribe" clip, chroma-keyed, fixed bottom-right,
    appearing on randomized 3-7 minute gaps.
  - Thumbnail frame pulled from the finished render for the release.
  - asset_manifest.csv logs every asset used per render -- confirm/own
    the rights to everything pointed at here; the manifest is a paper
    trail, not a license.
"""

import csv
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gdown

# ===========================================================================
# HARDCODED CONFIG -- edit these directly, no workflow inputs needed
# ===========================================================================
IMAGES_FOLDER = "1bbYxw2pNbVS05liS0pObjxevuJ-BdXck"   # Drive folder: images1
SONGS_FOLDER = "1DILwSnl-m4yY2w5J29hIlv19DnzNzVm_"     # Drive folder: songs
SUB_FILE_ID = "1PsqVZyJZbyy8oh5LQoKkgms7bGkkJquY"      # Drive file: subscribe green-screen clip

CHANNEL_TAG = "render"
LOGO_PATH = ""  # e.g. "assets/logo.png" relative to repo root, blank = no logo overlay

TARGET_MEDIA_NAME = ""      # blank = auto-pick a random file from IMAGES_FOLDER every run
DURATION_MIN_SEC = 3600     # 1 hour
DURATION_MAX_SEC = 10800    # 3 hours

FPS = 24
ZOOM_MIN = 1.0
ZOOM_MAX = 1.15
ZOOM_PERIOD_SECONDS = 14.0  # 7s in / 7s out, forever

AUDIO_BITRATE_K = 192
TARGET_SIZE_GB = 1.5        # aim point
MIN_SIZE_GB = 1.0           # floor (warn only, not enforced)
MAX_SIZE_GB = 1.9           # hard cap, encode is stopped if hit

SUB_GAP_MIN_SEC = 180       # 3 min
SUB_GAP_MAX_SEC = 420       # 7 min
SUB_MAX_SHOW_SECONDS = 6.0
SUB_CHROMA_COLOR = "0x00FF00"
SUB_CHROMA_SIMILARITY = "0.18"
SUB_CHROMA_BLEND = "0.06"
SUB_SCALE_WIDTH = "340"

MANIFEST_PATH = Path("asset_manifest.csv")
TMP = Path("/tmp/render")
IMAGE_EXT = (".png", ".jpg", ".jpeg")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi")

TARGET_SIZE_BYTES = int(TARGET_SIZE_GB * 1024 * 1024 * 1024)
MIN_SIZE_BYTES = int(MIN_SIZE_GB * 1024 * 1024 * 1024)
MAX_SIZE_BYTES = int(MAX_SIZE_GB * 1024 * 1024 * 1024)
DURATION = random.randint(DURATION_MIN_SEC, DURATION_MAX_SEC)

TMP.mkdir(parents=True, exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
import os
stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free space: {free_gb:.1f} GB")
if free_gb < 4.0:
    raise SystemExit(f"[DISK] Not enough free space ({free_gb:.1f} GB).")

# ---------------------------------------------------------------------------
# Download everything -- images folder, songs folder, sub overlay clip
# ---------------------------------------------------------------------------
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
# Pick source media -- explicit name if set above, otherwise auto-pick so
# this runs fully unattended, no prompts, no inputs.
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
# MAX_SIZE_BYTES (1.9GB) by the size watcher, floor-checked at 1.0GB.
# preset=slow buys meaningfully better quality per bit than medium.
# ---------------------------------------------------------------------------
target_bits = TARGET_SIZE_BYTES * 8
target_total_kbps = target_bits / 1000 / DURATION
video_bitrate_k = max(800, int(target_total_kbps - AUDIO_BITRATE_K))
print(f"[BITRATE] video={video_bitrate_k}k audio={AUDIO_BITRATE_K}k")

# ---------------------------------------------------------------------------
# Subscribe overlay schedule -- randomized 3-7 min gaps, fixed position
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
    print(f"[WARN] Output is under the 1.0GB floor -- consider raising TARGET_SIZE_GB or DURATION_MIN_SEC.")

# ---------------------------------------------------------------------------
# Thumbnail -- pull a frame from partway through for the release
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
