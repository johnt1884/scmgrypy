import os
import sys
import subprocess
import hashlib
import re
import datetime
import shlex
import sqlite3
import concurrent.futures
import struct
import logging
import json
import threading
from pathlib import Path

# --- DEPENDENCY CHECK ---
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import pylnk3
    HAS_PYLNK3 = True
except ImportError:
    HAS_PYLNK3 = False

def check_dependencies():
    missing = []
    for tool in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.check_call([tool, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            missing.append(tool)
    if missing:
        print(f"CRITICAL ERROR: Missing dependencies: {', '.join(missing)}")
        print("Please install FFmpeg and ensure it is in your PATH.")
        sys.exit(1)

# --- CONFIGURATION ---
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv")
THUMB_WIDTH = 256
THUMB_HEIGHT = 256
DB_FILE = "shortcut_db.txt"
SPECIAL_FOLDERS = {"sc", "landscape", "landscape rotate", "edit", "thumbnails", "edit thumbnails"}
CACHE_DB = "metadata_cache.db"
MAX_WORKERS = min(8, os.cpu_count() or 8)
USE_CACHE = True
STRICT_MODE = False
LOG_FILE = "sc_manager.log"

THUMB_FILTER = f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- METADATA CACHE ---

class MetadataCache:
    def __init__(self, db_path=CACHE_DB):
        self.db_path = os.path.abspath(db_path)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self.db_path,
                timeout=60,
                isolation_level=None, # Autocommit mode for WAL
                check_same_thread=False
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    duration REAL,
                    fps REAL,
                    md5 TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_path_mtime ON metadata(path, mtime)")

    def get(self, path):
        path_str = os.path.abspath(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT duration, fps, md5 FROM metadata WHERE path = ? AND mtime = ?",
            (path_str, mtime)
        )
        return cursor.fetchone()

    def set(self, path, duration=None, fps=None, md5=None):
        path_str = os.path.abspath(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
            
        conn = self._get_conn()
        cursor = conn.execute("SELECT duration, fps, md5 FROM metadata WHERE path = ?", (path_str,))
        existing = cursor.fetchone()
        
        final_dur = duration if duration is not None else (existing[0] if existing else None)
        final_fps = fps if fps is not None else (existing[1] if existing else None)
        final_md5 = md5 if md5 is not None else (existing[2] if existing else None)
        
        conn.execute(
            "INSERT OR REPLACE INTO metadata (path, mtime, duration, fps, md5) VALUES (?, ?, ?, ?, ?)",
            (path_str, mtime, final_dur, final_fps, final_md5)
        )

    def close(self):
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn

    def prune(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT path FROM metadata")
            paths = cursor.fetchall()
            to_delete = []
            for (p_str,) in paths:
                if not os.path.exists(p_str):
                    to_delete.append((p_str,))
            if to_delete:
                conn.executemany("DELETE FROM metadata WHERE path = ?", to_delete)
                logger.info(f"Pruned {len(to_delete)} stale entries.")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

cache = MetadataCache()

# --- HELPERS ---

def verify_jpeg_integrity(path):
    """Verifies JPEG SOI and EOI markers."""
    try:
        if os.path.getsize(path) < 4: return False
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8': return False
            f.seek(-2, 2)
            return f.read(2) == b'\xff\xd9'
    except Exception:
        return False

def get_jpeg_dimensions(path):
    try:
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8': return None
            while True:
                marker = f.read(2)
                if not marker or marker[0] != 0xff: break
                if marker[1] in (0xc0, 0xc1, 0xc2, 0xc3): # SOF markers
                    f.read(3)
                    h, w = struct.unpack('>HH', f.read(4))
                    return {"width": w, "height": h}
                else:
                    raw_len = f.read(2)
                    if not raw_len: break
                    length = struct.unpack('>H', raw_len)[0]
                    if length < 2: break
                    f.seek(length - 2, 1)
    except Exception: pass
    return None

def get_image_dimensions(image_path):
    dims = get_jpeg_dimensions(image_path)
    if dims: return dims
    if HAS_PILLOW:
        try:
            with Image.open(image_path) as img:
                w, h = img.size
                exif = img.getexif()
                if exif and exif.get(0x0112) in [5,6,7,8]: w, h = h, w
                return {"width": w, "height": h}
        except Exception: pass
    return None

def run_command(cmd, timeout=180, retries=1):
    for attempt in range(retries + 1):
        try:
            if isinstance(cmd, list):
                if cmd[0] == "mkdir":
                    Path(cmd[1]).mkdir(parents=True, exist_ok=True)
                elif cmd[0] == "rm":
                    p = Path(cmd[1])
                    if p.is_file(): p.unlink()
                else:
                    res = subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=timeout
                    )
                    if res.returncode != 0:
                        logger.error(f"Command failed (code {res.returncode}): {shlex.join(cmd)}")
                        err_out = res.stderr.decode('utf-8', errors='replace')
                        logger.error(err_out[-2000:])
                        return False
            else:
                res = subprocess.run(
                    cmd,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout
                )
                if res.returncode != 0:
                    logger.error(f"Shell command failed (code {res.returncode}): {cmd}")
                    err_out = res.stderr.decode('utf-8', errors='replace')
                    logger.error(err_out[-2000:])
                    return False
            return True
        except Exception as e:
            if attempt == retries:
                cmd_str = shlex.join(cmd) if isinstance(cmd, list) else str(cmd)
                logger.error(f"Command failed after {retries} retries: {cmd_str}. Error: {e}")
                return False
    return False

def run_parallel_commands(commands, desc="Processing"):
    if not commands: return
    print(f"{desc} using {MAX_WORKERS} workers...")
    seen, unique = set(), []
    for c in commands:
        t = tuple(c) if isinstance(c, list) else c
        if t not in seen:
            seen.add(t)
            unique.append(c)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run_command, cmd): cmd for cmd in unique}
        done, total = 0, len(unique)
        for _ in concurrent.futures.as_completed(futures):
            done += 1
            if done % 5 == 0 or done == total:
                print(f"\rProgress: {done}/{total} ({(done/total)*100:.1f}%)", end="", flush=True)
    print()

def get_video_metadata(video_path, use_cache=None):
    if use_cache is None: use_cache = USE_CACHE
    if use_cache:
        cached = cache.get(video_path)
        if cached and cached[0] is not None: return {"duration": cached[0], "fps": cached[1]}

    metadata = {"duration": 0.0, "fps": 25.0}
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=avg_frame_rate",
            "-of", "json", str(video_path)
        ]
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10)
        data = json.loads(res.decode('utf-8', errors='replace'))
        if 'streams' in data and data['streams']:
            fps_str = data['streams'][0].get('avg_frame_rate', '25/1')
            if "/" in fps_str:
                parts = fps_str.split("/")
                n, d = map(float, parts)
                if d != 0: metadata["fps"] = n / d
            else:
                try: metadata["fps"] = float(fps_str)
                except ValueError: pass
        if 'format' in data:
            try: metadata["duration"] = float(data['format'].get('duration', 0))
            except ValueError: pass

        if use_cache and metadata["duration"] > 0:
            cache.set(video_path, duration=metadata["duration"], fps=metadata["fps"])
    except Exception: pass
    return metadata

