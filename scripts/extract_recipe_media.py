#!/usr/bin/env python3
"""
scripts/extract_recipe_media.py

Zero-Auth Multimodal Recipe Media Extractor for TikTok, Instagram Reels, and YouTube Shorts.
Executes a 3-pass extraction:
  Pass 1: Metadata, uploader, description, and auto-subtitles extraction via yt-dlp + curl-cffi.
  Pass 2: Subtitle text cleaning and timestamp removal.
  Pass 3: Audio / keyframe fallback extraction (using ffmpeg with -nostdin -hide_banner -y).
"""

import os
import sys
import json
import re
import argparse
import subprocess
from typing import Dict, Any, List, Optional
from pathlib import Path

# Common culinary measurement units and tokens to detect presence of ingredients
CULINARY_MEASUREMENT_TOKENS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons",
    "tsp", "teaspoon", "teaspoons", "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds", "gram", "grams", "g", "kg",
    "ml", "pinch", "pinches", "clove", "cloves", "slice", "slices",
    "dash", "can", "cans", "stalk", "stalks", "bunch", "bunches"
}


def clean_subtitle_text(raw_text: str) -> str:
    """
    Cleans WebVTT or SRT subtitle content:
    - Removes WEBVTT header and cues
    - Removes timestamps (00:00:00.000 --> 00:00:00.000)
    - Strips inline formatting tags (<c>, <b>, <font>, etc.)
    - Deduplicates repeated adjacent lines from rolling captions
    """
    if not raw_text:
        return ""

    lines = raw_text.splitlines()
    cleaned_lines: List[str] = []

    # Regex for timestamp arrows and cue numbers
    time_regex = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}")
    tag_regex = re.compile(r"<[^>]+>")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE"):
            continue
        if line.isdigit():
            continue
        if time_regex.search(line):
            continue

        # Strip html/vtt tags
        clean_line = tag_regex.sub("", line).strip()
        if not clean_line:
            continue

        # Deduplicate consecutive duplicates from rolling subtitles
        if not cleaned_lines or cleaned_lines[-1] != clean_line:
            cleaned_lines.append(clean_line)

    return " ".join(cleaned_lines)


def count_culinary_tokens(text: str) -> int:
    """Counts occurrence of culinary measurement tokens in a string."""
    words = re.findall(r"\b[a-zA-Z]+\b", text.lower())
    return sum(1 for w in words if w in CULINARY_MEASUREMENT_TOKENS)


