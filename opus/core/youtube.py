import os
import re
import time
import yt_dlp
import random
import asyncio
import aiohttp

from py_yt import Playlist, VideosSearch
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from typing import Union

from opus import logger
from opus.core.cache import TursoCache
from opus.helpers import Track, utils

try:
    from dotenv import load_dotenv  # pip install python-dotenv

    load_dotenv()
except ImportError:
    # dotenv na ho to bhi chalega, agar hosting platform se env set hai
    pass

# ── Multi-API Config ──────────────────────────────────────────
# Keys code mein nahi hain. .env / hosting ke Environment Variables mein
# OpusKey1, OpusKey2, OpusKey3 ... set karo. URL secret nahi hai isliye
# yahin rakha hai.
API_URLS = [
    "https://opusytapi.onrender.com",
    "https://opusytapi.onrender.com",
    "https://opusytapi.onrender.com",
]


def _load_apis():
    apis = []
    for i, url in enumerate(API_URLS, start=1):
        key = (os.getenv(f"OpusKey{i}") or "").strip().strip("\"'").strip()   # paste ke space/newline/quotes hatao
        if not key:
            continue
        # Key kabhi log nahi hoti, sirf lambai — API ke MASTER_KEY ki lambai se compare karo.
        logger.info(f"OpusKey{i} loaded ({len(key)} chars) -> {url}")
        apis.append(
            {
                "url": url,
                "name": f"OpusKey{i}",
                "key": key,
                "param": "api_key",
                "endpoint": "/download",
            }
        )
    return apis


APIS = _load_apis()
if not APIS:
    logger.warning("Koi OpusKey set nahi hai, sirf yt-dlp fallback chalega.")

FAIL_THRESHOLD = 3
BLOCK_DURATION = 10 * 60  # 10 minutes

# API naye gaane ko ~15-60s me ready karti hai (Render free pe slow ho sakti hai).
# Pehle total=60 tha, to slow response bhi "fail" ginti thi aur API block ho jaati thi.
API_TOTAL_TIMEOUT = int(os.getenv("API_TOTAL_TIMEOUT", "150"))
API_CONNECT_TIMEOUT = int(os.getenv("API_CONNECT_TIMEOUT", "15"))

# STREAM_PLAY=1: pehla gaana (jiske liye user wait kar raha hai) API ke progressive
# stream URL se seedha play hota hai — poori file download hone ka wait nahi.
# STREAM_PLAY=0 karte hi purana behaviour (file download -> play) wapas.
STREAM_PLAY = os.getenv("STREAM_PLAY", "1").strip().lower() in ("1", "true", "yes", "on")
# ─────────────────────────────────────────────────────────────

DOWNLOAD_DIR = "downloads"
MIN_MEDIA_BYTES = 10_000  # anything smaller is an error page/JSON, not a song

# API ka Content-Type -> file extension (audio). Video hamesha .mp4 naam se save hota hai.
_AUDIO_EXT_BY_TYPE = {
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
}


def is_stream_url(path) -> bool:
    """True agar file_path local file nahi balki API ka stream URL hai."""
    return isinstance(path, str) and path.startswith(("http://", "https://"))


def _ext_from_response(resp, video: bool) -> str:
    if video:
        return "mp4"
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return _AUDIO_EXT_BY_TYPE.get(ctype, "mp3")


