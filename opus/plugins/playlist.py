# ╔══════════════════════════════════════════════╗
# ║             OpusMusic Bot                  ║
# ║      Advanced Telegram Music System         ║
# ╚══════════════════════════════════════════════╝
#
#  Feature: Saved Playlists
#  /playlist save <name>   → save current queue
#  /playlist list          → show all saved playlists
#  /playlist play <name>   → load a saved playlist
#  /playlist delete <name> → delete a saved playlist
#  /playlist clear         → delete all playlists
#
#  Powered by OpusMusic
#

from pyrogram import filters, types

from opus import app, config, db, lang, queue, yt
from opus.helpers import Track
from opus.helpers._dataclass import Track


# ── MongoDB helpers ──────────────────────────────────────────────────────────

async def _get_playlists(chat_id: int) -> dict:
    doc = await db.db.playlists.find_one({"_id": chat_id})
    return doc.get("playlists", {}) if doc else {}


async def _save_playlists(chat_id: int, playlists: dict) -> None:
    await db.db.playlists.update_one(
        {"_id": chat_id},
        {"$set": {"playlists": playlists}},
        upsert=True,
    )


# ── Command handler ──────────────────────────────────────────────────────────

@app.on_message(
    filters.command(["playlist", "pl"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
async def playlist_cmd(_, m: types.Message):

    args = m.command[1:]

    if not args:
        return await m.reply_text(m.lang["playlist_usage"])

    sub = args[0].lower()

    if sub == "list":
        playlists = await _get_playlists(m.chat.id)
        if not playlists:
            return await m.reply_text(m.lang["playlist_none"])
        text = m.lang["playlist_list_header"]
        for i, (name, tracks) in enumerate(playlists.items(), 1):
            text += m.lang["playlist_list_item"].format(i, name, len(tracks))
        return await m.reply_text(text)

    if sub == "save":
        if len(args) < 2:
            return await m.reply_text(m.lang["playlist_save_usage"])
        name = " ".join(args[1:])[:32].strip()
        if not name:
            return await m.reply_text(m.lang["playlist_save_usage"])
        current_queue = queue.get_queue(m.chat.id)
        if not current_queue:
            return await m.reply_text(m.lang["playlist_queue_empty"])
        playlists = await _get_playlists(m.chat.id)
        if len(playlists) >= 10:
            return await m.reply_text(m.lang["playlist_limit"])
        serialized = [
            {
                "id": t.id,
                "title": t.title or "Unknown",
                "url": t.url or "",
                "duration": t.duration or "00:00",
                "duration_sec": t.duration_sec,
                "video": t.video,
            }
            for t in current_queue
        ]
        playlists[name] = serialized
        await _save_playlists(m.chat.id, playlists)
        return await m.reply_text(m.lang["playlist_saved"].format(name, len(serialized)))

    if sub == "play":
        if len(args) < 2:
            return await m.reply_text(m.lang["playlist_play_usage"])
        name = " ".join(args[1:]).strip()
        playlists = await _get_playlists(m.chat.id)
        if name not in playlists:
            return await m.reply_text(m.lang["playlist_not_found"].format(name))
        tracks_data = playlists[name]
        if not tracks_data:
            return await m.reply_text(m.lang["playlist_empty_saved"])
        sent = await m.reply_text(m.lang["playlist_loading"].format(name, len(tracks_data)))
        mention = m.from_user.mention
        queue.clear(m.chat.id)
        for td in tracks_data:
            track = Track(
                id=td["id"], title=td["title"], url=td["url"],
                duration=td["duration"], duration_sec=td["duration_sec"],
                video=td["video"], user=mention,
            )
            queue.add(m.chat.id, track)
        first_track = queue.get_current(m.chat.id)
        if not first_track.file_path:
            await sent.edit_text(m.lang["play_downloading"])
            first_track.file_path = await yt.download(
                first_track.id, video=first_track.video, title=first_track.title,
                stream=not first_track.video,
            )
        if not first_track.file_path:
            return await sent.edit_text(m.lang["error_no_file"].format(config.SUPPORT_CHAT))
        from opus import anon
        await anon.play_media(chat_id=m.chat.id, message=sent, media=first_track)
        await sent.edit_text(m.lang["playlist_loaded"].format(name, len(tracks_data)))
        return

    if sub in ("delete", "del", "remove"):
        if len(args) < 2:
            return await m.reply_text(m.lang["playlist_delete_usage"])
        name = " ".join(args[1:]).strip()
        playlists = await _get_playlists(m.chat.id)
        if name not in playlists:
            return await m.reply_text(m.lang["playlist_not_found"].format(name))
        del playlists[name]
        await _save_playlists(m.chat.id, playlists)
        return await m.reply_text(m.lang["playlist_deleted"].format(name))

    if sub == "clear":
        await _save_playlists(m.chat.id, {})
        return await m.reply_text(m.lang["playlist_cleared"])

    return await m.reply_text(m.lang["playlist_usage"])
  