def extract_recipe(
    url: str,
    output_dir: str = "cache/media",
    force_audio: bool = False,
    force_keyframes: bool = False,
) -> Dict[str, Any]:
    """
    Zero-auth multimodal extraction pipeline for short-form video URLs.
    """
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------
    # PASS 1: Metadata & Subtitle Query (Zero-Auth)
    # ---------------------------------------------------------
    ydl_opts: Dict[str, Any] = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-Orig"],
        "quiet": True,
        "no_warnings": True,
    }

    # Attempt browser impersonation for zero-auth scraping
    try:
        ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception:
        pass

    info: Dict[str, Any] = {}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False) or {}
        except Exception as e:
            # Fallback without impersonate if target failed
            if "impersonate" in ydl_opts:
                ydl_opts.pop("impersonate")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_retry:
                    info = ydl_retry.extract_info(url, download=False) or {}
            else:
                raise e

    video_id = str(info.get("id") or "video")
    title = str(info.get("title") or "")
    description = str(info.get("description") or "")
    uploader = str(info.get("uploader") or info.get("channel") or info.get("creator") or "")
    canonical_url = str(info.get("webpage_url") or url)

    # Determine platform
    platform = "unknown"
    if "tiktok.com" in url.lower():
        platform = "tiktok"
    elif "instagram.com" in url.lower():
        platform = "instagram"
    elif "youtube.com" in url.lower() or "youtu.be" in url.lower():
        platform = "youtube"

    # ---------------------------------------------------------
    # PASS 2: Caption & Subtitle Parsing
    # ---------------------------------------------------------
    subtitles_raw = ""
    subtitles_dict = info.get("subtitles") or {}
    auto_subtitles_dict = info.get("automatic_captions") or {}

    # Check for subtitles in downloaded metadata
    all_subs = {**auto_subtitles_dict, **subtitles_dict}
    for lang in ["en", "en-US", "en-Orig"]:
        if lang in all_subs:
            formats = all_subs[lang]
            # Prefer vtt or json
            for fmt in formats:
                sub_url = fmt.get("url")
                if sub_url:
                    try:
                        import urllib.request
                        req = urllib.request.Request(
                            sub_url,
                            headers={"User-Agent": "Mozilla/5.0"}
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            subtitles_raw = resp.read().decode("utf-8", errors="ignore")
                        if subtitles_raw:
                            break
                    except Exception:
                        pass
        if subtitles_raw:
            break

    cleaned_subtitles = clean_subtitle_text(subtitles_raw)

    combined_text = f"{title} {description} {cleaned_subtitles}".strip()
    measurement_token_count = count_culinary_tokens(combined_text)
    has_sufficient_recipe_text = measurement_token_count >= 3

    # ---------------------------------------------------------
    # PASS 3: Fallback Audio & Keyframe Extraction
    # ---------------------------------------------------------
    audio_path: Optional[str] = None
    keyframe_paths: List[str] = []

    # If transcript/description is sparse or user forced audio/keyframes
    needs_audio = force_audio or (not has_sufficient_recipe_text)
    needs_keyframes = force_keyframes or (not has_sufficient_recipe_text)

    if needs_audio or needs_keyframes:
        media_subfolder = out_path / video_id
        media_subfolder.mkdir(parents=True, exist_ok=True)

        # Download audio stream via yt-dlp
        if needs_audio:
            target_audio = media_subfolder / f"{video_id}.mp3"
            if not target_audio.exists():
                audio_opts: Dict[str, Any] = {
                    "format": "bestaudio/best",
                    "outtmpl": str(media_subfolder / f"{video_id}.%(ext)s"),
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "quiet": True,
                    "no_warnings": True,
                }
                try:
                    with yt_dlp.YoutubeDL(audio_opts) as ydl_audio:
                        ydl_audio.download([url])
                    if target_audio.exists():
                        audio_path = str(target_audio)
                except Exception as ex:
                    # Non-fatal if audio download fails on restricted networks
                    pass
            else:
                audio_path = str(target_audio)

        # Download lightweight video to extract frames via ffmpeg
        if needs_keyframes:
            frames_dir = media_subfolder / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            existing_frames = list(frames_dir.glob("frame_*.jpg"))
            if existing_frames:
                keyframe_paths = [str(f) for f in sorted(existing_frames)]
            else:
                temp_video = media_subfolder / f"{video_id}_temp.mp4"
                video_opts: Dict[str, Any] = {
                    "format": "worst[ext=mp4]/worst",
                    "outtmpl": str(temp_video),
                    "quiet": True,
                    "no_warnings": True,
                }
                try:
                    with yt_dlp.YoutubeDL(video_opts) as ydl_vid:
                        ydl_vid.download([url])
                    if temp_video.exists():
                        # Extract frames at 0.5 fps (1 frame every 2s) with strict non-interactive ffmpeg flags
                        cmd = [
                            "ffmpeg",
                            "-nostdin",
                            "-hide_banner",
                            "-y",
                            "-i", str(temp_video),
                            "-vf", "fps=0.5,scale=640:-1",
                            "-frames:v", "15", # Bound extraction to max 15 frames
                            str(frames_dir / "frame_%03d.jpg")
                        ]
                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                        keyframe_paths = [str(f) for f in sorted(frames_dir.glob("frame_*.jpg"))]
                        # Remove temporary video to conserve disk
                        try:
                            temp_video.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass

    return {
        "video_id": video_id,
        "canonical_url": canonical_url,
        "platform": platform,
        "creator": uploader,
        "title": title,
        "raw_description": description,
        "subtitles_text": cleaned_subtitles,
        "culinary_token_count": measurement_token_count,
        "has_sufficient_recipe_text": has_sufficient_recipe_text,
        "audio_path": audio_path,
        "keyframe_paths": keyframe_paths,
    }


def main():
    parser = argparse.ArgumentParser(description="Zero-auth TikTok/Reel Recipe Extractor")
    parser.add_argument("url", help="URL of the TikTok, Instagram Reel, or YouTube Short")
    parser.add_argument("--output-dir", default="cache/media", help="Output directory for media cache")
    parser.add_argument("--force-audio", action="store_true", help="Force audio track extraction")
    parser.add_argument("--force-keyframes", action="store_true", help="Force keyframe extraction")
    parser.add_argument("--json", action="store_true", default=True, help="Output formatted JSON")

    args = parser.parse_args()

    try:
        result = extract_recipe(
            url=args.url,
            output_dir=args.output_dir,
            force_audio=args.force_audio,
            force_keyframes=args.force_keyframes,
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        sys.stderr.write(f"Error during extraction: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
