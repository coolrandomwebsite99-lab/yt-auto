import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont

TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
PUBLISH_HOUR = int(os.environ.get("PUBLISH_HOUR", "12"))
GEO = os.environ.get("TRENDS_GEO", "US")
VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

W, H = 1080, 1920
OUT = Path("build")
HISTORY = Path("history.json")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Trending tragedies make for tasteless auto-generated videos, so skip them.
SENSITIVE = re.compile(
    r"\b(died|dies|dead|death|killed|kill|shooting|shot|murder|crash|obituary|funeral|"
    r"suicide|attack|victim|missing|arrest|war|bomb|abuse|overdose|passed away)\b",
    re.I,
)
NS = {"ht": "https://trends.google.com/trending/rss"}


def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []


def fetch_trends():
    r = requests.get(f"https://trends.google.com/trending/rss?geo={GEO}", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    topics = []
    for item in ET.fromstring(r.content).iter("item"):
        news = [
            {
                "title": (n.findtext("ht:news_item_title", "", NS) or "").strip(),
                "snippet": (n.findtext("ht:news_item_snippet", "", NS) or "").strip(),
                "source": (n.findtext("ht:news_item_source", "", NS) or "").strip(),
            }
            for n in item.findall("ht:news_item", NS)
        ]
        traffic = item.findtext("ht:approx_traffic", "0", NS)
        topics.append({
            "topic": item.findtext("title", "").strip(),
            "traffic": int(re.sub(r"\D", "", traffic) or 0),
            "news": news,
        })
    return sorted(topics, key=lambda t: t["traffic"], reverse=True)


def pick_topic(topics, used):
    for t in topics:
        blob = t["topic"] + " " + " ".join(n["title"] + " " + n["snippet"] for n in t["news"])
        if t["topic"].lower() in used or SENSITIVE.search(blob) or not t["news"]:
            continue
        return t
    sys.exit("No suitable trending topic today.")


def write_script_gemini(t, key):
    news = "\n".join(f"- {n['title']} ({n['source']}): {n['snippet']}" for n in t["news"])
    prompt = f"""You write scripts for a fun, upbeat YouTube Shorts channel that explains what's trending today.
Today's trending topic: "{t['topic']}"
News headlines about it (the ONLY facts you may use; do not invent details):
{news}

Return JSON with keys:
"title": catchy YouTube title under 70 characters, end with " #Shorts"
"description": 2-3 sentence description, then 3-5 hashtags
"tags": list of 5-10 short tags
"lines": list of 7-10 short spoken sentences (max 18 words each) totaling about 40 seconds. Start with a hook. End with "Follow for what's trending tomorrow!"
"""
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        params={"key": key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}},
        timeout=90,
    )
    r.raise_for_status()
    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    assert data["lines"] and data["title"]
    return data


def write_script_fallback(t):
    lines = [f"Everyone is searching for {t['topic']} today. Here's why."]
    for n in t["news"][:3]:
        lines.append(f"{n['source'] or 'One report'} says: {n['title']}.")
    lines.append("Follow for what's trending tomorrow!")
    return {
        "title": f"Why is {t['topic']} trending? #Shorts"[:100],
        "description": f"Why {t['topic']} is trending today.\n\n#trending #news #shorts",
        "tags": [t["topic"], "trending", "news", "shorts"],
        "lines": lines,
    }


def font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default(size=size)


def draw_frame(topic, line, idx, path):
    hue_sets = [((88, 24, 140), (230, 50, 110)), ((10, 60, 160), (0, 180, 200)),
                ((200, 60, 20), (250, 170, 30)), ((20, 120, 70), (140, 210, 60))]
    top, bottom = hue_sets[idx % len(hue_sets)]
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(top, bottom)))

    d.rounded_rectangle([60, 140, W - 60, 340], radius=40, fill=(0, 0, 0))
    label = textwrap.shorten(topic.upper(), 30, placeholder="…")
    size = 64
    while size > 32 and d.textlength(label, font=font(size)) > W - 180:
        size -= 4
    tf = font(size)
    d.text((W / 2, 200), "TRENDING TODAY", font=font(40), fill=(255, 210, 0), anchor="mm")
    d.text((W / 2, 280), label, font=tf, fill="white", anchor="mm")

    bf = font(80)
    wrapped = textwrap.fill(line, 20)
    d.multiline_text((W / 2, H / 2 + 80), wrapped, font=bf, fill="white", anchor="mm",
                     align="center", spacing=24, stroke_width=6, stroke_fill="black")
    img.save(path)


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)])
    return float(out)


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate="+8%").save(str(path))


def build_video(topic, lines):
    OUT.mkdir(exist_ok=True)
    segs = []
    for i, line in enumerate(lines):
        png, mp3, mp4 = OUT / f"{i}.png", OUT / f"{i}.mp3", OUT / f"{i}.mp4"
        draw_frame(topic, line, i, png)
        asyncio.run(tts(line, mp3))
        dur = duration(mp3) + 0.25
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png), "-i", str(mp3),
            "-t", f"{dur:.2f}", "-r", "30", "-c:v", "libx264", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-af", "apad", str(mp4)], check=True)
        segs.append(mp4)
    listing = OUT / "list.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segs))
    final = OUT / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(final)], check=True)
    return final


def publish_time():
    tz = ZoneInfo(TIMEZONE)
    now = dt.datetime.now(tz)
    target = now.replace(hour=PUBLISH_HOUR, minute=0, second=0, microsecond=0)
    if target - now < dt.timedelta(minutes=15):
        return None
    return target.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def upload(path, meta):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    when = publish_time()
    status = {"selfDeclaredMadeForKids": False}
    if when:
        status.update(privacyStatus="private", publishAt=when)
    else:
        status["privacyStatus"] = "public"
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"][:4900],
            "tags": meta.get("tags", [])[:15],
            "categoryId": "24",
        },
        "status": status,
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print(f"Uploaded https://youtu.be/{resp['id']} (publish at: {when or 'now'})")
    return resp["id"]


def main():
    history = load_history()
    used = {h["topic"].lower() for h in history}
    t = pick_topic(fetch_trends(), used)
    print("Topic:", t["topic"])

    key = os.environ.get("GEMINI_API_KEY")
    meta = None
    if key:
        try:
            meta = write_script_gemini(t, key)
        except Exception as e:
            print("Gemini failed, using fallback script:", e)
    meta = meta or write_script_fallback(t)
    print(json.dumps(meta, indent=2))

    video = build_video(t["topic"], meta["lines"])
    print(f"Built {video} ({duration(video):.1f}s)")
    if DRY_RUN:
        return

    vid = upload(video, meta)
    history.append({"date": dt.date.today().isoformat(), "topic": t["topic"], "video_id": vid})
    HISTORY.write_text(json.dumps(history[-60:], indent=2))


if __name__ == "__main__":
    main()
