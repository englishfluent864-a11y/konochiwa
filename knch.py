import os, random, subprocess, threading, time
from pathlib import Path
import gdown

TMP             = Path("/tmp/redsky")
IMAGES_FOLDER   = "1bbYxw2pNbVS05liS0pObjxevuJ-BdXck"
SONGS_FOLDER    = "1DILwSnl-m4yY2w5J29hIlv19DnzNzVm_"
SUB_FOLDER      = "1mpk5rcZRtVcjrKwrsrSIyfu6TzDWZiTl"
DURATION        = random.randint(3600, 7200)   # 1-2 hours max
MIN_SIZE_BYTES  = int(1.0 * 1024 * 1024 * 1024)
MAX_SIZE_BYTES  = int(1.95 * 1024 * 1024 * 1024)
TARGET_SIZE_BYTES = int(1.5 * 1024 * 1024 * 1024)  # aim for the middle of the band
FADE_SEC        = 0.4

IMAGE_EXT = ('.png', '.jpg', '.jpeg')
VIDEO_EXT = ('.mp4', '.mov', '.mkv', '.webm', '.avi')

TARGET_MEDIA_NAME = os.environ.get("TARGET_MEDIA_NAME") or os.environ.get("TARGET_IMAGE_NAME")
if not TARGET_MEDIA_NAME:
    raise SystemExit("TARGET_MEDIA_NAME env var not set.")

TMP.mkdir(exist_ok=True)
(TMP / "images").mkdir(exist_ok=True)
(TMP / "songs").mkdir(exist_ok=True)
(TMP / "sub").mkdir(exist_ok=True)

def download_with_timeout(fn, timeout_sec=1800, label="download"):
    result = [None]; error = [None]
    def worker():
        try: result[0] = fn()
        except Exception as e: error[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start(); t.join(timeout_sec)
    if t.is_alive(): raise TimeoutError(f"{label} timed out after {timeout_sec}s")
    if error[0]: raise error[0]
    return result[0]

stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free space: {free_gb:.1f} GB")
if free_gb < 4.0:
    raise SystemExit(f"[DISK] Not enough free space ({free_gb:.1f} GB).")

print("Downloading media (images/videos)...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=IMAGES_FOLDER, output=str(TMP / "images"), quiet=False),
            timeout_sec=900, label="media"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"media download failed: {e}")
        time.sleep(30)

print("Downloading songs...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=SONGS_FOLDER, output=str(TMP / "songs"), quiet=False),
            timeout_sec=900, label="songs"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"songs download failed: {e}")
        time.sleep(30)

print("Downloading sub overlay...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=SUB_FOLDER, output=str(TMP / "sub"), quiet=False),
            timeout_sec=600, label="sub"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"sub download failed: {e}")
        time.sleep(30)

matches = list((TMP / "images").rglob(TARGET_MEDIA_NAME))
if not matches:
    raise SystemExit(f"Target media {TARGET_MEDIA_NAME} not found.")
media_path = matches[0]
is_video = media_path.suffix.lower() in VIDEO_EXT
is_image = media_path.suffix.lower() in IMAGE_EXT
if not (is_video or is_image):
    raise SystemExit(f"Unsupported media type: {media_path.suffix}")

subs = (
    list((TMP / "sub").glob("*.mp4")) +
    list((TMP / "sub").glob("*.mov")) +
    list((TMP / "sub").glob("*.webm"))
)
if not subs:
    raise SystemExit("No sub overlay video found.")
sub_path = subs[0]

print("Building boomerang (forward + reverse) sub clip...")
sub_reversed_path  = TMP / "sub_reversed.mp4"
sub_boomerang_path = TMP / "sub_boomerang.mp4"
concat_sub_list     = TMP / "concat_sub.txt"

subprocess.run(
    ["ffmpeg", "-y", "-i", str(sub_path), "-vf", "reverse", "-an", str(sub_reversed_path)],
    check=True
)
with open(concat_sub_list, "w") as f:
    f.write(f"file '{sub_path}'\n")
    f.write(f"file '{sub_reversed_path}'\n")
subprocess.run(
    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_sub_list),
     "-c", "copy", str(sub_boomerang_path)],
    check=True
)
sub_path = sub_boomerang_path

output_path = TMP / f"OUT_{media_path.stem}.mp4"
print(f"\n>>> MEDIA    : {media_path.name} ({'video' if is_video else 'image'})")
print(f">>> SUB      : {sub_path.name} (boomerang)")
print(f">>> DURATION : {DURATION}s ({DURATION//60}m {DURATION%60}s)\n")

stat = os.statvfs(str(TMP))
free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
print(f"[DISK] Free after downloads: {free_gb:.1f} GB")
if free_gb < 2.0:
    raise SystemExit(f"[DISK] Not enough space ({free_gb:.1f} GB).")

songs = list((TMP / "songs").glob("*.mp3"))
if not songs:
    raise SystemExit("No songs found.")
random.shuffle(songs)
print("Song order:")
for i, s in enumerate(songs):
    print(f"  {i+1}. {s.name}")

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
# Bitrate math: solve for a video bitrate that lands the whole file near
# TARGET_SIZE_BYTES for this DURATION, given a fixed audio bitrate.
# total_bits = (video_kbps + audio_kbps) * 1000 * DURATION
# ---------------------------------------------------------------------------
AUDIO_BITRATE_K = 128
target_bits = TARGET_SIZE_BYTES * 8
target_total_kbps = target_bits / 1000 / DURATION
video_bitrate_k = max(300, int(target_total_kbps - AUDIO_BITRATE_K))
print(f"[BITRATE] video={video_bitrate_k}k audio={AUDIO_BITRATE_K}k target_total={target_total_kbps:.0f}k")