def _looks_like_media(path: str) -> bool:
    """False for tiny files or JSON/HTML error bodies saved by mistake."""
    try:
        if os.path.getsize(path) < MIN_MEDIA_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(16).lstrip()
        return not head.startswith((b"{", b"<", b"["))
    except OSError:
        return False


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="

        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)"
        )

        self.cookies = []
        self.checked = False
        self.cookie_dir = "opus/cookies"
        self._api_index = 0
        self.cache = TursoCache()

        # Prefetch: agla gaana peeche background me ready karne ke liye.
        self._prefetching = {}   # video_id -> asyncio.Task
        # Plugins (autoplay / preloaded) yahan async hook(chat_id) register karte hain;
        # gaana khatam hone se pehle wo agla gaana pehle se chun ke prefetch karwa dete hain.
        self.prepare_hooks = []

        # Circuit Breaker — har API ka unique identifier key se banao
        self._fail_count = {self._api_id(api): 0 for api in APIS}
        self._blocked_until = {self._api_id(api): 0 for api in APIS}

    def _api_id(self, api: dict) -> str:
        """Same URL par bhi alag key ho to unique ID banao"""
        return f"{api['url']}::{api['key']}"

    def _api_label(self, api: dict) -> str:
        """Log ke liye safe label — key kabhi log mein nahi aati"""
        return f"{api['url']} #{APIS.index(api) + 1}"

    def get_cookies(self):
        """
        Cookie files load karta hai, lekin sirf wahi jo valid Netscape
        format mein hon. Corrupt/empty/wrong-format file ko yahin skip
        kar dete hain taaki yt-dlp ke andar cookiejar load/save crash
        na kare aur poora download() chain na toote.
        """
        if not self.checked:
            if os.path.exists(self.cookie_dir):
                for file in os.listdir(self.cookie_dir):
                    if file.endswith(".txt"):
                        path = f"{self.cookie_dir}/{file}"
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                first_line = f.readline()
                            if first_line.startswith("# Netscape") or first_line.startswith(
                                "# HTTP Cookie"
                            ):
                                self.cookies.append(path)
                            else:
                                logger.warning(
                                    f"Skipping invalid cookie file (bad header): {path}"
                                )
                        except Exception as e:
                            logger.warning(f"Cookie read error {path}: {e}")
            self.checked = True
        return random.choice(self.cookies) if self.cookies else None

    # ── Circuit Breaker ───────────────────────────────────────

    def _is_blocked(self, api: dict) -> bool:
        aid = self._api_id(api)
        now = time.time()

        if now < self._blocked_until[aid]:
            remaining = int((self._blocked_until[aid] - now) / 60) + 1
            logger.info(f"[{self._api_label(api)}] Blocked! ~{remaining}m remaining.")
            return True

        if self._fail_count[aid] >= FAIL_THRESHOLD:
            logger.info(f"[{self._api_label(api)}] Block khatam, phir se try karega.")
            self._fail_count[aid] = 0

        return False

    def _mark_fail(self, api: dict):
        aid = self._api_id(api)
        self._fail_count[aid] += 1
        logger.warning(
            f"[{self._api_label(api)}] Fail count: {self._fail_count[aid]}/{FAIL_THRESHOLD}"
        )

        if self._fail_count[aid] >= FAIL_THRESHOLD:
            self._blocked_until[aid] = time.time() + BLOCK_DURATION
            logger.warning(f"[{self._api_label(api)}] Blocked for 10 minutes!")

    def _mark_success(self, api: dict):
        aid = self._api_id(api)
        self._fail_count[aid] = 0
        self._blocked_until[aid] = 0
        logger.info(f"[{self._api_label(api)}] Success! Fail count reset.")

    # ── Core Methods ──────────────────────────────────────────

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def search(self, query: str, m_id: int, video=False):
        try:
            search = VideosSearch(query, limit=1)
            results = await search.next()
        except Exception as e:
            logger.error(f"Search Error: {e}")
            return None

        if results and results.get("result"):
            data = results["result"][0]
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=data.get("title")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1]["url"].split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )

    async def playlist(self, limit, user, url, video):
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                tracks.append(
                    Track(
                        id=data.get("id"),
                        channel_name=data.get("channel", {}).get("name"),
                        duration=data.get("duration"),
                        duration_sec=utils.to_seconds(data.get("duration")),
                        title=data.get("title")[:25],
                        thumbnail=data.get("thumbnails")[-1]["url"].split("?")[0],
                        url=data.get("link").split("&list=")[0],
                        user=user,
                        view_count="",
                        video=video,
                    )
                )
        except Exception as e:
            logger.error(f"Playlist Error: {e}")
        return tracks

    async def url(self, message_1: Message):
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)

        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
        return None

    # ── Download Methods ──────────────────────────────────────

    def _ordered_apis(self):
        """Unblocked APIs, round-robin order. Khaali list = sab blocked/missing."""
        available = [api for api in APIS if not self._is_blocked(api)]
        if not available:
            return []
        start = self._api_index % len(available)
        self._api_index += 1
        return available[start:] + available[:start]

    async def _try_single_api(self, api: dict, video_id: str, video: bool):
        part = None
        try:
            youtube_url = self.base + video_id
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)

            params = {
                "url": youtube_url,
                "type": "video" if video else "audio",
            }
            if api["key"] and api["param"]:
                params[api["param"]] = api["key"]

            started = time.monotonic()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{api['url']}{api['endpoint']}",
                    params=params,
                    timeout=aiohttp.ClientTimeout(
                        total=API_TOTAL_TIMEOUT, connect=API_CONNECT_TIMEOUT
                    ),
                ) as resp:

                    if resp.status != 200:
                        logger.warning(
                            f"[{self._api_label(api)}] Status: {resp.status}"
                        )
                        if resp.status == 401:
                            logger.error(
                                f"[{self._api_label(api)}] API key reject hui (401): "
                                f"{api['name']} ko API ke MASTER_KEY "
                                f"(ya valid key) se match karo."
                            )
                        self._mark_fail(api)
                        return None

                    # Extension API ke Content-Type se (m4a / mp3). File pehle
                    # `.part` me likhi jaati hai taaki adhoori file kabhi
                    # "complete" na dikhe (cache/exists checks).
                    ext = _ext_from_response(resp, video)
                    filename = f"{DOWNLOAD_DIR}/{video_id}.{ext}"
                    part = filename + ".part"

                    with open(part, "wb") as f:
                        async for chunk in resp.content.iter_chunked(131072):
                            f.write(chunk)

            if os.path.exists(part) and _looks_like_media(part):
                os.replace(part, filename)
                part = None
                self._mark_success(api)
                logger.info(
                    f"[{self._api_label(api)}] Download success ✓ "
                    f"({video_id}.{ext}, {time.monotonic() - started:.1f}s)"
                )
                return filename
            else:
                logger.warning(
                    f"[{self._api_label(api)}] Response is not a media file "
                    f"(empty/JSON/error page)!"
                )
                self._mark_fail(api)

        except Exception as e:
            # NOTE: aiohttp error mein kabhi kabhi poora URL (key ke saath)
            # aa jata hai, isliye error text se key hata ke log karte hain.
            err = str(e).replace(api["key"], "***")
            logger.warning(f"[{self._api_label(api)}] Error: {err}")
            self._mark_fail(api)
        finally:
            if part:
                try:
                    os.remove(part)
                except OSError:
                    pass

        return None

    async def api_download(self, video_id: str, video: bool = False):
        ordered = self._ordered_apis()

        if not ordered:
            logger.error("Saari APIs blocked/missing! yt-dlp fallback.")
            return None

        for api in ordered:
            logger.info(
                f"Trying: {self._api_label(api)} "
                f"(fails: {self._fail_count[self._api_id(api)]}/{FAIL_THRESHOLD})"
            )
            result = await self._try_single_api(api, video_id, video)
            if result:
                return result

        return None

    async def stream_url(self, video_id: str):
        """
        Progressive play ke liye: API se sirf download token lo (~0.5s,
        quota yahan charge nahi hota) aur `/v1/stream/{id}?token=...` URL
        wapas do. ffmpeg/PyTgCalls ye URL seedha padh leta hai aur API ke
        pehle bytes aate hi play shuru ho jaata hai.
        Fail ho to None — caller normal file download pe chala jata hai.
        """
        for api in self._ordered_apis():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{api['url']}/v1/download",
                        params={"url": self.base + video_id, "type": "audio"},
                        headers={"X-API-Key": api["key"]},
                        timeout=aiohttp.ClientTimeout(
                            total=20, connect=API_CONNECT_TIMEOUT
                        ),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"[{self._api_label(api)}] Token status: {resp.status}"
                            )
                            if resp.status == 401:
                                logger.error(
                                    f"[{self._api_label(api)}] API key reject hui (401): "
                                    f"{api['name']} ko API ke MASTER_KEY "
                                    f"(ya valid key) se match karo."
                                )
                            self._mark_fail(api)
                            continue
                        data = await resp.json()
                token = data.get("download_token")
                if token:
                    return f"{api['url']}/v1/stream/{video_id}?token={token}"
                self._mark_fail(api)
            except Exception as e:
                err = str(e).replace(api["key"], "***")
                logger.warning(f"[{self._api_label(api)}] Token error: {err}")
                self._mark_fail(api)
        return None

    async def ytdlp_download(self, video_id: str, video: bool = False):
        url = self.base + video_id
        cookie = self.get_cookies()

        opts = {
            "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
            "quiet": True,
            "nocheckcertificate": True,
        }

        if cookie:
            opts["cookiefile"] = cookie

        if video:
            opts["format"] = "bestvideo+bestaudio"
        else:
            opts["format"] = "bestaudio"

        def run():
            # 'with' block poora try ke andar hai taaki extract_info ho ya
            # cleanup/save_cookies, har exception yahin pakda jaye aur
            # poora download() crash na kare.
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info)
            except Exception as e:
                logger.error(f"yt-dlp Error: {e}")
                return None

        return await asyncio.to_thread(run)

    def _local_file(self, video_id: str, video: bool = False):
        """Pehle se utri hui (poori) audio file, agar disk pe hai. Video ke liye None."""
        if video:
            return None
        for ext in ("m4a", "mp3", "webm", "opus", "ogg"):
            path = f"{DOWNLOAD_DIR}/{video_id}.{ext}"
            if os.path.exists(path) and _looks_like_media(path):
                return path
        return None

    def prefetch(self, track) -> None:
        """
        Track ki file background me ready karo (non-blocking). Cache/disk me ho to
        wahi use hoti hai, warna API se download. Ho jane par `track.file_path`
        khud set ho jata hai. Same gaane ke liye dobara call karna safe hai.
        """
        vid = getattr(track, "id", None)
        if not vid or getattr(track, "file_path", None) or vid in self._prefetching:
            return

        async def run():
            try:
                path = self._local_file(vid, track.video) or await self._fetch(
                    vid, track.video, track.title, stream=False
                )
                if path and not track.file_path:
                    track.file_path = path
                    logger.info(f"[Prefetch] Ready: {vid}")
            except Exception as e:
                logger.warning(f"[Prefetch] Failed {vid}: {e}")
            finally:
                self._prefetching.pop(vid, None)

        self._prefetching[vid] = asyncio.create_task(run())

    async def download(
        self,
        video_id: str,
        video: bool = False,
        title: str = None,
        stream: bool = False,
    ):
        """
        Public entry. Agar yahi gaana prefetch ho raha hai to uska wait karta hai
        (dobara API call nahi), aur disk pe poori file ho to wahi return karta hai.
        """
        task = self._prefetching.get(video_id)
        if task and not task.done():
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        local = self._local_file(video_id, video)
        if local:
            logger.info(f"[Download] Local file reuse: {local}")
            return local
        return await self._fetch(video_id, video, title, stream)

    async def _fetch(
        self,
        video_id: str,
        video: bool = False,
        title: str = None,
        stream: bool = False,
    ):
        """
        Local file path return karta hai. `stream=True` (sirf audio) par,
        agar Turso cache me gaana nahi hai, to file ki jagah API ka
        progressive stream URL return hota hai (turant play ke liye).
        Queue/preload/loop wale callers `stream` na dein — unhe local file
        chahiye (URL ka token 5 min me expire ho jata hai).
        """
        # ── Step 1: Turso cache check ────────────────────────────
        # Pehle dekho ki yeh song pehle se cache DB mein hai ya nahi.
        # Mil gaya to seedha disk pe likh ke return — na koi API call,
        # na yt-dlp. Isse daily API limit repeat songs par consume
        # nahi hoti.
        cached = await self.cache.get(video_id)
        if cached and len(cached[1]) < MIN_MEDIA_BYTES:
            logger.warning(f"[TursoCache] Ignoring broken cache entry: {video_id}")
            cached = None
        if cached:
            ext, data = cached
            try:
                return await self.cache.write_to_disk(
                    video_id, ext, data, DOWNLOAD_DIR
                )
            except Exception as e:
                logger.warning(f"[TursoCache] Disk write error: {e}")
                # disk write fail ho to bhi normal chain se download
                # try karte hain, playback rukna nahi chahiye.

        # ── Step 1b: progressive stream URL (sirf audio, opt-in) ─
        if stream and not video and STREAM_PLAY and APIS:
            url = await self.stream_url(video_id)
            if url:
                logger.info(f"[Stream] Progressive URL ready: {video_id}")
                return url
            logger.warning("[Stream] Token nahi mila, normal download pe ja rahe hain")

        # ── Step 2: normal fallback chain ────────────────────────
        file = await self.api_download(video_id, video)

        if not file:
            logger.warning("All APIs failed/blocked, falling back to yt-dlp")
            file = await self.ytdlp_download(video_id, video)

        # ── Step 3: file cache mein save karo (fire-and-forget) ──
        # `await` nahi karte — save background task mein chalta hai
        # aur `file` turant return ho jaati hai, taaki playback ko
        # Turso ke DB-write ka wait na karna pade.
        if file:
            ext = file.rsplit(".", 1)[-1] if "." in file else ("mp4" if video else "mp3")
            asyncio.create_task(self._save_to_cache(video_id, file, ext))

        return file

    async def _save_to_cache(self, video_id: str, filepath: str, ext: str) -> None:
        try:
            await self.cache.save(video_id, filepath, ext)
        except Exception as e:
            logger.warning(f"[TursoCache] Background save error: {e}")
