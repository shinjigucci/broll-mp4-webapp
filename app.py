import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

import imageio_ffmpeg
import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY", "")
DEFAULT_VOICE_ID = os.getenv("FISH_AUDIO_VOICE_ID", "")
DEFAULT_MODEL = os.getenv("FISH_AUDIO_MODEL", "s2-pro")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "replace-this-in-production")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

jobs = {}
jobs_lock = threading.Lock()
generation_lock = threading.Lock()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ORIENTATIONS = {
    "portrait": (720, 1280),
    "landscape": (1280, 720),
    "square": (720, 720),
}


def now_label():
    return time.strftime("%Y%m%d-%H%M%S")


def is_logged_in():
    return True


def require_login():
    return None


def set_job(job_id, **updates):
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        job.setdefault("created_at", time.time())
        job.update(updates)
        job["updated_at"] = time.time()
    if "message" in updates or "status" in updates:
        app.logger.info("job=%s status=%s progress=%s message=%s", job_id, job.get("status"), job.get("progress"), job.get("message"))


def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))


def sort_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in split_number(path.stem)]


def split_number(text):
    parts = []
    current = ""
    current_is_digit = None
    for char in text:
        is_digit = char.isdigit()
        if current and is_digit != current_is_digit:
            parts.append(current)
            current = char
        else:
            current += char
        current_is_digit = is_digit
    if current:
        parts.append(current)
    return parts


def save_uploads(files, job_dir):
    upload_dir = job_dir / "uploads"
    slide_dir = job_dir / "slides_raw"
    upload_dir.mkdir(parents=True, exist_ok=True)
    slide_dir.mkdir(parents=True, exist_ok=True)

    for file in files:
        if not file or not file.filename:
            continue
        filename = secure_filename(file.filename)
        if not filename:
            filename = f"upload-{uuid.uuid4().hex}"
        target = upload_dir / filename
        file.save(target)

        suffix = target.suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(target) as archive:
                for info in archive.infolist():
                    inner = Path(info.filename)
                    if inner.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    safe_name = secure_filename(inner.name) or f"slide-{uuid.uuid4().hex}{inner.suffix.lower()}"
                    out_path = slide_dir / safe_name
                    with archive.open(info) as source, open(out_path, "wb") as dest:
                        shutil.copyfileobj(source, dest)
        elif suffix in IMAGE_EXTENSIONS:
            shutil.copy2(target, slide_dir / filename)

    slides = sorted([p for p in slide_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS], key=sort_key)
    if not slides:
        raise ValueError("スライド画像が見つかりませんでした。PNG/JPG/WebP、または画像入りZIPをアップロードしてください。")
    return slides