# Sub overlay: every 6-14 min randomized, 3-4 sec, alternating left/right,
# with a randomized margin range each time (not a fixed pixel spot) so
# placement isn't identical on every appearance.
intervals_left  = []
intervals_right = []
t = random.randint(360, 840)
while t < DURATION - 10:
    show_dur = random.randint(3, 4)
    end_t = t + show_dur
    margin_x = random.randint(20, 60)
    margin_y = random.randint(20, 60)
    entry = (t, end_t, margin_x, margin_y)
    if random.random() < 0.5:
        intervals_left.append(entry)
    else:
        intervals_right.append(entry)
    t += random.randint(360, 840)

def build_fade_chain(intervals, fade_dur=FADE_SEC):
    parts = ["fade=t=out:st=0:d=0.01:alpha=1"]
    for (s, e, _, _) in sorted(intervals, key=lambda x: x[0]):
        dur = e - s
        fd = min(fade_dur, dur / 2)
        parts.append(f"fade=t=in:st={s}:d={fd}:alpha=1")
        parts.append(f"fade=t=out:st={e - fd}:d={fd}:alpha=1")
    return ",".join(parts)

def build_overlay_pos_expr(intervals, side):
    """
    Builds an x/y overlay expression that switches margin per interval using
    nested if()/between() so each appearance uses its own randomized margin,
    instead of one fixed offset for the whole video.
    """
    if not intervals:
        return ("0", "0")
    x_expr, y_expr = "0", "0"
    for (s, e, mx, my) in sorted(intervals, key=lambda x: x[0]):
        cond = f"between(t,{s},{e})"
        if side == "left":
            x_expr = f"if({cond},{mx},{x_expr})"
        else:
            x_expr = f"if({cond},W-w-{mx},{x_expr})"
        y_expr = f"if({cond},H-h-{my},{y_expr})"
    return (x_expr, y_expr)

fade_chain_left  = build_fade_chain(intervals_left)
fade_chain_right = build_fade_chain(intervals_right)
x_left, y_left   = build_overlay_pos_expr(intervals_left, "left")
x_right, y_right = build_overlay_pos_expr(intervals_right, "right")

print(f"Sub overlay: {len(intervals_left)} left, {len(intervals_right)} right appearances")

filter_complex = (
    f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p[bg];"
    f"[1:v]scale=220:-1,chromakey=0x00ff00:0.3:0.1,format=yuva420p[sub_a];"
    f"[sub_a]split[sl0][sr0];"
    f"[sl0]{fade_chain_left}[sl];"
    f"[sr0]{fade_chain_right}[sr];"
    f"[bg][sl]overlay=x='{x_left}':y='{y_left}'[mid];"
    f"[mid][sr]overlay=x='{x_right}':y='{y_right}'[outv]"
)

# Background media input. -an mutes the original video's own audio track
# (no-op for still images, which have no audio anyway).
if is_video:
    bg_input_args = ["-an", "-stream_loop", "-1", "-i", str(media_path)]
else:
    bg_input_args = ["-loop", "1", "-framerate", "1", "-i", str(media_path)]

cmd = [
    "ffmpeg", "-y",
    *bg_input_args,
    # -an here mutes the sub/boomerang (duplicated) overlay clip's own audio.
    "-an", "-stream_loop", "-1", "-i", str(sub_path),
    "-f", "concat", "-safe", "0", "-i", str(concat_path),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]",
    "-map", "2:a",
    "-c:v", "libx264", "-preset", "medium",
    "-b:v", f"{video_bitrate_k}k",
    "-maxrate", f"{int(video_bitrate_k * 1.2)}k",
    "-bufsize", f"{int(video_bitrate_k * 2)}k",
    "-r", "1", "-g", "2",
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "44100",
    "-movflags", "+faststart",
    str(output_path),
]

print("\nRunning FFmpeg...")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
stopped_by_watcher = False
under_minimum = False

def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(15)
        if output_path.exists():
            size = output_path.stat().st_size
            mb = size / (1024 * 1024)
            gb = size / (1024 * 1024 * 1024)
            print(f"[SIZE] {mb:.1f} MB ({gb:.3f} GB)", flush=True)
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

final_size    = output_path.stat().st_size
final_size_mb = final_size / (1024 * 1024)
final_size_gb = final_size / (1024 * 1024 * 1024)
if final_size < MIN_SIZE_BYTES:
    print(f"[SIZE] ⚠️ Under 1 GB ({final_size_gb:.3f} GB).")
    under_minimum = True

stop_reason = "cap reached" if stopped_by_watcher else "duration reached"
print(f"\nDONE — {output_path}")
print(f"Stop   : {stop_reason}")
print(f"Size   : {final_size_mb:.1f} MB ({final_size_gb:.3f} GB)")
print(f"1-2 GB : {'✅' if MIN_SIZE_BYTES <= final_size <= MAX_SIZE_BYTES else '❌'}")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"media_name={media_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
        f.write(f"under_minimum={str(under_minimum).lower()}\n")
