#!/usr/bin/env python3
"""
LimeWireify — intentionally wreck audio into early-2000s P2P MP3 artifacts.

Windows/Linux/macOS
Requires: ffmpeg + ffprobe (ffprobe comes with ffmpeg)
Optional: tqdm (for progress bars) -> py -m pip install tqdm

Interactive:
  py limewireify.py

Output naming:
  <name>_lw_<destroy>.mp3    e.g. song_lw_50.mp3

Key behavior change (your request):
- User slider is remapped so 50% ≈ old 80% strength.
- Steeper ramp in the first half:
    0..50  -> 0..80 internal
    50..100 -> 80..100 internal
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
import random
import sys


AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}


# -----------------------------
# Basics
# -----------------------------

def have_ffmpeg() -> bool:
    return (shutil.which("ffmpeg") is not None or shutil.which("ffmpeg.exe") is not None) and \
           (shutil.which("ffprobe") is not None or shutil.which("ffprobe.exe") is not None)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def lerp(a, b, t):
    return a + (b - a) * t


def get_duration_seconds(path: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True
    )
    return float(probe.stdout.strip())


# -----------------------------
# Progress display
# -----------------------------

def run_ffmpeg_with_progress(cmd: list[str], duration_s: float, desc: str):
    """
    Runs ffmpeg with -progress pipe:1 and shows a progress bar if tqdm is installed.
    Keeps output clean by suppressing stderr.
    """
    try:
        from tqdm import tqdm  # type: ignore
        use_bar = True
    except Exception:
        use_bar = False

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    total_ms = max(1, int(duration_s * 1000))
    last = 0

    if use_bar:
        bar = tqdm(total=total_ms, unit="ms", desc=desc, leave=True)
    else:
        bar = None
        print(desc)

    try:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                v = int(line.split("=", 1)[1])
                v = clamp(v, 0, total_ms)
                delta = v - last
                if delta > 0:
                    if use_bar:
                        bar.update(delta)  # type: ignore
                    else:
                        # crude percent update without spamming too much
                        pct = int((v / total_ms) * 100)
                        if pct % 5 == 0 and pct != int((last / total_ms) * 100):
                            print(f"  {pct}%")
                    last = v
            elif line == "progress=end":
                break
    finally:
        if use_bar:
            bar.close()  # type: ignore

    rc = p.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


# -----------------------------
# New: remap user destroy -> internal strength
# -----------------------------

def remap_destroy(user_destroy: int) -> float:
    """
    Your requested curve:
      user 0..50  maps to internal 0..0.80   (steep ramp)
      user 50..100 maps to internal 0.80..1.0 (gentler ramp)
    Returns internal_d in 0..1.
    """
    x = clamp(user_destroy / 100.0, 0.0, 1.0)

    if x <= 0.5:
        # 0..0.5 -> 0..0.8
        internal = (x / 0.5) * 0.8
    else:
        # 0.5..1.0 -> 0.8..1.0
        internal = 0.8 + ((x - 0.5) / 0.5) * 0.2

    return clamp(internal, 0.0, 1.0)


# -----------------------------
# Recommended settings
# -----------------------------

def recommended_destroy_for_file(input_file: Path) -> int:
    """
    With the new curve, 50 is a good "classic" default.
    MP3 sources stack generational loss better, so recommend 50 for MP3, 45 otherwise.
    """
    ext = input_file.suffix.lower()
    return 50 if ext == ".mp3" else 45


def prompt_seed():
    s = input("Seed (Enter for random, or type a number for repeatable runs): ").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


# -----------------------------
# Sound model
# -----------------------------

def pick_settings(user_destroy: int):
    """
    Uses internal_d (remapped curve) to produce “MP3-ish” LimeWire/Kazaa degradation:
    - bitrate + generational loss are the main character
    - gentle lowpass only (avoid weird EQ)
    - slight clipping
    - bitrate wobble per pass
    - occasional mono-fold for “bad rip” flavor
    """
    internal_d = remap_destroy(user_destroy)

    # Bitrate: 160k -> 24k (but the wobble will vary per pass)
    base_bitrate_k = int(round(lerp(160, 24, internal_d)))

    # Generations: 1 -> 11 (internal 0.8+ becomes quite stacked)
    passes = int(round(lerp(1, 11, internal_d)))

    # Sample rate: keep 44.1k until very high internal strength
    sr = 44100 if internal_d < 0.95 else 22050

    # Slight “hot rip” vibe, not smashed
    clip_db = lerp(0.0, 2.4, internal_d)

    # Gentle bandwidth rolloff (classic mp3 haze)
    lowpass_hz = int(round(lerp(19000, 13000, internal_d)))

    # LAME quality: 2 -> 8 (worse at high)
    q = int(round(lerp(2, 8, internal_d)))

    # Wobble: always a little, more with internal strength
    wobble_pct = lerp(0.08, 0.45, internal_d)  # ±8%..±45%

    # Occasional mono fold chance (joint-stereo weirdness / bad rips)
    mono_chance = lerp(0.02, 0.22, internal_d)  # 2%..22%

    # Glitches: off by default, only at very high user values
    glitch_enable = user_destroy >= 90
    glitch_events_per_min = lerp(0.0, 8.0, max(0.0, internal_d - 0.95) / 0.05)
    glitch_ms = int(round(lerp(0, 70, max(0.0, internal_d - 0.95) / 0.05)))

    return {
        "user_destroy": user_destroy,
        "d": internal_d,
        "base_bitrate_k": base_bitrate_k,
        "passes": passes,
        "sr": sr,
        "clip_db": clip_db,
        "lowpass_hz": lowpass_hz,
        "q": q,
        "wobble_pct": wobble_pct,
        "mono_chance": mono_chance,
        "glitch_enable": glitch_enable,
        "glitch_events_per_min": glitch_events_per_min,
        "glitch_ms": glitch_ms,
    }


def build_filter(params: dict, make_mono: bool) -> str:
    filters = []

    # Occasionally fold toward mono (adds that cheap/old rip vibe)
    if make_mono:
        filters.append("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")

    # MP3-ish bandwidth rolloff
    filters.append(f"lowpass=f={params['lowpass_hz']}")

    # Slight “too hot” rip
    if params["clip_db"] > 0.01:
        filters.append(f"volume={params['clip_db']:.2f}dB")
        filters.append("alimiter=limit=0.99:level=disabled")

    return ",".join(filters)


def apply_simple_glitches(input_wav: Path, output_wav: Path, params: dict, seed: int | None = None) -> None:
    """
    Minimal stutter/dropout simulation for 'corrupted download' vibe.
    Only used at high user_destroy (>=90).
    """
    if seed is not None:
        random.seed(seed)

    dur = get_duration_seconds(input_wav)
    events_per_min = params["glitch_events_per_min"]
    glitch_ms = max(20, params["glitch_ms"])

    if events_per_min <= 0 or glitch_ms <= 0:
        shutil.copyfile(input_wav, output_wav)
        return

    events = int(round((dur / 60.0) * events_per_min))
    events = int(clamp(events, 1, 40))

    event_list = []
    for _ in range(events):
        t = random.uniform(0.2, max(0.21, dur - 0.2))
        length = random.uniform(glitch_ms * 0.6, glitch_ms * 1.4) / 1000.0
        kind = "repeat" if random.random() < 0.65 else "drop"
        reps = random.randint(2, 4)
        event_list.append((t, length, kind, reps))

    event_list.sort(key=lambda x: x[0])

    parts = []
    cursor = 0.0
    for (t, length, kind, reps) in event_list:
        if t > cursor:
            parts.append(("audio", cursor, t))
        start = t
        end = min(dur, t + length)
        if end <= start:
            continue

        if kind == "repeat":
            for _ in range(reps):
                parts.append(("audio", start, end))
        else:
            parts.append(("silence", 0.0, end - start))

        cursor = end

    if cursor < dur:
        parts.append(("audio", cursor, dur))

    fc_lines = []
    concat_inputs = []
    a_count = 0
    s_count = 0

    for p in parts:
        if p[0] == "audio":
            st, en = p[1], p[2]
            label = f"a{a_count}"
            fc_lines.append(f"[0:a]atrim=start={st:.5f}:end={en:.5f},asetpts=PTS-STARTPTS[{label}]")
            concat_inputs.append(f"[{label}]")
            a_count += 1
        else:
            dur_s = p[2]
            label = f"s{s_count}"
            fc_lines.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur_s:.5f},asetpts=PTS-STARTPTS[{label}]")
            concat_inputs.append(f"[{label}]")
            s_count += 1

    n = len(concat_inputs)
    filter_complex = ";".join(fc_lines + [f"{''.join(concat_inputs)}concat=n={n}:v=0:a=1[outa]"])

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_wav),
         "-filter_complex", filter_complex,
         "-map", "[outa]",
         "-c:a", "pcm_s16le",
         str(output_wav)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def encode_mp3(input_audio: Path, output_mp3: Path, params: dict, desc: str):
    # Per-pass bitrate wobble (mixed sources)
    wob = params["wobble_pct"]
    base = params["base_bitrate_k"]
    factor = 1.0 + random.uniform(-wob, wob)
    bitrate_k = int(round(clamp(base * factor, 16, 192)))

    # Occasional mono-fold
    make_mono = (random.random() < params["mono_chance"])

    filt = build_filter(params, make_mono)
    dur = get_duration_seconds(input_audio)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_audio),
        "-vn",
        "-af", filt,
        "-ar", str(params["sr"]),
        "-codec:a", "libmp3lame",
        "-b:a", f"{bitrate_k}k",
        "-q:a", str(params["q"]),
        "-progress", "pipe:1",
        "-nostats",
        str(output_mp3)
    ]

    run_ffmpeg_with_progress(
        cmd, dur,
        desc + f" ({bitrate_k}k{' mono' if make_mono else ''})"
    )


# -----------------------------
# UI / interaction
# -----------------------------

def choose_file() -> Path:
    script_dir = Path(__file__).parent
    files = [f for f in script_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS]

    print("\nLooking for audio files in script folder:\n")
    if files:
        for i, f in enumerate(files, start=1):
            print(f"{i}. {f.name}")
        choice = input("\nSelect number, or press Enter to type a full path: ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(files):
                return files[idx]

    while True:
        p = input("\nEnter full file path: ").strip().strip('"')
        fp = Path(p)
        if fp.exists() and fp.is_file():
            return fp
        print("File not found. Try again.")


def prompt_destroy(recommended: int) -> int:
    print("\nDestruction presets (NEW curve):")
    print("  30 = mild old MP3")
    print("  50 = classic LimeWire/Kazaa rip (this is the sweet spot now)")
    print("  70 = crunchy / obviously re-encoded")
    print("  90 = wrecked + optional glitches")
    print("  100 = maximum destruction")
    print(f"\nRecommended for this file: {recommended}  (type R to use it)")
    while True:
        s = input("Enter destruction (0-100) or 'R': ").strip()
        if s.lower() == "r":
            return int(recommended)
        try:
            v = int(s)
            if 0 <= v <= 100:
                return v
        except ValueError:
            pass
        print("Please enter 0–100 or R.")


# -----------------------------
# Main
# -----------------------------

def main():
    if not have_ffmpeg():
        print("FFmpeg/FFprobe not found in PATH.")
        print("Try: ffmpeg -version  (in this same terminal)")
        return

    print("=== LimeWireify Interactive Mode ===")

    input_file = choose_file()
    recommended = recommended_destroy_for_file(input_file)
    user_destroy = prompt_destroy(recommended)

    seed = prompt_seed()
    if seed is not None:
        random.seed(seed)

    params = pick_settings(user_destroy)

    out = input_file.with_suffix("")
    output_file = out.with_name(f"{out.name}_lw_{user_destroy}.mp3")

    # Show both user and internal strength so the new curve is transparent
    internal_pct = int(round(params["d"] * 100))

    print("\n--- Settings chosen ---")
    print(f"Input              : {input_file}")
    print(f"Output             : {output_file.name}")
    print(f"Destroy (user)      : {user_destroy}")
    print(f"Destroy (internal)  : {internal_pct}   (50 -> ~80 now)")
    print(f"Bitrate (base)      : {params['base_bitrate_k']} kbps (wobbles per pass)")
    print(f"Generations         : {params['passes']}")
    print(f"Sample rate         : {params['sr']} Hz")
    print(f"Lowpass             : {params['lowpass_hz']} Hz")
    print(f"Clip boost          : +{params['clip_db']:.2f} dB")
    if params["glitch_enable"]:
        print(f"Glitches            : ON ({params['glitch_events_per_min']:.0f}/min, {params['glitch_ms']}ms)")
    else:
        print("Glitches            : OFF")
    if seed is not None:
        print(f"Seed                : {seed}")
    print("-----------------------\n")

    with tempfile.TemporaryDirectory(prefix="limewireify_") as td:
        td = Path(td)

        # Normalize input to WAV once (clean base)
        base_wav = td / "base.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_file), "-c:a", "pcm_s16le", str(base_wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Optional glitch stage at high destroy
        work_wav = td / "work.wav"
        if params["glitch_enable"]:
            apply_simple_glitches(base_wav, work_wav, params, seed=seed)
        else:
            shutil.copyfile(base_wav, work_wav)

        # MP3 generations
        cur = work_wav
        for i in range(1, params["passes"] + 1):
            nxt = td / f"gen{i}.mp3"
            encode_mp3(cur, nxt, params, desc=f"Encode pass {i}/{params['passes']}")
            cur = nxt

        shutil.copyfile(cur, output_file)

    print(f"Done: {output_file}")


if __name__ == "__main__":
    main()