def render_slide_images(raw_slides, job_dir, size, fit_mode, subtitle_space_percent):
    out_dir = job_dir / "slides_rendered"
    out_dir.mkdir(exist_ok=True)
    width, height = size
    subtitle_space = int(height * subtitle_space_percent / 100)
    content_height = max(1, height - subtitle_space)
    rendered = []

    for index, raw in enumerate(raw_slides, start=1):
        with Image.open(raw) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            if fit_mode == "cover":
                fitted = ImageOps.fit(image, (width, content_height), method=Image.Resampling.LANCZOS)
                if subtitle_space:
                    canvas = Image.new("RGB", (width, height), (12, 16, 24))
                    canvas.paste(fitted, (0, 0))
                    fitted = canvas
            else:
                fitted = ImageOps.contain(image, (width, content_height), method=Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (width, height), (12, 16, 24))
                x = (width - fitted.width) // 2
                y = max(0, (content_height - fitted.height) // 2)
                canvas.paste(fitted, (x, y))
                fitted = canvas
            out_path = out_dir / f"slide-{index:03d}.jpg"
            fitted.save(out_path, quality=92, optimize=True)
            rendered.append(out_path)
    return rendered


def normalize_tts_script(script):
    text = script.strip()
    replacements = [
        (r"Claude\s*Code", "クロードコード"),
        (r"Claude", "クロード"),
        (r"SNS", "エスエヌエス"),
        (r"L\s*P", "エルピー"),
        (r"LINE", "ライン"),
        (r"AI", "エーアイ"),
        (r"You\s*Tube", "ユーチューブ"),
        (r"Instagram", "インスタグラム"),
        (r"PDF", "ピーディーエフ"),
        (r"URL", "ユーアールエル"),
        (r"CTA", "シーティーエー"),
        (r"VREW", "ブリュー"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#*_`~<>\\[\\]{}|^=]", "、", text)
    text = text.replace("／", "、").replace("/", "、")
    text = text.replace("&", "アンド")
    text = text.replace("%", "パーセント")
    text = text.replace("：", "。").replace(":", "。")
    text = re.sub(r"[ \t　]+", "、", text)
    text = re.sub(r"\n{2,}", "。", text)
    text = re.sub(r"\n", "。", text)

    boundary_terms = [
        "訴求フレーズ集",
        "セミナー案内",
        "ライン案内",
        "ブログ投稿",
        "動画台本",
        "投稿文",
        "無料教材",
        "無料オファー",
        "プロフィール",
        "キャプション",
        "フローマップ",
        "プロンプト",
        "クロードコード",
        "エスエヌエス",
        "ユーチューブ",
        "インスタグラム",
        "ピーディーエフ",
        "エルピー",
        "ライン",
        "エーアイ",
        "画像作り",
        "画像",
        "投稿",
        "販売",
        "教育",
        "導線",
        "商品",
        "ターゲット",
    ]
    boundary_terms = sorted(set(boundary_terms), key=len, reverse=True)
    result = []
    index = 0
    while index < len(text):
        matched = None
        for term in boundary_terms:
            if text.startswith(term, index):
                matched = term
                break
        if matched:
            if result and result[-1] not in "、。！？":
                result.append("、")
            result.append(matched)
            next_index = index + len(matched)
            if next_index < len(text) and text[next_index] not in "、。！？":
                result.append("、")
            index = next_index
        else:
            result.append(text[index])
            index += 1

    text = "".join(result)
    text = re.sub(r"、{2,}", "、", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"、(まで|から|へ|に|で|を|は|が|と|も|や|の|など|だけ|では|なら|として)", r"\1", text)
    text = re.sub(r"(そして|また|さらに|ただ|でも|まず|次に|最後に|今回|今回の)、", r"\1、", text)
    text = re.sub(r"、([。！？])", r"\1", text)
    text = re.sub(r"([。！？])、", r"\1", text)
    return text.strip("。、 ")


def fish_tts(script, voice_id, model, speed, audio_path):
    if not FISH_AUDIO_API_KEY:
        raise RuntimeError("FISH_AUDIO_API_KEY が未設定です。")
    if not voice_id:
        raise RuntimeError("FISH_AUDIO_VOICE_ID が未設定です。")

    tts_text = normalize_tts_script(script)
    (audio_path.parent / "narration_tts_text.txt").write_text(tts_text, encoding="utf-8")

    payload = {
        "text": tts_text,
        "reference_id": voice_id,
        "format": "mp3",
        "mp3_bitrate": 128,
        "sample_rate": 44100,
        "normalize": True,
        "latency": "normal",
        "prosody": {"speed": speed, "volume": 0},
    }
    app.logger.info("fish_tts start chars=%s normalized_chars=%s voice_id_set=%s model=%s", len(script), len(tts_text), bool(voice_id), model or DEFAULT_MODEL)
    response = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={
            "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
            "Content-Type": "application/json",
            "model": model or DEFAULT_MODEL,
        },
        json=payload,
        timeout=(20, 240),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"FISH Audioで音声生成に失敗しました: {response.status_code} {response.text[:500]}")
    audio_path.write_bytes(response.content)
    app.logger.info("fish_tts done bytes=%s", len(response.content))


def ffmpeg_path():
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffprobe_duration(media_path):
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-i",
        str(media_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
    for line in proc.stderr.splitlines():
        if "Duration:" not in line:
            continue
        timestamp = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
        hh, mm, ss = timestamp.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    raise RuntimeError("音声の長さを取得できませんでした。")


def create_slideshow(slides, audio_path, video_path, slide_seconds, target_duration):
    list_path = video_path.parent / "slides.txt"
    with open(list_path, "w", encoding="utf-8") as handle:
        for slide in slides:
            handle.write(f"file '{slide.as_posix()}'\n")
            handle.write(f"duration {slide_seconds:.4f}\n")
        handle.write(f"file '{slides[-1].as_posix()}'\n")

    cmd = [
        ffmpeg_path(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-i",
        str(audio_path),
        "-t",
        f"{target_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "24",
        "-threads",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(video_path),
    ]
    app.logger.info("ffmpeg start slides=%s slide_seconds=%.2f target_duration=%.2f", len(slides), slide_seconds, target_duration)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore", timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"MP4生成に失敗しました: {proc.stderr[-1200:]}")
    app.logger.info("ffmpeg done output=%s bytes=%s", video_path.name, video_path.stat().st_size)


def run_job(job_id, form_data):
    job_dir = JOBS_DIR / job_id
    try:
        if generation_lock.locked():
            set_job(job_id, status="queued", message="前の生成が終わるまで待機しています。", progress=5)

        with generation_lock:
            set_job(job_id, status="running", message="スライドを整理しています。", progress=10)
            raw_slides = [Path(path) for path in form_data["raw_slides"]]

            orientation = form_data["orientation"]
            size = ORIENTATIONS.get(orientation, ORIENTATIONS["portrait"])
            slides = render_slide_images(
                raw_slides,
                job_dir,
                size,
                form_data["fit_mode"],
                form_data["subtitle_space_percent"],
            )
            app.logger.info("slides rendered job=%s count=%s size=%s", job_id, len(slides), size)

            script_path = job_dir / "script.txt"
            script_path.write_text(form_data["script"], encoding="utf-8")

            set_job(job_id, message="FISH Audioでナレーションを生成しています。", progress=35)
            audio_path = job_dir / "narration.mp3"
            fish_tts(form_data["script"], form_data["voice_id"], form_data["model"], form_data["speed"], audio_path)

            set_job(job_id, message="音声尺に合わせてMP4を作っています。", progress=70)
            duration = ffprobe_duration(audio_path)
            slide_seconds = max(1.0, duration / len(slides))
            target_duration = duration
            video_path = job_dir / f"broll-video-{now_label()}.mp4"
            create_slideshow(slides, audio_path, video_path, slide_seconds, target_duration)

            meta = {
                "slides": len(slides),
                "duration_seconds": round(duration, 1),
                "slide_seconds": round(slide_seconds, 2),
                "target_duration_seconds": round(target_duration, 1),
                "orientation": orientation,
                "subtitle_space_percent": form_data["subtitle_space_percent"],
                "video": video_path.name,
            }
            (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            set_job(job_id, status="done", message="完成しました。", progress=100, video=str(video_path), meta=meta)
    except Exception as exc:
        app.logger.exception("job failed job=%s", job_id)
        set_job(job_id, status="error", message=str(exc), progress=100)


@app.route("/", methods=["GET", "POST"])
def index():
    return render_template("index.html", default_voice_id=DEFAULT_VOICE_ID, default_model=DEFAULT_MODEL)


@app.route("/create", methods=["POST"])
def create():
    require_login()
    script = request.form.get("script", "").strip()
    if not script:
        return jsonify({"error": "台本を入力してください。"}), 400

    files = request.files.getlist("slides")
    if not files:
        return jsonify({"error": "スライド画像またはZIPをアップロードしてください。"}), 400

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        raw_slides = save_uploads(files, job_dir)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    form_data = {
        "script": script,
        "raw_slides": [str(path) for path in raw_slides],
        "orientation": request.form.get("orientation", "portrait"),
        "fit_mode": request.form.get("fit_mode", "contain"),
        "subtitle_space_percent": float(request.form.get("subtitle_space_percent", "18")),
        "voice_id": request.form.get("voice_id", "").strip() or DEFAULT_VOICE_ID,
        "model": request.form.get("model", "").strip() or DEFAULT_MODEL,
        "speed": float(request.form.get("speed", "1.0")),
    }

    set_job(job_id, status="queued", message="受付しました。", progress=0)
    thread = threading.Thread(target=run_job, args=(job_id, form_data), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status_url": url_for("status", job_id=job_id)})


@app.route("/status/<job_id>")
def status(job_id):
    require_login()
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "ジョブが見つかりません。"}), 404
    if job.get("status") == "done":
        job["download_url"] = url_for("download", job_id=job_id)
    if job.get("created_at"):
        job["elapsed_seconds"] = round(time.time() - job["created_at"])
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    require_login()
    job = get_job(job_id)
    video = job.get("video")
    if not video or not Path(video).exists():
        return "MP4が見つかりません。", 404
    return send_file(video, as_attachment=True, download_name=Path(video).name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