def get_md5(path, use_cache=None):
    if use_cache is None: use_cache = USE_CACHE
    if not os.path.exists(path): return None
    if use_cache:
        cached = cache.get(path)
        if cached and cached[2] is not None: return cached[2]
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""): 
                h.update(chunk)
        m = h.hexdigest().upper()
        if use_cache: cache.set(path, md5=m)
        return m
    except (OSError, PermissionError, Exception):
        return None

def get_md5_parallel(paths):
    print(f"Hashing {len(paths)} files...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(lambda p: get_md5(p, USE_CACHE), paths))
    return results

def build_thumbnail_command(video_path, thumb_path, timestamp_str):
    """
    FFmpeg command optimized for speed and reliability:
    - Input-related flags (-noautorotate, -ss, -err_detect, etc.) MUST be BEFORE -i
    - setparams filter used to normalize colorspace for FFmpeg 7+ compatibility
    - yuvj420p for MJPEG compatibility
    """
    # Normalize colorspace metadata to avoid "Invalid color space" errors in FFmpeg 7+
    csp_fix = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    full_filter = f"{csp_fix},{THUMB_FILTER}"
    
    return [
        "ffmpeg", "-y", "-threads", "1", 
        "-noautorotate",
        "-err_detect", "ignore_err", 
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-ss", str(timestamp_str),
        "-i", os.path.abspath(video_path), 
        "-map", "0:v:0", "-an", "-vframes", "1", 
        "-vf", full_filter,
        "-pix_fmt", "yuvj420p", "-map_metadata", "-1",
        os.path.abspath(thumb_path)
    ]

def get_edit_thumbnail_timestamp(duration, fps, index):
    if duration <= 0:
        duration = 10.0

    if fps <= 0:
        fps = 25.0

    frame_time = 1.0 / fps

    # 2 frames into video
    start = frame_time * 2

    # 2 frames before end
    end = duration - (frame_time * 2)

    # Safety clamp for very short videos
    if end <= start:
        start = 0.0
        end = max(0.05, duration - frame_time)

    ts = start + ((end - start) * (index / 9.0))

    return f"{max(start, min(ts, end)):.4f}"

def is_valid_thumbnail(video_mtime, thumb_path):
    tp = Path(thumb_path)
    if not tp.exists(): return False
    try:
        stat = tp.stat()
        if stat.st_size < 1024: return False
        if stat.st_mtime < video_mtime: return False
    except OSError: return False

    if STRICT_MODE:
        if not verify_jpeg_integrity(tp): return False
        dims = get_image_dimensions(tp)
        if not dims or dims["width"] > THUMB_WIDTH or dims["height"] > THUMB_HEIGHT:
            return False
    return True

def get_project_folders():
    try:
        entries = os.listdir(".")
    except OSError:
        return []
    return [Path(d).resolve() for d in entries if os.path.isdir(d) and d.lower() not in SPECIAL_FOLDERS]

def get_video_files(project_folder):
    vids = []
    for f in project_folder.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS: vids.append(f)
    for s in ["Landscape", "Landscape Rotate", "Edit"]:
        sp = project_folder / s
        if sp.is_dir():
            for f in sp.iterdir():
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS: vids.append(f)
    
    results = []
    for v in vids:
        try:
            stat = v.stat()
            results.append({
                'path': v, 
                'abspath': os.path.abspath(v),
                'mtime': stat.st_mtime
            })
        except OSError: pass
    
    results.sort(key=lambda x: x['abspath'])
    unique = []
    seen = set()
    for r in results:
        if r['abspath'] not in seen:
            seen.add(r['abspath'])
            unique.append(r)
    return unique

def get_lnk_target(lnk_path):
    if not HAS_PYLNK3: return None
    try:
        lnk = pylnk3.parse(str(lnk_path))
        return lnk.path
    except Exception: return None

# --- SC UTILS ---

def update_sc_date():
    print("\nUpdating scdate.txt files...")
    root = os.path.abspath(".")
    root_sc = Path(root) / 'sc'
    cached_dates = []
    if root_sc.is_dir():
        for lnk in root_sc.glob("*.lnk"):
            t = get_lnk_target(lnk)
            if t: cached_dates.append({"target": os.path.abspath(t), "date": datetime.datetime.fromtimestamp(lnk.stat().st_mtime, tz=datetime.timezone.utc)})

    target_dirs = {root}
    for sc_dir in Path(root).rglob("sc"):
        if sc_dir.is_dir(): target_dirs.add(os.path.abspath(sc_dir.parent))
    for d in Path(root).iterdir():
        if d.is_dir() and d.name.lower() not in SPECIAL_FOLDERS: target_dirs.add(os.path.abspath(d))

    for ds in target_dirs:
        d = Path(ds)
        out = d / 'scdate.txt'
        newest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        p_sc = d / 'sc'
        if p_sc.is_dir():
            links = list(p_sc.glob("*.lnk"))
            if links:
                nl = max(links, key=lambda x: x.stat().st_mtime)
                newest = datetime.datetime.fromtimestamp(nl.stat().st_mtime, tz=datetime.timezone.utc)
        
        if ds != root:
            for c in cached_dates:
                if c["target"].startswith(ds + os.sep) or c["target"] == ds:
                    if c["date"] > newest: newest = c["date"]

        if newest > datetime.datetime.min.replace(tzinfo=datetime.timezone.utc):
            write = True
            if out.exists():
                try:
                    cur = out.read_text().strip()
                    if cur.startswith('dummy:'): cur = cur[6:].strip()
                    if newest <= datetime.datetime.fromisoformat(cur.replace('Z', '+00:00')): write = False
                except Exception: pass
            if write: out.write_text(newest.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z', encoding='utf-8')

def update_sc_data():
    print("\nUpdating scdata.txt files...")
    root = os.path.abspath(".")
    for sc_dir in Path(root).rglob("sc"):
        if sc_dir.is_dir():
            links = sorted([lnk.name for lnk in sc_dir.glob("*.lnk")])
            if links: (sc_dir.parent / 'scdata.txt').write_text("\n".join(links), encoding='utf-8')
    root_sc = Path(root) / 'sc'
    if root_sc.is_dir():
        out = Path(root) / 'rootdata.txt'
        groups = {}
        for lnk in root_sc.glob("*.lnk"):
            t = get_lnk_target(lnk)
            if t:
                tp = os.path.abspath(t)
                fn, gn = lnk.name, Path(tp).parent.name
                tag = '[BOTH]' if (Path(tp).parent / 'sc' / fn).exists() else '[ROOT]'
                if gn not in groups: groups[gn] = []
                groups[gn].append(f"{fn} {tag}")
        if groups:
            with out.open('w', encoding='utf-8') as f:
                for g in sorted(groups.keys()):
                    f.write(f'"{g}"\n')
                    for i in sorted(groups[g]): f.write(f"{i}\n")
                    f.write("\n")
        elif out.exists(): out.unlink()

def generate_sc_new():
    print("\nGenerating scnew.txt...")
    root = os.path.abspath(".")
    root_links = list((Path(root) / 'sc').glob("*.lnk")) if (Path(root) / 'sc').is_dir() else []
    for proj in Path(root).iterdir():
        if proj.is_dir() and proj.name != 'sc':
            proj_sc = proj / 'sc'
            out = proj / 'scnew.txt'
            plinks = list(proj_sc.glob("*.lnk")) if proj_sc.is_dir() else []
            if not plinks:
                if out.exists(): out.unlink()
                continue
            matching = []
            for rl in root_links:
                t = get_lnk_target(rl)
                if t and os.path.abspath(t).startswith(os.path.abspath(proj)): matching.append(rl)
            new = []
            if not matching: new = sorted(plinks, key=lambda x: x.stat().st_mtime)
            else:
                cutoff = max(l.stat().st_mtime for l in matching)
                new = sorted([l for l in plinks if l.stat().st_mtime > cutoff], key=lambda x: x.stat().st_mtime)
            if new: out.write_text("\n".join([l.name for l in new]), encoding='utf-8')
            elif out.exists(): out.unlink()

def update_selections():
    print("\nUpdating selections.txt files...")
    for proj in get_project_folders():
        print(f"Processing: {proj.name}")
        c = []
        for s in ["sc", "Landscape", "Landscape Rotate", "Edit"]:
            c.append(f"# {s}")
            sp = proj / s
            if sp.is_dir(): c.extend(sorted([f.name for f in sp.iterdir() if f.is_file()]))
            c.append("")
        (proj / 'selections.txt').write_text("\n".join(c), encoding='utf-8')

# --- THUMBNAILS ---

def check_thumbnails():
    all_proj = get_project_folders()
    fix_commands = []
    print("\nChecking thumbnails...")
    for folder in all_proj:
        videos = get_video_files(folder)
        video_basenames = {v['path'].stem for v in videos}
        rt_dir, et_dir = folder / "Thumbnails", folder / "Edit Thumbnails"
        issues = {"MissingRegular": [], "MissingEdit": [], "Obsolete": []}
        
        for v in videos:
            vp = v['path']
            vm = v['mtime']
            rp = rt_dir / f"{vp.stem}.jpg"
            if not is_valid_thumbnail(vm, rp): issues["MissingRegular"].append(vp)
            
            valid_indices = set()

            if et_dir.exists():
                for thumb in et_dir.glob(f"{vp.stem}_*.jpg"):
                    m = re.match(
                        rf'^{re.escape(vp.stem)}_(\d+)\.jpg$',
                        thumb.name
                    )

                    if not m:
                        continue

                    idx = int(m.group(1))

                    if 1 <= idx <= 10 and is_valid_thumbnail(vm, thumb):
                        valid_indices.add(idx)

            missing_edit_indices = [
                i for i in range(1, 11)
                if i not in valid_indices
            ]
            
            if missing_edit_indices: issues["MissingEdit"].append((vp, missing_edit_indices))

        if rt_dir.is_dir():
            for t in rt_dir.glob("*.jpg"):
                if t.stem not in video_basenames: issues["Obsolete"].append(t)
        if et_dir.is_dir():
            for t in et_dir.glob("*.jpg"):
                if not find_video_basename_for_edit_thumbnail(t.name, video_basenames): issues["Obsolete"].append(t)

        if issues["MissingRegular"]:
            rt_dir.mkdir(parents=True, exist_ok=True)
            for vp in issues["MissingRegular"]:
                m = get_video_metadata(vp)
                ts = "00:00:02.000" if m['duration'] > 4.0 else f"{m['duration'] * 0.5:.4f}"
                fix_commands.append(build_thumbnail_command(vp, rt_dir / f"{vp.stem}.jpg", ts))
        if issues["MissingEdit"]:
            et_dir.mkdir(parents=True, exist_ok=True)
            for vp, indices in issues["MissingEdit"]:
                m = get_video_metadata(vp)
                for i in indices:
                    ts = get_edit_thumbnail_timestamp(m["duration"], m["fps"], i - 1)
                    fix_commands.append(build_thumbnail_command(vp, et_dir / f"{vp.stem}_{i}.jpg", ts))
        for t in issues["Obsolete"]: fix_commands.append(["rm", str(t)])
        
        c = len(issues["MissingRegular"]) + len(issues["MissingEdit"]) + len(issues["Obsolete"])
        status = "OK"
        if c > 0:
            parts = []
            if issues["MissingRegular"]: parts.append(f"{len(issues['MissingRegular'])} missing regular")
            if issues["MissingEdit"]: parts.append(f"{len(issues['MissingEdit'])} missing edit")
            if issues["Obsolete"]: parts.append(f"{len(issues['Obsolete'])} obsolete")
            status = f"Issues: {', '.join(parts)}"
        print(f"{folder.name}: {status}")

    handle_fix_prompt(fix_commands)

def update_new_thumbnails():
    print("\nFast update for new videos...")
    all_proj = get_project_folders()
    fix_commands = []
    for folder in all_proj:
        sd = folder / "scdate.txt"
        cutoff = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc).timestamp()
        if sd.exists():
            try:
                cur = sd.read_text().strip()
                if cur.startswith('dummy:'): cur = cur[6:].strip()
                cutoff = datetime.datetime.fromisoformat(cur.replace('Z', '+00:00')).timestamp()
            except Exception: pass
        
        videos = get_video_files(folder)
        new = [v for v in videos if v['mtime'] > cutoff]
        if new:
            print(f"{folder.name}: {len(new)} new")
            rt_dir, et_dir = folder / "Thumbnails", folder / "Edit Thumbnails"
            
            # Prep folders once
            need_rt, need_et = False, False
            project_cmds = []
            
            for v in new:
                vp = v['path']
                vm = v['mtime']
                rp = rt_dir / f"{vp.stem}.jpg"
                if not is_valid_thumbnail(vm, rp):
                    need_rt = True
                    m = get_video_metadata(vp)
                    ts = "00:00:02.000" if m['duration'] > 4.0 else f"{m['duration'] * 0.5:.4f}"
                    project_cmds.append(build_thumbnail_command(vp, rp, ts))
                
                missing_indices = []
                for i in range(1, 11):
                    tp = et_dir / f"{vp.stem}_{i}.jpg"
                    if not is_valid_thumbnail(vm, tp): missing_indices.append(i)
                if missing_indices:
                    need_et = True
                    m = get_video_metadata(vp)
                    for i in missing_indices:
                        ts = get_edit_thumbnail_timestamp(m["duration"], m["fps"], i - 1)
                        project_cmds.append(build_thumbnail_command(vp, et_dir / f"{vp.stem}_{i}.jpg", ts))
            
            if need_rt: rt_dir.mkdir(parents=True, exist_ok=True)
            if need_et: et_dir.mkdir(parents=True, exist_ok=True)
            fix_commands.extend(project_cmds)
            
    handle_fix_prompt(fix_commands)

def handle_fix_prompt(fix_commands):
    if not fix_commands:
        print("\nAll good!")
        return
    seen, unique = set(), []
    for c in fix_commands:
        t = tuple(c) if isinstance(c, list) else c
        if t not in seen: seen.add(t); unique.append(c)
    print(f"\n{len(unique)} unique actions.")
    print("1. Fix now (parallel)\n2. Generate script\n3. Skip")
    choice = input("Select: ")
    if choice == '1': run_parallel_commands(unique, "Fixing")
    elif choice == '2': generate_fix_script(unique)

def generate_fix_script(fix_commands):
    sn = "fix_thumbnails.bat" if os.name == 'nt' else "fix_thumbnails.sh"
    with open(sn, "w", encoding="utf-8") as f:
        if os.name == 'nt':
            f.write("@echo off\necho Starting thumbnail fix process...\n")
            for c in fix_commands:
                if c[0] == "mkdir": f.write(f'if not exist "{c[1]}" mkdir "{c[1]}"\n')
                elif c[0] == "rm": f.write(f'if exist "{c[1]}" del /q "{c[1]}"\n')
                else: f.write(" ".join(f'"{a}"' for a in c) + "\n")
            f.write("echo Thumbnail fix process complete.\npause\n")
        else:
            f.write("#!/bin/bash\necho \"Starting thumbnail fix process...\"\n")
            for c in fix_commands:
                if c[0] == "mkdir": f.write(f"mkdir -p {shlex.quote(c[1])}\n")
                elif c[0] == "rm": f.write(f"rm -f {shlex.quote(c[1])}\n")
                else: f.write(" ".join(shlex.quote(a) for a in c) + "\n")
            f.write("echo \"Thumbnail fix process complete.\"\n")
    if os.name != 'nt': os.chmod(sn, 0o755)
    print(f"Generated {sn}")

def find_video_basename_for_edit_thumbnail(n, bl):
    best = None
    for b in bl:
        if n.startswith(f"{b}_"):
            if best is None or len(b) > len(best): best = b
    # Only match indices 1-10
    if best and re.match(r'^_(10|[1-9])\.jpg$', n[len(best):]): return best
    return None

# --- SHORTCUTS ---

def update_shortcut_database():
    print("\nUpdating Shortcut DB...")
    db = []
    p = Path(DB_FILE)
    if p.exists():
        try:
            for e in p.read_text(encoding='utf-8').split("---\n"):
                if not e.strip(): continue
                it = {}
                for l in e.strip().splitlines():
                    if l.startswith("Folder path: "): it["FolderPath"] = l[13:]
                    elif l.startswith("Shortcut: "): it["ShortcutName"] = l[10:]
                    elif l.startswith("Shortcut Video Path: "): it["VideoPath"] = l[21:]
                    elif l.startswith("Shortcut md5: "): it["MD5"] = l[14:]
                if "FolderPath" in it: db.append(it)
        except Exception: pass
    
    links = list(Path(".").rglob("*.lnk"))
    targets = []
    for lnk in links:
        t = get_lnk_target(lnk)
        if t and Path(t).suffix.lower() in VIDEO_EXTENSIONS:
            targets.append((lnk, os.path.abspath(t)))
    
    paths_to_hash = [tp for _, tp in targets if os.path.exists(tp)]
    md5_results = get_md5_parallel(paths_to_hash)
    md5_map = dict(zip(paths_to_hash, md5_results))

    new_db = []
    for lnk, tp in targets:
        if not os.path.exists(tp): continue
        m = md5_map.get(tp)
        new_db.append({"FolderPath": os.path.abspath(lnk.parent), "ShortcutName": lnk.name, "VideoPath": tp, "MD5": m})

    try:
        with p.open('w', encoding='utf-8') as f:
            for e in new_db:
                f.write(f"Folder path: {e['FolderPath']}\nShortcut: {e['ShortcutName']}\nShortcut Video Path: {e['VideoPath']}\nShortcut md5: {e['MD5']}\n---\n")
    except Exception: pass
    print(f"Total entries: {len(new_db)}")

def scan_broken_shortcuts():
    print("\nScanning broken shortcuts...")
    p = Path(DB_FILE)
    if not p.exists(): return
    db = []
    try:
        for e in p.read_text(encoding='utf-8').split("---\n"):
            if not e.strip(): continue
            it = {}
            for l in e.strip().splitlines():
                if l.startswith("Folder path: "): it["FolderPath"] = l[13:]
                elif l.startswith("Shortcut: "): it["ShortcutName"] = l[10:]
                elif l.startswith("Shortcut Video Path: "): it["VideoPath"] = l[21:]
                elif l.startswith("Shortcut md5: "): it["MD5"] = l[14:]
            if "FolderPath" in it: db.append(it)
    except Exception: return

    for e in db:
        lp = Path(e["FolderPath"]) / e["ShortcutName"]
        if not lp.exists(): continue
        t = get_lnk_target(lp)
        if not t or not Path(t).exists():
            print(f"\nBroken: {e['ShortcutName']} in {e['FolderPath']}")
            od = Path(e["VideoPath"]).parent
            if od.is_dir():
                found = None
                for f in od.iterdir():
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and get_md5(f) == e["MD5"]:
                        found = f; break
                if found:
                    print(f"Match: {found.name}")
                    if input("Repair? (y/n): ").lower() == 'y':
                        try:
                            pylnk3.create(str(lp), os.path.abspath(found))
                            nt = get_lnk_target(lp)
                            if nt and os.path.abspath(nt) == os.path.abspath(found): print("Repaired.")
                            else: print("Repair failed verification.")
                        except Exception: pass
            else: print(f"Original dir gone: {od}")

def shortcut_manager_menu():
    if not HAS_PYLNK3: print("pylnk3 missing."); return
    while True:
        print("\n--- Shortcut Manager ---\n1. Update DB\n2. Scan Broken\n3. Back")
        c = input("Select: ")
        if c == "1": update_shortcut_database()
        elif c == "2": scan_broken_shortcuts()
        elif c == "3": break

# --- UI ---

def show_menu():
    print("======================================")
    print("   SC Utilities (Python)")
    print("======================================")
    print(f"   [Workers: {MAX_WORKERS} | Cache: {'ON' if USE_CACHE else 'OFF'} | Mode: {'STRICT' if STRICT_MODE else 'FAST'}]")
    print("\n   UPDATES\n   1. scdate\n   2. scdata\n   3. scnew\n   4. selections\n   5. ALL updates (1-4)")
    print("\n   TOOLS\n   6. Check Thumbs (All)\n   7. Update New Thumbs (Fast)\n   8. Shortcuts")
    print("\n   SETTINGS\n   W. Workers\n   C. Toggle Cache\n   S. Toggle Strict Mode\n   P. Prune Cache")
    print("\n   9. Exit\n")

def main():
    check_dependencies()
    global MAX_WORKERS, USE_CACHE, STRICT_MODE
    while True:
        show_menu()
        choices = input("Choice(s): ")
        if not choices: continue
        for c in re.split(r'[\s,;]+', choices.strip()):
            cu = c.upper()
            if cu == "W":
                try: 
                    val = int(input(f"Workers ({MAX_WORKERS}): "))
                    MAX_WORKERS = max(1, min(32, val))
                except Exception: pass
            elif cu == "C": USE_CACHE = not USE_CACHE
            elif cu == "S": STRICT_MODE = not STRICT_MODE
            elif cu == "P": cache.prune()
            elif c == "1": update_sc_date()
            elif c == "2": update_sc_data()
            elif c == "3": generate_sc_new()
            elif c == "4": update_selections()
            elif c == "5": update_sc_date(); update_sc_data(); generate_sc_new(); update_selections()
            elif c == "6": check_thumbnails()
            elif c == "7": update_new_thumbnails()
            elif c == "8": shortcut_manager_menu()
            elif c == "9": 
                cache.close()
                sys.exit(0)
        input("\nPress Enter...")

if __name__ == "__main__":
    main()
