import os, random, subprocess, threading, time
from pathlib import Path
import gdown

TMP             = Path("/tmp/redsky")
IMAGES_FOLDER   = "1bbYxw2pNbVS05liS0pObjxevuJ-BdXck"
SONGS_FOLDER    = "1DILwSnl-m4yY2w5J29hIlv19DnzNzVm_"
SUB_FOLDER      = "1mpk5rcZRtVcjrKwrsrSIyfu6TzDWZiTl"
DURATION        = random.randint(18000, 28800)
AUDIO_BITRATE_K = 128
MIN_SIZE_BYTES  = int(1.0 * 1024 * 1024 * 1024)
MAX_SIZE_BYTES  = int(1.95 * 1024 * 1024 * 1024)

TARGET_IMAGE_NAME = os.environ.get("TARGET_IMAGE_NAME")
if not TARGET_IMAGE_NAME:
    raise SystemExit("TARGET_IMAGE_NAME env var not set.")

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

print("Downloading images...")
for attempt in range(3):
    try:
        download_with_timeout(
            lambda: gdown.download_folder(id=IMAGES_FOLDER, output=str(TMP / "images"), quiet=False),
            timeout_sec=900, label="images"
        )
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}")
        if attempt == 2:
            raise SystemExit(f"images download failed: {e}")
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

matches = list((TMP / "images").rglob(TARGET_IMAGE_NAME))
if not matches:
    raise SystemExit(f"Target image {TARGET_IMAGE_NAME} not found.")
image_path = matches[0]

subs = (
    list((TMP / "sub").glob("*.mp4")) +
    list((TMP / "sub").glob("*.mov")) +
    list((TMP / "sub").glob("*.webm"))
)
if not subs:
    raise SystemExit("No sub overlay video found.")
sub_path = subs[0]
output_path = TMP / f"OUT_{image_path.stem}.mp4"

print(f"\n>>> IMAGE    : {image_path.name}")
print(f">>> SUB      : {sub_path.name}")
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

concat_path = TMP / f"concat_{image_path.stem}.txt"
estimated_song_len = 200
repeats = max(1, (DURATION // (len(songs) * estimated_song_len)) + 2)
with open(concat_path, "w") as f:
    for _ in range(repeats):
        batch = songs[:]
        random.shuffle(batch)
        for s in batch:
            f.write(f"file '{s}'\n")

# Sub overlay: every 6-14 min randomized, 3-4 sec, random bottom-left or bottom-right
intervals_left  = []
intervals_right = []
t = random.randint(360, 840)
while t < DURATION - 10:
    show_dur = random.randint(3, 4)
    end_t = t + show_dur
    if random.random() < 0.5:
        intervals_left.append((t, end_t))
    else:
        intervals_right.append((t, end_t))
    t += random.randint(360, 840)

def make_enable(intervals):
    if not intervals: return "0"
    return "+".join([f"between(t,{s},{e})" for s, e in intervals])

enable_left  = make_enable(intervals_left)
enable_right = make_enable(intervals_right)
print(f"Sub overlay: {len(intervals_left)} left, {len(intervals_right)} right appearances")

filter_complex = (
    f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p[bg];"
    f"[1:v]scale=220:-1,chromakey=0x00ff00:0.3:0.1[sub_clean];"
    f"[sub_clean]split[sl][sr];"
    f"[bg][sl]overlay=30:H-h-30:enable='{enable_left}'[mid];"
    f"[mid][sr]overlay=W-w-30:H-h-30:enable='{enable_right}'[outv]"
)

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
        f.write(f"image_name={image_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
        f.write(f"under_minimum={str(under_minimum).lower()}\n")
