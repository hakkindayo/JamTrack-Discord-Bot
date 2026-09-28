import discord
from discord import app_commands
import json
import tempfile
from urllib.parse import quote
import os
import glob
import aiohttp
import asyncio
import re
import base64
import shutil
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

# ================= CONFIG =================
# 秘密情報(トークン・Webhook URL)はコードに書かず、環境変数から読み込む

def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。Renderの Environment で設定してください。")
    return value

TOKEN = _require_env("DISCORD_TOKEN")
WEBHOOK_URL = _require_env("WEBHOOK_URL")              # 新曲通知用Webhook
AUDIO_WEBHOOK_URL = _require_env("AUDIO_WEBHOOK_URL")  # 音源アップロード用Webhook
GUILD_ID = int(os.environ.get("GUILD_ID", "1220291861621506099"))
MENTION_ID = os.environ.get("MENTION_ID", "990884675989958676")

SPARK_API = "https://fortnitecontent-website-prod07.ol.epicgames.com/content/api/pages/fortnite-game/spark-tracks"
CDN_BASE = "https://cdn.qstv.on.epicgames.com/"

# 同梱ファイル(フォント・アイコン)はリポジトリ内の assets/ に置く
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

# 保存データ。Renderの Disk を /data にマウントすると再デプロイ後も残る
DATA_DIR = os.environ.get("DATA_DIR", "/data")
IMG_DIR = os.path.join(DATA_DIR, "images")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
TEMP_SEND_DIR = os.path.join(DATA_DIR, "tmp_send")

SNAPSHOT_FILE = os.path.join(DATA_DIR, "tracks_snapshot.json")
PENDING_AUDIO_FILE = os.path.join(DATA_DIR, "pending_audio.json")
CHANNEL_CONFIG_FILE = os.path.join(DATA_DIR, "channel_config.json")

FONT_PATH = os.path.join(ASSETS_DIR, "NotoSansJP-Black.ttf")

CHECK_INTERVAL = 1
MAX_CONCURRENT_IMAGES = 6
BATCH_SIZE = 10

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(TEMP_SEND_DIR, exist_ok=True)

# ================= チャンネル設定読み込み =================

def load_channel_config():
    return load_json(CHANNEL_CONFIG_FILE, {"watch": None, "notify": None})

def save_channel_config(watch_id, notify_id):
    save_json(CHANNEL_CONFIG_FILE, {"watch": watch_id, "notify": notify_id})

# ================= 原神JSONロード =================

_json_path_cache = None

def find_json(filename="genshin_names.json"):
    global _json_path_cache
    if _json_path_cache is not None:
        return _json_path_cache
    for path in (os.path.join(ASSETS_DIR, filename), os.path.join(BASE_DIR, filename), os.path.join(DATA_DIR, filename)):
        if os.path.exists(path):
            _json_path_cache = path
            return path
    raise FileNotFoundError(f"{filename} が見つかりませんでした(assets/ に置いてください)")

json_path = None  # 起動時には探索しない。実際に使うときだけ find_json() を呼ぶ

def load_characters():
    global json_path
    if json_path is None:
        json_path = find_json()
        print(f"JSONパス: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)

# ================= UTILS =================

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

async def fetch_json(session, url):
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    async with session.get(url, headers=headers) as r:
        r.raise_for_status()
        return await r.json()

def format_error_for_discord(prefix: str, e: Exception, limit: int = 1900) -> str:
    """Discordのメッセージ文字数上限(2000)を超えないよう、長いエラーは切り詰める。
    実際に役立つエラー理由は末尾に出ることが多いため、末尾を優先して残す。"""
    body = str(e)
    text = f"{prefix}: {body}"
    if len(text) > limit:
        # プレフィックス分を差し引いた上で、本文の「末尾」を優先して残す
        keep = limit - len(prefix) - len(": …(前略)\n")
        text = f"{prefix}: …(前略)\n{body[-keep:]}"
    return text

async def search_youtube_async(session, title, artist):
    query = f"{artist} - {title}".strip(" -")
    encoded = quote(query)
    search_url = f"https://www.youtube.com/results?search_query={encoded}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(search_url, headers=headers) as r:
            html = await r.text()
        ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        if ids:
            return f"https://www.youtube.com/watch?v={ids[0]}"
    except Exception as e:
        print(f"YouTube検索エラー: {e}")
    return None

# ================= IMAGE =================

CANVAS_WIDTH = 1000
CANVAS_HEIGHT = 520
TEXT_AREA_BOTTOM = CANVAS_HEIGHT - 30

def rounded(img, r):
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, *img.size), r, fill=255)
    img.putalpha(mask)
    return img

def _wrap_word_by_char(draw, word, font, max_width):
    lines = []
    current = ""
    for ch in word:
        candidate = current + ch
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines

