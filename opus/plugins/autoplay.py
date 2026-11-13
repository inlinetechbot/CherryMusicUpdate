# ╔══════════════════════════════════════════════╗
# ║             OpusMusic Bot                  ║
# ║      Advanced Telegram Music System         ║
# ╚══════════════════════════════════════════════╝
#
#  Feature: AutoPlay
#  Queue khatam hone pe automatically related song play kare
#
#  /autoplay on  → enable
#  /autoplay off → disable
#
#  Powered by OpusMusic
#

import os
import random
import re
import types as _types

from py_yt import VideosSearch
from pyrogram import filters, types

from opus import anon, app, config, db, lang, logger, queue, yt
from opus.helpers import can_manage_vc


# ── MongoDB helpers ──────────────────────────────────────────────────────────

_autoplay_cache: dict[int, bool] = {}


async def is_autoplay(chat_id: int) -> bool:
    if chat_id in _autoplay_cache:
        return _autoplay_cache[chat_id]
    doc = await db.db.autoplay.find_one({"_id": chat_id})
    state = bool(doc.get("enabled", False)) if doc else False
    _autoplay_cache[chat_id] = state
    return state


async def set_autoplay(chat_id: int, enabled: bool) -> None:
    _autoplay_cache[chat_id] = enabled
    if not enabled:
        _pending.pop(chat_id, None)
    await db.db.autoplay.update_one(
        {"_id": chat_id}, {"$set": {"enabled": enabled}}, upsert=True,
    )


# ── Track memory ─────────────────────────────────────────────────────────────

_last_track: dict[int, str] = {}
_pending: dict[int, object] = {}   # chat_id -> agla autoplay track (prefetch)
_peeking: set = set()
_used_ids: dict[int, set] = {}


def remember_track(chat_id: int, title: str, track_id: str = "") -> None:
    if title:
        _last_track[chat_id] = title
    if track_id:
        _used_ids.setdefault(chat_id, set()).add(track_id)


async def _find_autoplay_track(chat_id: int, title: str, m_id: int):
    """Related gaana dhundo (messages bheje bina). Track ya None."""
    used_ids = _used_ids.setdefault(chat_id, set())
    stop_words = {"the","a","an","is","in","on","and","or","of","to","tu","hai","ho","hoon","main","mein","ke","ka","ki","se","ne"}
    words = re.sub(r"[^\w\s]", "", title.lower()).split()
    keywords = [w for w in words if w not in stop_words and len(w) > 2]

    genre_queries = [
        "hindi romantic songs 2024",
        "best bollywood songs",
        "hindi hit songs playlist",
        "top indian songs 2024",
        "popular hindi songs",
    ]

    if keywords:
        kw = keywords[0]
        queries = [f"{kw} hindi songs", f"songs similar to {kw}", random.choice(genre_queries)]
    else:
        queries = genre_queries[:3]

    track = None

    for search_query in queries:
        try:
            search = VideosSearch(search_query, limit=15)
            results = (await search.next()).get("result", [])
        except Exception as e:
            logger.error(f"AutoPlay search error: {e}")
            continue

        random.shuffle(results)

        for video in results:
            vid_link = video.get("link", "")
            vid_id = vid_link.split("v=")[-1].split("&")[0]
            vid_title = video.get("title", "").lower().strip()
            duration_str = video.get("duration") or "0:00"

            try:
                parts = duration_str.split(":")
                dur_sec = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2]) if len(parts) == 3 else int(parts[0])*60 + int(parts[1])
            except Exception:
                dur_sec = 0

            if not vid_id or vid_id in used_ids:
                continue
            if dur_sec > config.DURATION_LIMIT or dur_sec < 60:
                continue
            if any(kw in vid_title for kw in keywords[:2]):
                continue

            track = await yt.search(vid_link, m_id, video=False)
            if track:
                break

        if track:
            break
    return track

async def try_autoplay(chat_id: int) -> bool:
    if not await is_autoplay(chat_id):
        return False

    title = _last_track.get(chat_id)
    if not title:
        return False

    _lang = await lang.get_lang(chat_id)
    used_ids = _used_ids.setdefault(chat_id, set())

    try:
        msg = await app.send_message(
            chat_id=chat_id,
            text=_lang["autoplay_searching"].format(title),
        )

        track = _pending.pop(chat_id, None)   # pehle se chuna + prefetch kiya hua
        if track and track.id in used_ids:
            track = None
        if track and track.file_path and not (
            track.file_path.startswith('http') or os.path.exists(track.file_path)
        ):
            track.file_path = None            # file cleanup ho chuki hai
        if not track:
            track = await _find_autoplay_track(chat_id, title, msg.id)

        if not track:
            await msg.edit_text(_lang["autoplay_not_found"])
            return False

        track.user = app.name
        queue.add(chat_id, track)
        remember_track(chat_id, track.title, track.id)

        if not track.file_path:
            await msg.edit_text(_lang["play_downloading"])
            track.file_path = await yt.download(track.id, video=False, title=track.title, stream=True)

        if not track.file_path:
            await msg.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            queue.clear(chat_id)
            return False

        track.message_id = msg.id
        await anon.play_media(chat_id=chat_id, message=msg, media=track)
        return True

    except Exception as e:
        logger.error(f"AutoPlay error in {chat_id}: {e}")
        return False


async def peek_autoplay(chat_id: int) -> None:
    """Gaana khatam hone se pehle agla autoplay gaana chun ke prefetch karo."""
    if chat_id in _pending or chat_id in _peeking:
        return
    if queue.get_next(chat_id, check=True) is not None:
        return
    if not await is_autoplay(chat_id):
        return
    current = queue.get_current(chat_id)
    title = getattr(current, "title", None)
    if not title:
        return
    _peeking.add(chat_id)
    try:
        track = await _find_autoplay_track(chat_id, title, 0)
        if track:
            _pending[chat_id] = track
            yt.prefetch(track)
    except Exception as e:
        logger.warning(f"[AutoPlay] peek error in {chat_id}: {e}")
    finally:
        _peeking.discard(chat_id)


yt.prepare_hooks.append(peek_autoplay)



# ── Patch anon.play_next ─────────────────────────────────────────────────────

_original_play_next = anon.play_next.__func__


async def _patched_play_next(self, chat_id: int) -> None:
    current = queue.get_current(chat_id)
    current_title = current.title if (current and hasattr(current, "title")) else None
    current_id = current.id if current else ""
    has_next = queue.get_next(chat_id, check=True) is not None

    await _original_play_next(self, chat_id)

    if current_title:
        remember_track(chat_id, current_title, current_id)

    if not has_next and not await db.get_call(chat_id):
        await try_autoplay(chat_id)


anon.play_next = _types.MethodType(_patched_play_next, anon)


# ── /autoplay command ─────────────────────────────────────────────────────────

@app.on_message(
    filters.command(["autoplay", "ap"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
@can_manage_vc
async def autoplay_cmd(_, m: types.Message):

    chat_id = m.chat.id

    if len(m.command) < 2:
        state = await is_autoplay(chat_id)
        key = "autoplay_on" if state else "autoplay_off"
        return await m.reply_text(m.lang["autoplay_status"].format(m.lang[key]))

    sub = m.command[1].lower()

    if sub in ("on", "enable", "1", "true"):
        await set_autoplay(chat_id, True)
        return await m.reply_text(m.lang["autoplay_enabled"])

    if sub in ("off", "disable", "0", "false"):
        await set_autoplay(chat_id, False)
        return await m.reply_text(m.lang["autoplay_disabled"])

    return await m.reply_text(m.lang["autoplay_usage"])
  
