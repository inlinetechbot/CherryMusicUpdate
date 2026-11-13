# Copyright (c) 2025 OpusMusic
# Licensed under the MIT License.
# This file is part of OpusMusic
#
#  Feature: Preloaded Songs
#  Player pe "Preloaded" button -> playlist menu (thumbnail + heading + buttons).
#  Playlist chunne par 50 songs ki list shuffle hoti hai aur bot random order me
#  nonstop play karta hai (list khatam ho to dobara shuffle).
#
#  Song lists:  opus/locales/playlists/<name>.json
#     {"name": "English", "songs": [{"title": "...", "artist": "...", "url": ""}]}
#     ("url" optional - dene par search ki jagah wahi YouTube link play hoga)

import asyncio
import json
import random
import types as _types
from pathlib import Path

from pyrogram import enums, filters, types

from opus import anon, app, config, db, lang, logger, queue, yt
from opus.helpers import can_manage_vc, rich

# ── Settings (yahin se badlo) ────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent.parent
PRELOAD_THUMB = str(BASE / "assets" / "preload.png")  # image path ya URL
PLAYLIST_DIR = BASE / "locales" / "playlists"
TITLE = "Preloaded Songs"
TEXT = (
    "𝘋𝘪𝘴𝘤𝘰𝘷𝘦𝘳 𝘠𝘰𝘶𝘳 𝘧𝘢𝘷𝘰𝘳𝘪𝘵𝘦 𝘵𝘳𝘢𝘤𝘬𝘴 𝘧𝘳𝘰𝘮 𝘰𝘶𝘳 𝘦𝘹𝘤𝘭𝘶𝘴𝘪𝘷𝘦 𝘱𝘳𝘦𝘭𝘰𝘢𝘥𝘦𝘥 𝘮𝘶𝘴𝘪𝘤 𝘭𝘪𝘣𝘳𝘢𝘳𝘺.\n"
    "<blockquote expandable>"
    "<b>𝘗𝘭𝘦𝘢𝘴𝘦 𝘴𝘦𝘭𝘦𝘤𝘵 𝘢𝘯𝘺 𝘰𝘯𝘦 𝘱𝘭𝘢𝘺𝘭𝘪𝘴𝘵 𝘢𝘯𝘥 𝘦𝘯𝘫𝘰𝘺 𝘯𝘰𝘯𝘴𝘵𝘰𝘱 𝘴𝘰𝘯𝘨</b>\n"
    "<b>✤ 𝘏𝘢𝘯𝘥𝘱𝘪𝘤𝘬𝘦𝘥 𝘚𝘰𝘯𝘨 𝘊𝘰𝘭𝘭𝘦𝘤𝘵𝘪𝘰𝘯</b>\n"
    "<b>✤ 𝘔𝘶𝘭𝘵𝘪𝘱𝘭𝘦 𝘔𝘶𝘴𝘪𝘤 𝘊𝘢𝘵𝘦𝘨𝘰𝘳𝘪𝘦𝘴</b>\n"
    "<b>✤ 𝘊𝘰𝘯𝘵𝘪𝘯𝘶𝘰𝘶𝘴 𝘗𝘭𝘢𝘺𝘭𝘪𝘴𝘵 𝘗𝘭𝘢𝘺𝘣𝘢𝘤𝘬</b>"
    "</blockquote>"
)

# (button ka naam, json file ka naam)
PAGE_1 = [
    ("𝐎𝗐𝗇𝖾𝗋 𝐏𝗅𝖺𝗒𝗅𝗂𝗌𝗍", "owner_playlist"),
    ("𝐎𝗐𝗇𝖾𝗋 𝐏𝗅𝖺𝗒𝗅𝗂𝗌𝗍 2", "owner_playlist_2"),
    ("𝐄𝗇𝗀𝗅𝗂𝗌 𝐒𝗈𝗇𝗀𝗌", "english"),
    ("𝐁𝗈𝗅𝗅𝗒𝗐𝗈𝗈𝖽 𝐇𝗂𝗍𝗌", "bollywood_hits"),
    ("90 𝐇𝗂𝗍𝗌", "hits_90s"),
    ("𝐏𝖺𝗋𝗍𝗒 𝐒𝗈𝗇𝗀𝗌", "party_songs"),
]
PAGE_2 = [
    ("𝐒𝖺𝖽 𝐒𝗈𝗇𝗀𝗌", "sad_songs"),
    ("𝐋𝗈𝗏𝖾 𝐒𝗈𝗇𝗀𝗌", "love_songs"),
    ("𝐁𝗁𝗈𝗃𝗉𝗎𝗋𝗂 𝐒𝗈𝗇𝗀𝗌", "bhojpuri"),
    ("𝐓𝗈𝗉𝐂𝗈𝗏𝖾𝗋 𝐒𝗈𝗇𝗀𝗌", "top_cover_songs"),
]
LABELS = dict((key, name) for name, key in PAGE_1 + PAGE_2)

S = enums.ButtonStyle
_state: dict[int, dict] = {}  # chat_id -> {"name", "pool", "i", "user"}
_cache: dict[str, list] = {}


# ── Keyboard ─────────────────────────────────────────────────────────────────

def _btn(text: str, data: str, style=S.PRIMARY) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data, style=style)


def _menu(page: int = 1) -> types.InlineKeyboardMarkup:
    items = PAGE_1 if page == 1 else PAGE_2
    rows = [
        [_btn(name, f"preload play {key}") for name, key in items[i:i + 2]]
        for i in range(0, len(items), 2)
    ]
    if page == 1:
        rows.append([_btn("Back", "preload back", S.DANGER),
                     _btn("Next", "preload page 2", S.SUCCESS)])
    else:
        rows.append([_btn("Back", "preload page 1", S.DANGER)])
    return types.InlineKeyboardMarkup(rows)