def _wrap_by_pixel_width(draw, text, font, max_width):
    if not text:
        return [""]
    words = text.split(" ")
    lines = []
    current = ""
    for word in words:
        if not word:
            continue
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            if draw.textlength(word, font=font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                    current = ""
                sub_lines = _wrap_word_by_char(draw, word, font, max_width)
                lines.extend(sub_lines[:-1])
                current = sub_lines[-1] if sub_lines else ""
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [""]

def _truncate_with_ellipsis(draw, text, font, max_width):
    while draw.textlength(text + "…", font=font) > max_width and len(text) > 1:
        text = text[:-1]
    return text + "…"

def draw_auto_text(draw, text, x, y, max_width, font_path, start_size, min_size, color, max_lines, max_bottom_y=None):
    if max_bottom_y is None:
        max_bottom_y = TEXT_AREA_BOTTOM

    font = None
    lines = None
    for size in range(start_size, min_size - 1, -2):
        candidate_font = ImageFont.truetype(font_path, size)
        candidate_lines = _wrap_by_pixel_width(draw, text, candidate_font, max_width)
        line_height = size + 6
        total_height = line_height * min(len(candidate_lines), max_lines)
        if len(candidate_lines) <= max_lines and y + total_height <= max_bottom_y:
            font, lines = candidate_font, candidate_lines
            break

    if font is None:
        font = ImageFont.truetype(font_path, min_size)
        lines = _wrap_by_pixel_width(draw, text, font, max_width)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _truncate_with_ellipsis(draw, lines[-1], font, max_width)

    y_start = y
    for line in lines:
        if y + font.size > max_bottom_y:
            break
        draw.text((x, y), line, fill=color, font=font)
        y += font.size + 6
    return y - y_start

def draw_speaker_icon(draw, x, y, size, color=(255, 255, 255, 230)):
    """左下などに置く、シンプルな音源(スピーカー)マークをベクター図形で描く。"""
    h = size
    w = int(size * 0.9)
    # スピーカー本体(台形+四角)
    box_w = int(w * 0.32)
    box_h = int(h * 0.4)
    box_top = y + (h - box_h) // 2
    draw.rectangle(
        [x, box_top, x + box_w, box_top + box_h],
        fill=color,
    )
    cone_tip_x = x + int(w * 0.62)
    draw.polygon(
        [
            (x + box_w, box_top),
            (cone_tip_x, y),
            (cone_tip_x, y + h),
            (x + box_w, box_top + box_h),
        ],
        fill=color,
    )
    # 音波(弧)を2本
    for i, r in enumerate([int(w * 0.18), int(w * 0.34)]):
        arc_box = [
            cone_tip_x - r, y + h // 2 - r,
            cone_tip_x + r, y + h // 2 + r,
        ]
        draw.arc(arc_box, start=-40, end=40, fill=color, width=max(2, size // 12))

ICON_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "icon.png")

async def create_track_image(session, sem, key, track):
    async with sem:
        async with session.get(track["au"]) as r:
            img_bytes = await r.read()
        cover = Image.open(BytesIO(img_bytes)).convert("RGBA")

        if os.path.exists(ICON_TEMPLATE_PATH):
            bg = Image.open(ICON_TEMPLATE_PATH).convert("RGBA")
        else:
            bg = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (15, 23, 42, 255))

        canvas_w, canvas_h = bg.size
        draw = ImageDraw.Draw(bg)

        # ジャケットを中央上寄りに大きめに配置
        cover_size = int(canvas_w * 0.62)
        cover = rounded(cover.resize((cover_size, cover_size)), 40)
        cover_x = (canvas_w - cover_size) // 2
        cover_y = int(canvas_h * 0.06)
        bg.paste(cover, (cover_x, cover_y), cover)

        # ジャケットの下に半透明の黒帯を敷いて文字を読みやすくする
        band_top = cover_y + cover_size + 16
        if band_top < canvas_h:
            band = Image.new("RGBA", (canvas_w, canvas_h - band_top), (0, 0, 0, 160))
            bg.alpha_composite(band, (0, band_top))

        text_x = 40
        text_max_width = canvas_w - text_x * 2
        text_y = band_top + 18
        bottom_limit = canvas_h - 16

        title_h = draw_auto_text(
            draw, track["tt"], text_x, text_y, text_max_width, FONT_PATH,
            48, 24, "white", 2, max_bottom_y=bottom_limit,
        )
        artist_h = draw_auto_text(
            draw, track["an"], text_x, text_y + title_h + 8, text_max_width, FONT_PATH,
            30, 18, (210, 225, 235), 1, max_bottom_y=bottom_limit,
        )

        ti_raw = track.get("ti", "")
        song_id = ti_raw.split(":", 1)[-1] if ":" in ti_raw else ti_raw
        if song_id:
            draw_auto_text(
                draw, f"ID: {song_id}", text_x, text_y + title_h + 8 + artist_h + 8, text_max_width, FONT_PATH,
                22, 14, (100, 200, 255), 1, max_bottom_y=bottom_limit,
            )

        path = os.path.join(IMG_DIR, f"{key}.png")
        bg.convert("RGB").save(path)
        return path

# ================= 差分検知 =================

FIELD_LABELS = {
    "tt": "曲名", "an": "アーティスト", "ab": "アルバム", "ry": "リリース年",
    "mt": "テンポ(BPM)", "mk": "キー(調)", "mm": "スケール(メジャー/マイナー)",
    "dn": "曲の長さ(秒)", "ge": "ジャンル", "gt": "ゲームプレイタグ",
    "ar": "ESRBレーティング", "au": "アルバムアートURL", "ti": "テンプレートID",
    "sn": "API用トラック名", "jc": "Jamリンクコード", "in": "難易度情報",
    "qi": "音源データ(Quicksilver)", "mu": "MIDIデータURL(暗号化)",
    "ld": "リップシンクデータURL", "isrc": "ISRCコード", "su": "イベントUUID",
    "sib": "開始楽器(ベース)", "sid": "開始楽器(ドラム)", "sig": "開始楽器(ギター)",
    "siv": "開始楽器(ボーカル)", "mmo": "楽曲メタ情報", "nu": "追加/更新日時",
}

INTENSITY_LABELS = {
    "bd": "バンド", "pb": "プロベース", "pd": "プロドラム", "vl": "ボーカル",
    "pg": "プロギター", "gr": "ギター", "ds": "ドラム", "ba": "ベース",
}

_MAX_VALUE_LEN = 40
TRACKED_FIELDS = ["tt", "an", "au"]

def _format_scalar(v):
    if v is None:
        return "なし"
    s = str(v)
    return s if len(s) <= _MAX_VALUE_LEN else "(長い値のため省略)"

def diff_track(old, new):
    if old is None:
        return ["詳細不明（前回データなし）"]
    changes = []
    for key in TRACKED_FIELDS:
        ov, nv = old.get(key), new.get(key)
        if ov == nv:
            continue
        label = FIELD_LABELS.get(key, f"({key})")
        if isinstance(ov, dict) or isinstance(nv, dict):
            ov_d, nv_d = ov or {}, nv or {}
            sub_labels = INTENSITY_LABELS if key == "in" else {}
            sub_changes = [
                f"{sub_labels.get(sk, sk)}: {_format_scalar(ov_d.get(sk))} → {_format_scalar(nv_d.get(sk))}"
                for sk in sorted(set(ov_d.keys()) | set(nv_d.keys()))
                if sk != "_type" and ov_d.get(sk) != nv_d.get(sk)
            ]
            changes.append(f"{label}（{', '.join(sub_changes)}）" if sub_changes else label)
        else:
            changes.append(f"{label}: {_format_scalar(ov)} → {_format_scalar(nv)}")
    return changes or ["曲名/アーティスト/アートワーク/難易度以外の項目が更新されました"]

# ================= DISCORD WEBHOOK =================

async def send_webhook(session, label, tracks, files, is_update=False, changes_map=None):
    changes_map = changes_map or {}
    total = len(tracks)
    batches = [
        (tracks[i:i+BATCH_SIZE], files[i:i+BATCH_SIZE])
        for i in range(0, total, BATCH_SIZE)
    ]
    tag = "（更新）" if is_update else ""
    for batch_idx, (batch_tracks, batch_files) in enumerate(batches):
        if batch_idx == 0:
            lines = [f"<@{MENTION_ID}> {label}"]
        else:
            lines = [f"<@{MENTION_ID}> {label}（続き {batch_idx+1}/{len(batches)}）"]
        offset = batch_idx * BATCH_SIZE
        for n, (k, t) in enumerate(batch_tracks, offset + 1):
            ti_raw = t.get("ti", "")
            song_id = ti_raw.split(":", 1)[-1] if ":" in ti_raw else ti_raw
            song_id_str = f" *{song_id}*" if song_id else ""
            lines.append(f"{n}. {t['an']} - {t['tt']}{song_id_str}{tag}")
            if is_update:
                for c in changes_map.get(k, []):
                    lines.append(f"    ・{c}")
        form = aiohttp.FormData()
        form.add_field("content", "\n".join(lines))
        for idx, path in enumerate(batch_files):
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    form.add_field(
                        f"file{idx}", f.read(),
                        filename=os.path.basename(path),
                        content_type="image/png"
                    )
        async with session.post(WEBHOOK_URL, data=form) as resp:
            if resp.status not in (200, 204):
                print(f"webhook送信エラー: {resp.status} (バッチ {batch_idx+1})")
        if batch_idx < len(batches) - 1:
            await asyncio.sleep(1)

# ================= AUDIO/VIDEO EXTRACTION =================

def sanitize_filename(name: str) -> str:
    invalid_chars = '<>:"/\\|?*'
    for ch in invalid_chars:
        name = name.replace(ch, "_")
    return name.strip(" .")

def is_track_active(value: dict) -> bool:
    active_date_raw = value.get("_activeDate")
    if not active_date_raw:
        return False
    try:
        normalized = active_date_raw.replace("Z", "+00:00")
        active_date = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return active_date <= datetime.now(timezone.utc)

def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

async def _download_bytes(session, url):
    async with session.get(url) as r:
        r.raise_for_status()
        return await r.read()

async def acquire_mpd_playlist_async(session, pid: str) -> str:
    async with session.get(CDN_BASE + pid) as r:
        r.raise_for_status()
        data = await r.json()
    playlist_b64 = data.get("playlist") or data.get("Playlist")
    if not playlist_b64:
        raise KeyError(f"レスポンスに playlist キーが見つかりませんでした: {data}")
    return base64.b64decode(playlist_b64).decode("utf-8")

async def download_and_stitch_segments_async(session, mpd_content: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cleaned = mpd_content.strip().replace('xmlns:xsi=""', "").replace('xsi:schemaLocation=""', "")

    root = ET.fromstring(cleaned)
    ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else ""
    ns = {"m": ns_uri} if ns_uri else {}
    def q(tag):
        return f"m:{tag}" if ns_uri else tag

    base_url = root.find(f".//{q('BaseURL')}", ns).text
    representation = root.find(f".//{q('Representation')}", ns)
    init_file = representation.find(q("BaseURL"), ns).text

    init_path = out_dir / init_file
    master_file = out_dir / "master_audio.mp4"

    segment_template = representation.find(q("SegmentTemplate"), ns)
    segment_base = representation.find(q("SegmentBase"), ns)

    if segment_template is not None:
        init_bytes = await _download_bytes(session, base_url + init_file)
        init_path.write_bytes(init_bytes)
        with open(master_file, "wb") as out_f:
            out_f.write(init_bytes)
            media_attr = segment_template.get("media")
            start_number = int(segment_template.get("startNumber", "1"))
            segment_timeline = segment_template.find(q("SegmentTimeline"), ns)
            segment_count = 0
            if segment_timeline is not None:
                for s in segment_timeline.findall(q("S"), ns):
                    segment_count += 1 + int(s.get("r", "0"))
            number = start_number
            for _ in range(segment_count):
                seg_name = media_attr.replace("$Number$", str(number))
                seg_bytes = await _download_bytes(session, base_url + seg_name)
                out_f.write(seg_bytes)
                number += 1
        init_path.unlink(missing_ok=True)
    elif segment_base is not None:
        full_bytes = await _download_bytes(session, base_url + init_file)
        master_file.write_bytes(full_bytes)
    else:
        init_bytes = await _download_bytes(session, base_url + init_file)
        init_path.write_bytes(init_bytes)
        init_path.rename(master_file)

    return master_file

def _ensure_fontconfig_file() -> str:
    """
    Windows版ffmpeg(full build)はdrawtextフィルタ使用時、フォントを直接指定していても
    内部でFontconfigの初期化を試み、設定ファイルが無いとクラッシュ(WinError/Access Violation)する。
    最小限の空設定ファイルを用意して読ませることでこれを回避する。
    """
    conf_path = os.path.join(DATA_DIR, "fonts.conf")
    if not os.path.exists(conf_path):
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n</fontconfig>\n')
    return conf_path

async def _run_ffmpeg(args: list) -> None:
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = _ensure_fontconfig_file()
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg失敗 (code {proc.returncode}): {stderr.decode(errors='ignore')}")

async def convert_to_ogg_async(master_audio_path: Path, out_dir: Path) -> Path:
    ogg_path = out_dir / "preview.ogg"
    await _run_ffmpeg(["ffmpeg", "-y", "-i", str(master_audio_path), "-acodec", "libopus", "-ar", "48000", str(ogg_path)])
    master_audio_path.unlink()
    return ogg_path

async def _get_audio_duration(path: Path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None

# ================= ここが変更箇所 =================

_ascii_font_path_cache = None

def _get_ascii_font_path() -> str:
    """
    Windows版ffmpegはdrawtextフィルタに日本語を含むパスを渡すと、内部の
    フィルタ文字列パーサーで正しく解釈できずフォントを読み込めないことがある
    (Cannot find a valid font for the family Sans エラーの原因)。
    回避策として、フォントファイルを英数字のみの一時パスにコピーして使う。
    """
    global _ascii_font_path_cache
    if _ascii_font_path_cache is not None and os.path.exists(_ascii_font_path_cache):
        return _ascii_font_path_cache

    if not os.path.exists(FONT_PATH):
        raise RuntimeError(
            f"フォントファイルが見つかりません: {FONT_PATH}\n"
            f"このパスに NotoSansJP-Black.ttf を配置してください。"
        )

    ascii_dir = os.path.join(tempfile.gettempdir(), "jamtrack_bot_assets")
    os.makedirs(ascii_dir, exist_ok=True)
    ascii_path = os.path.join(ascii_dir, "font.ttf")
    shutil.copyfile(FONT_PATH, ascii_path)
    _ascii_font_path_cache = ascii_path
    return ascii_path

async def create_video_with_art_async(session, ogg_path: Path, image_path, out_dir: Path) -> Path:
    """
    image_path: create_track_imageで既に(icon.png + ジャケット + テキスト)合成済みのPNGパス。
    動画用にだけ、左下に音源マーク(スピーカーアイコン)を追加してから音声と合成する。
    (静止画の方にはアイコンを付けないため、ここでコピーして加工する)
    """
    frame = Image.open(image_path).convert("RGBA")
    draw = ImageDraw.Draw(frame)
    icon_size = int(frame.width * 0.05)
    draw_speaker_icon(draw, 24, frame.height - icon_size - 20, icon_size)
    video_frame_path = out_dir / "video_frame.png"
    frame.convert("RGB").save(video_frame_path)

    # 音源の長さを取得
    duration = await _get_audio_duration(ogg_path)

    video_path = out_dir / "video.mp4"

    ffmpeg_args = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(video_frame_path),
        "-i", str(ogg_path),
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
    ]
    if duration is not None:
        ffmpeg_args += ["-t", f"{duration:.3f}"]
    else:
        ffmpeg_args += ["-shortest"]
    ffmpeg_args.append(str(video_path))

    await _run_ffmpeg(ffmpeg_args)
    video_frame_path.unlink(missing_ok=True)
    return video_path

async def extract_audio_and_video(session, track: dict, out_dir: Path | None = None, make_video: bool = True, image_path=None):
    qi_raw = track.get("qi")
    if not qi_raw:
        return None
    qi = json.loads(qi_raw)
    pid = qi.get("pid")
    if not pid:
        return None

    title = track.get("tt", "")
    artist = track.get("an", "")

    if out_dir is None:
        folder_name = sanitize_filename(f"{artist} - {title}") or pid
        out_dir = Path(AUDIO_DIR) / folder_name

    mpd = await acquire_mpd_playlist_async(session, pid)
    mp4_path = await download_and_stitch_segments_async(session, mpd, out_dir)
    ogg_path = await convert_to_ogg_async(mp4_path, out_dir)

    video_path = None
    if make_video and image_path and os.path.exists(image_path):
        video_path = await create_video_with_art_async(session, ogg_path, image_path, out_dir)

    return ogg_path, video_path

# ================= 音源フォルダー → Discord送信 =================

UPLOAD_RECORD_FILE = os.path.join(AUDIO_DIR, "upload_record.json")
AUDIO_VIDEO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".mp4", ".mov", ".mkv", ".webm"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
UPLOAD_SCAN_INTERVAL = 1800

def load_upload_record():
    return load_json(UPLOAD_RECORD_FILE, {})

def save_upload_record(record):
    save_json(UPLOAD_RECORD_FILE, record)

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

async def _post_form(session, folder_name, chunk, is_first_chunk):
    form = aiohttp.FormData()
    form.add_field("content", f"📁 **{folder_name}**" if is_first_chunk else "")
    for idx, path in enumerate(chunk):
        with open(path, "rb") as f:
            form.add_field(f"file{idx}", f.read(), filename=os.path.basename(path))
    return await session.post(AUDIO_WEBHOOK_URL, data=form)

async def send_audio_files(session, folder_name, file_paths):
    for i in range(0, len(file_paths), MAX_FILES_PER_MESSAGE):
        chunk = file_paths[i:i + MAX_FILES_PER_MESSAGE]
        is_first = (i == 0)
        try:
            resp = await _post_form(session, folder_name, chunk, is_first)
            if resp.status == 429:
                body = await resp.json()
                retry_after = body.get("retry_after", 5)
                print(f"  ⏳ レート制限、{retry_after}秒待機して再送します")
                await asyncio.sleep(retry_after + 0.5)
                resp = await _post_form(session, folder_name, chunk, is_first)
            if resp.status not in (200, 204):
                text = await resp.text()
                print(f"  ✗ 送信失敗 (status={resp.status}): {text[:200]}")
                return False
        except aiohttp.ClientError as e:
            print(f"  ✗ 通信エラー: {e}")
            return False
        if i + MAX_FILES_PER_MESSAGE < len(file_paths):
            await asyncio.sleep(1)
    return True

async def scan_and_upload_audio(session):
    if not os.path.isdir(AUDIO_DIR):
        return
    record = load_upload_record()
    for folder_name in sorted(os.listdir(AUDIO_DIR)):
        folder_path = os.path.join(AUDIO_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        sent_hashes = set(record.get(folder_name, []))
        target_files = []
        for fname in sorted(os.listdir(folder_path)):
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_VIDEO_EXTS:
                continue
            size = os.path.getsize(fpath)
            if size > MAX_FILE_SIZE:
                print(f"[{folder_name}] ⚠ サイズ超過のためスキップ: {fname} ({size / 1024 / 1024:.1f}MB)")
                continue
            fhash = await asyncio.to_thread(file_hash, fpath)
            if fhash in sent_hashes:
                continue
            target_files.append((fpath, fhash))
        if not target_files:
            continue
        print(f"[{folder_name}] {len(target_files)}件をDiscordへ送信中...")
        paths = [t[0] for t in target_files]
        success = await send_audio_files(session, folder_name, paths)
        if success:
            hashes = record.setdefault(folder_name, [])
            for _, fhash in target_files:
                hashes.append(fhash)
            save_upload_record(record)
            print(f"  ✓ 送信完了、upload_record.jsonに記録しました")
        else:
            print(f"  ✗ {folder_name} の送信に一部失敗しました")

async def send_extracted_audio(session, ogg_path: Path, video_path):
    files = [p for p in (ogg_path, video_path) if p and p.exists()]
    if not files:
        return
    folder_name = ogg_path.parent.name
    record = load_upload_record()
    sent_hashes = set(record.get(folder_name, []))
    targets = []
    for p in files:
        size = p.stat().st_size
        if size > MAX_FILE_SIZE:
            print(f"[{folder_name}] ⚠ サイズ超過のためスキップ: {p.name} ({size / 1024 / 1024:.1f}MB)")
            continue
        fhash = await asyncio.to_thread(file_hash, str(p))
        if fhash in sent_hashes:
            continue
        targets.append((str(p), fhash))
    if not targets:
        return
    success = await send_audio_files(session, folder_name, [t[0] for t in targets])
    if success:
        hashes = record.setdefault(folder_name, [])
        for _, fhash in targets:
            hashes.append(fhash)
        save_upload_record(record)
        print(f"[{folder_name}] ✓ 抽出直後にDiscordへ送信完了")
    else:
        print(f"[{folder_name}] ✗ 抽出直後の送信に失敗しました")

async def upload_loop():
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print("📤 音源アップロード監視開始")
        while True:
            try:
                await scan_and_upload_audio(session)
            except Exception as e:
                print("upload_loop error:", e)
            await asyncio.sleep(UPLOAD_SCAN_INTERVAL)

# ================= JAM TRACK 監視ループ =================

async def spark_loop():
    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=60)
    sem = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        print("⚡ Jam Track 監視開始")
        while True:
            try:
                snapshot = load_json(SNAPSHOT_FILE, {})
                data = await fetch_json(session, SPARK_API)
                current_snapshot = dict(snapshot)
                new_tracks = []

                for key, value in data.items():
                    if not isinstance(value, dict):
                        continue
                    last_modified = value.get("lastModified")
                    prev_entry = snapshot.get(key)
                    prev_last_modified = (
                        prev_entry.get("lastModified") if isinstance(prev_entry, dict) else None
                    )
                    prev_track = (
                        prev_entry.get("track") if isinstance(prev_entry, dict) else None
                    )
                    is_new = key not in snapshot
                    is_update = (not is_new) and (last_modified != prev_last_modified)
                    if is_new or is_update:
                        new_tracks.append((key, value["track"], is_update, prev_track))

                if not snapshot:
                    for key, value in data.items():
                        if not isinstance(value, dict):
                            continue
                        current_snapshot[key] = {
                            "lastModified": value.get("lastModified"),
                            "track": value["track"],
                        }
                    save_json(SNAPSHOT_FILE, current_snapshot)
                else:
                    if new_tracks:
                        tasks = [
                            create_track_image(session, sem, key, track)
                            for key, track, _, _ in new_tracks
                        ]
                        results = await asyncio.gather(*tasks)
                        brand_new = [(k, t) for (k, t, u, _), img in zip(new_tracks, results) if not u]
                        brand_new_imgs = [img for (k, t, u, _), img in zip(new_tracks, results) if not u]
                        updated = [(k, t) for (k, t, u, _), img in zip(new_tracks, results) if u]
                        updated_imgs = [img for (k, t, u, _), img in zip(new_tracks, results) if u]
                        updated_changes = {
                            k: diff_track(pt, t)
                            for (k, t, u, pt) in new_tracks if u
                        }
                        if brand_new:
                            await send_webhook(session, "🆕 新Jam Track", brand_new, brand_new_imgs, is_update=False)
                        if updated:
                            await send_webhook(
                                session, "🔄 Jam Track", updated, updated_imgs,
                                is_update=True, changes_map=updated_changes,
                            )
                        for key, track, is_update, _ in new_tracks:
                            entry = current_snapshot.get(key, {})
                            entry = dict(entry) if isinstance(entry, dict) else {}
                            entry["lastModified"] = data[key].get("lastModified")
                            entry["track"] = track
                            current_snapshot[key] = entry
                    save_json(SNAPSHOT_FILE, current_snapshot)

                    # ---- 公開日ゲート付き自動抽出 ----
                    pending = load_json(PENDING_AUDIO_FILE, [])
                    pending_keys = set(pending) | {key for key, _, _, _ in new_tracks}
                    still_pending = []
                    for key in pending_keys:
                        value = data.get(key)
                        if not isinstance(value, dict):
                            continue
                        if not is_track_active(value):
                            still_pending.append(key)
                            continue
                        try:
                            auto_image_path = None
                            try:
                                auto_image_path = await create_track_image(session, sem, key, value["track"])
                            except Exception as e:
                                print(f"画像生成エラー ({key}): {e}")
                            result = await extract_audio_and_video(session, value["track"], image_path=auto_image_path)
                        except Exception as e:
                            print(f"音声/動画抽出エラー ({key}): {e}")
                            continue
                        if result:
                            ogg_path, video_path = result
                            title = value["track"].get("tt", key)
                            artist = value["track"].get("an", "")
                            print(f"✅ 公開確認後、自動抽出完了: {artist} - {title} -> {ogg_path}")
                            await send_extracted_audio(session, ogg_path, video_path)
                            entry = current_snapshot.get(key, {})
                            if not isinstance(entry, dict):
                                entry = {}
                            entry["lastModified"] = value.get("lastModified")
                            entry["preview_audio"] = True
                            current_snapshot[key] = entry
                    save_json(PENDING_AUDIO_FILE, still_pending)
                    save_json(SNAPSHOT_FILE, current_snapshot)

            except Exception as e:
                print("loop error:", e)
            await asyncio.sleep(CHECK_INTERVAL)

# ================= BOT =================

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True   # ボイスチャンネル入退室の検知に必須
        intents.members = True        # メンバー情報取得のため(Developer PortalでもON)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        guild = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild)
        print("Commands synced")

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        # 再接続でon_readyが複数回呼ばれても、監視ループは1回だけ起動する
        if getattr(self, "_loops_started", False):
            return
        self._loops_started = True
        asyncio.create_task(spark_loop())
        asyncio.create_task(upload_loop())

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # ボイスチャンネルへの「新規参加」のみ反応(退出・移動時は無視)
        if before.channel is not None or after.channel is None:
            return
        if member.bot:
            return
        if member.id == int(MENTION_ID):
            # 通知先本人の入室は通知しない(必要なら削除)
            return

        notify_user = self.get_user(int(MENTION_ID))
        if notify_user is None:
            try:
                notify_user = await self.fetch_user(int(MENTION_ID))
            except discord.NotFound:
                print("通知先ユーザーが見つかりません。MENTION_ID を確認してください。")
                return

        joined_user_id = member.id  # 参加者のユーザーID(数字)を明示的に取得

        embed = discord.Embed(
            title="🔔 ボイスチャンネル参加通知",
            description=f"{member.mention} さんが参加しました",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="チャンネル", value=f"`{after.channel.name}`", inline=True)
        embed.add_field(name="ユーザー名", value=member.name, inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"ID: {joined_user_id}")

        try:
            await notify_user.send(content=f"<@{MENTION_ID}>", embed=embed)
        except discord.Forbidden:
            print("DM送信に失敗しました(DM拒否設定、または共通サーバーなしの可能性)。")
        return

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        config = load_channel_config()
        watch_id = config.get("watch")
        notify_id = config.get("notify")
        if not watch_id or not notify_id:
            return
        if message.channel.id != watch_id:
            return
        notify_channel = self.get_channel(notify_id)
        if not notify_channel:
            return

        lines = message.content.strip().splitlines()
        ids = [line.strip() for line in lines if line.strip()]
        if not ids:
            return

        async with aiohttp.ClientSession() as session:
            try:
                data = await fetch_json(session, SPARK_API)
            except Exception as e:
                await notify_channel.send(f"API取得エラー: {e}")
                return

            results = []
            not_found = []
            for sid in ids:
                found = False
                for key, value in data.items():
                    if not isinstance(value, dict):
                        continue
                    t = value.get("track", {})
                    ti_raw = t.get("ti", "")
                    song_id = ti_raw.split(":", 1)[-1] if ":" in ti_raw else ti_raw
                    if song_id.lower() == sid.lower():
                        title = t.get("tt", "不明")
                        artist = t.get("an", "不明")
                        yt_url = await search_youtube_async(session, title, artist)
                        yt_str = f"[YouTubeで見る]({yt_url})" if yt_url else "見つかりませんでした"
                        embed = discord.Embed(color=0x1db954)
                        embed.add_field(
                            name=f"🎵 {artist} - {title}",
                            value=f"`{song_id}`\n🔗 {yt_str}",
                            inline=False
                        )
                        if t.get("au"):
                            embed.set_thumbnail(url=t["au"])
                        results.append(embed)
                        found = True
                        break
                if not found:
                    not_found.append(f"❌ `{sid}` は見つかりませんでした")

        for embed in results:
            await notify_channel.send(embed=embed)
        if not_found:
            await notify_channel.send("\n".join(not_found))

bot = MyBot()

# ================= コマンド =================

@bot.tree.command(name="genshin", description="原神キャラ画像 (Enka)")
@app_commands.describe(name="キャラ名")
async def genshin(interaction: discord.Interaction, name: str):
    characters = load_characters()
    english = characters.get(name)
    if not english:
        await interaction.response.send_message("キャラが見つかりません", ephemeral=True)
        return
    gacha_url = f"https://enka.network/ui/UI_Gacha_AvatarImg_{english}.png"
    icon_url = f"https://enka.network/ui/UI_AvatarIcon_{english}.png"
    embed = discord.Embed(title=name, color=0x00ffcc)
    embed.set_thumbnail(url=icon_url)
    embed.set_image(url=gacha_url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="genshin_nanoka", description="原神キャラ画像 (Nanoka)")
@app_commands.describe(name="キャラ名")
async def genshin_nanoka(interaction: discord.Interaction, name: str):
    characters = load_characters()
    english = characters.get(name)
    if not english:
        await interaction.response.send_message("キャラが見つかりません", ephemeral=True)
        return
    gacha_url = f"https://static.nanoka.cc/assets/gi/UI_Gacha_AvatarImg_{english}.webp"
    icon_url = f"https://static.nanoka.cc/assets/gi/UI_AvatarIcon_{english}.webp"
    gacha_icon_url = f"https://static.nanoka.cc/assets/gi/UI_Gacha_AvatarIcon_{english}.webp"
    embed1 = discord.Embed(title=f"{name} (Nanoka)", color=0xffaa00)
    embed1.set_thumbnail(url=icon_url)
    embed1.set_image(url=gacha_url)
    embed2 = discord.Embed(color=0xffaa00)
    embed2.set_image(url=gacha_icon_url)
    await interaction.response.send_message(embeds=[embed1, embed2])

@bot.tree.command(name="track", description="Jam Track IDで曲を検索", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(id="Song ID (例: sid_placeholder_02)")
async def track(interaction: discord.Interaction, id: str):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, SPARK_API)
        except Exception as e:
            await interaction.followup.send(format_error_for_discord("❌ API取得エラー", e))
            return

        found_key = None
        found_track = None
        found_song_id = None
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            t = value.get("track", {})
            ti_raw = t.get("ti", "")
            song_id = ti_raw.split(":", 1)[-1] if ":" in ti_raw else ti_raw
            if song_id.lower() == id.lower():
                found_key = key
                found_track = t
                found_song_id = song_id
                break

        if not found_track:
            await interaction.followup.send(f"❌ ID `{id}` のトラックは見つかりませんでした。")
            return

        title = found_track.get("tt", "不明")
        artist = found_track.get("an", "不明")

        temp_dir = Path(TEMP_SEND_DIR)
        temp_dir.mkdir(parents=True, exist_ok=True)

        image_path = None
        ogg_path = None
        video_path = None
        try:
            sem = asyncio.Semaphore(1)
            image_path = await create_track_image(session, sem, found_key, found_track)
        except Exception as e:
            print(f"画像生成エラー ({found_key}): {e}")

        try:
            result = await extract_audio_and_video(session, found_track, out_dir=temp_dir, image_path=image_path)
            if result:
                ogg_path, video_path = result
        except Exception as e:
            await interaction.followup.send(format_error_for_discord("⚠️ 音源生成中にエラーが発生しました", e))

        # 合成画像と、音声入り動画の両方を送る
        files = []
        if image_path and os.path.exists(image_path):
            files.append(discord.File(image_path))
        if video_path and video_path.exists():
            files.append(discord.File(str(video_path)))
        elif ogg_path and ogg_path.exists():
            files.append(discord.File(str(ogg_path)))

        if not files:
            await interaction.followup.send(f"⚠️ `{found_song_id}` のプレビュー音源/画像を生成できませんでした。")
        else:
            await interaction.followup.send(
                content=f"{artist} - {title} **{found_song_id}**",
                files=files,
            )

        try:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"一時ファイル削除エラー: {e}")

# ================= チャンネル監視設定コマンド =================

@bot.tree.command(name="setchannel", description="監視チャンネルと通知先チャンネルを設定（特定ユーザー専用）", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(watch="監視するチャンネル", notify="通知を送るチャンネル")
async def setchannel(interaction: discord.Interaction, watch: discord.TextChannel, notify: discord.TextChannel):
    if interaction.user.id != int(MENTION_ID):
        await interaction.response.send_message("❌ このコマンドを使う権限がありません。", ephemeral=True)
        return
    save_channel_config(watch.id, notify.id)
    await interaction.response.send_message(
        f"✅ 監視チャンネル: <#{watch.id}>\n📢 通知チャンネル: <#{notify.id}>",
        ephemeral=True
    )

# ================= テスト用音声抽出コマンド =================

ALLOWED_USER_ID = int(MENTION_ID)

@bot.tree.command(
    name="testaudio",
    description="[管理者専用] 記録を使わず、公開日が新しい順に指定件数だけ音声抽出をテストする",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(count="テストする件数(1〜10)")
async def testaudio(interaction: discord.Interaction, count: int):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("❌ このコマンドを使う権限がありません。", ephemeral=True)
        return
    if count < 1 or count > 10:
        await interaction.response.send_message("❌ 件数は1〜10の範囲で指定してください。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, SPARK_API)
        except Exception as e:
            await interaction.followup.send(format_error_for_discord("❌ API取得エラー", e), ephemeral=True)
            return

        candidates = []
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            if not is_track_active(value):
                continue
            active_date_raw = value.get("_activeDate", "")
            candidates.append((active_date_raw, key, value))

        candidates.sort(key=lambda x: x[0], reverse=True)
        targets = candidates[:count]

        if not targets:
            await interaction.followup.send("公開済みの曲が見つかりませんでした。", ephemeral=True)
            return

        results_lines = []
        image_paths = []
        sem = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)

        for active_date_raw, key, value in targets:
            track_data = value.get("track", {})
            title = track_data.get("tt", key)
            artist = track_data.get("an", "?")
            date_str = active_date_raw[:10] if active_date_raw else "?"

            image_path = None
            try:
                image_path = await create_track_image(session, sem, key, track_data)
                image_paths.append(image_path)
            except Exception as e:
                print(f"画像生成エラー ({key}): {e}")

            try:
                result = await extract_audio_and_video(session, track_data, image_path=image_path)
            except Exception as e:
                results_lines.append(f"❌ {date_str} | {artist} - {title} : {e}")
                continue
            if result:
                ogg_path, video_path = result
                results_lines.append(f"✅ {date_str} | {artist} - {title} -> {ogg_path.parent}")
            else:
                results_lines.append(f"⚠️ {date_str} | {artist} - {title} : qi/pidが見つかりませんでした")

        summary = "\n".join(results_lines)
        files = [discord.File(p) for p in image_paths]
        await interaction.followup.send(
            f"テスト結果({len(targets)}件):\n{summary}",
            files=files if files else discord.utils.MISSING,
            ephemeral=True,
        )

# ================= 音源手動アップロードコマンド =================

@bot.tree.command(
    name="uploadjam",
    description="[管理者専用] 未送信の音源/動画ファイルをDiscordへ手動で送信する",
    guild=discord.Object(id=GUILD_ID),
)
async def uploadjam(interaction: discord.Interaction):
    if interaction.user.id != int(MENTION_ID):
        await interaction.response.send_message("❌ このコマンドを使う権限がありません。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        try:
            await scan_and_upload_audio(session)
        except Exception as e:
            await interaction.followup.send(format_error_for_discord("❌ 送信中にエラーが発生しました", e), ephemeral=True)
            return
    await interaction.followup.send("✅ 音源フォルダーのスキャン・送信が完了しました。", ephemeral=True)

# ================= メッセージ削除コマンド =================

@bot.tree.command(name="purge", description="チャンネル内のメッセージを削除する（特定ユーザー専用）", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    count="削除するメッセージ数（1〜100）",
    target="指定したユーザーのメッセージのみ削除（省略すると全員対象）"
)
async def purge(interaction: discord.Interaction, count: int, target: discord.Member = None):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("❌ このコマンドを使う権限がありません。", ephemeral=True)
        return
    if count < 1 or count > 100:
        await interaction.response.send_message("❌ 削除件数は1〜100の範囲で指定してください。", ephemeral=True)
        return
    if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("❌ Botに「メッセージの管理」権限がありません。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    def check(msg):
        if target is None:
            return True
        return msg.author.id == target.id

    deleted = await interaction.channel.purge(limit=count, check=check)

    if target:
        result_msg = f"✅ {target.display_name} のメッセージを {len(deleted)} 件削除しました。"
    else:
        result_msg = f"✅ {len(deleted)} 件のメッセージを削除しました。"

    await interaction.followup.send(result_msg, ephemeral=True)

# ================= START =================

bot.run(TOKEN)