# ── Song lists ───────────────────────────────────────────────────────────────

def load_playlist(key: str) -> list:
    if key in _cache:
        return _cache[key]
    try:
        data = json.loads((PLAYLIST_DIR / f"{key}.json").read_text(encoding="utf-8"))
        songs = [s for s in data.get("songs", []) if s.get("title") or s.get("url")]
    except Exception as e:
        logger.warning(f"[Preload] cannot read playlist {key}: {e}")
        songs = []
    _cache[key] = songs
    return songs


async def _next_track(chat_id: int):
    """Pool me se agla gaana search karke Track banata hai (None = nahi mila)."""
    st = _state.get(chat_id)
    if not st:
        return None
    for _ in range(10):  # search fail ho to agla gaana try karo
        if st["i"] >= len(st["pool"]):
            random.shuffle(st["pool"])  # list khatam -> dobara shuffle (nonstop)
            st["i"] = 0
        song = st["pool"][st["i"]]
        st["i"] += 1
        query = song.get("url") or f'{song.get("title", "")} {song.get("artist", "")}'.strip()
        try:
            track = await yt.search(query, 0, video=False)
        except Exception as e:
            logger.warning(f"[Preload] search error: {e}")
            track = None
        if not track:
            continue
        if track.duration_sec and track.duration_sec > config.DURATION_LIMIT:
            continue
        track.user = st["user"]
        return track
    return None


async def _start(chat_id: int, msg: types.Message) -> bool:
    """Chuni hui list ka pehla gaana play karo (jo chal raha hai uski jagah)."""
    track = await _next_track(chat_id)
    if not track:
        return False
    for old in queue.get_queue(chat_id):
        if old.file_path:
            asyncio.create_task(anon._schedule_cleanup(old.file_path))
    queue.clear(chat_id)
    queue.add(chat_id, track)
    if not track.file_path:
        track.file_path = await yt.download(track.id, video=False, title=track.title, stream=True)
    if not track.file_path:
        queue.clear(chat_id)
        return False
    track.message_id = msg.id
    await anon.play_media(chat_id, msg, track)
    return True


# ── Nonstop: queue khatam hote hi agla random gaana ──────────────────────────

_peeking: set = set()


async def _peek_next(chat_id: int) -> None:
    """Gaana khatam hone se pehle agla random gaana queue me daal ke prefetch karo."""
    if chat_id not in _state or chat_id in _peeking:
        return
    if queue.get_next(chat_id, check=True) is not None or await db.get_loop(chat_id):
        return
    _peeking.add(chat_id)
    try:
        track = await _next_track(chat_id)
        if (
            track
            and chat_id in _state
            and queue.get_next(chat_id, check=True) is None
        ):
            queue.add(chat_id, track)
            yt.prefetch(track)
    except Exception as e:
        logger.warning(f"[Preload] peek error in {chat_id}: {e}")
    finally:
        _peeking.discard(chat_id)


yt.prepare_hooks.append(_peek_next)


_orig_play_next = anon.play_next.__func__


async def _patched_play_next(self, chat_id: int) -> None:
    if (
        chat_id in _state
        and queue.get_next(chat_id, check=True) is None
        and not await db.get_loop(chat_id)
    ):
        track = await _next_track(chat_id)
        if track:
            queue.add(chat_id, track)
    await _orig_play_next(self, chat_id)


anon.play_next = _types.MethodType(_patched_play_next, anon)

_orig_stop = anon.stop.__func__


async def _patched_stop(self, chat_id: int) -> None:
    _state.pop(chat_id, None)  # /stop pe preloaded mode band
    await _orig_stop(self, chat_id)


anon.stop = _types.MethodType(_patched_stop, anon)


# ── Callbacks ────────────────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^preload ") & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _preload_cb(_, query: types.CallbackQuery):
    args = query.data.split()
    action = args[1]
    chat_id = query.message.chat.id

    if action == "open":
        if not await db.get_call(chat_id):
            return await query.answer(query.lang["not_playing"], show_alert=True)
        if not await rich.swap_to_menu(chat_id, PRELOAD_THUMB, TITLE, TEXT, _menu(1)):
            return await query.answer("Preloaded menu abhi available nahi hai.", show_alert=True)
        return await query.answer()

    if action == "page":
        page = 2 if len(args) > 2 and args[2] == "2" else 1
        if not await rich.edit_menu(query, _menu(page)):
            return await query.answer(
                "Menu expire ho gaya. Player se Preloaded dobara kholo.", show_alert=True
            )
        return await query.answer()

    if action == "back":
        if not await rich.restore_player(chat_id):
            try:
                await query.message.delete()
            except Exception:
                pass
        return await query.answer()

    if action == "play" and len(args) > 2:
        key = args[2]
        songs = list(load_playlist(key))
        if not songs:
            return await query.answer("Is list me abhi songs nahi hain.", show_alert=True)
        await query.answer(f"{LABELS.get(key, key)} shuru ho rahi hai...")
        random.shuffle(songs)
        _state[chat_id] = {
            "name": LABELS.get(key, key),
            "pool": songs,
            "i": 0,
            "user": query.from_user.mention,
        }
        try:
            ok = await _start(chat_id, query.message)
        except Exception as e:
            logger.error(f"[Preload] start error in {chat_id}: {e}")
            ok = False
        if not ok:
            _state.pop(chat_id, None)
            await app.send_message(chat_id, "Gaana load nahi hua, doosri list try karo.")
