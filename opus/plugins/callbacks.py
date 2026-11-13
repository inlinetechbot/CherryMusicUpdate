# Copyright (c) 2025 OpusMusic
# Licensed under the MIT License.
# This file is part of OpusMusic


import logging
import re

from pyrogram import enums, errors, filters, types

from opus import anon, app, db, lang, queue, tg, yt
from opus.helpers import admin_check, buttons, can_manage_vc, rich


@app.on_callback_query(filters.regex("cancel_dl") & ~app.bl_users)
@lang.language()
async def cancel_dl(_, query: types.CallbackQuery):
    await query.answer()
    await tg.cancel(query)


@app.on_callback_query(filters.regex("controls") & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _controls(_, query: types.CallbackQuery):
    args = query.data.split()
    action, chat_id = args[1], int(args[2])
    qaction = len(args) == 4
    user = query.from_user.mention

    if not await db.get_call(chat_id):
        try:
            return await query.answer(query.lang["not_playing"], show_alert=True)
        except errors.QueryIdInvalid:
            try:
                await query.message.delete()
            except Exception:
                pass
            return

    if action == "status":
        return await query.answer()
    await query.answer(query.lang["processing"], show_alert=True)

    if action == "pause":
        if not await db.playing(chat_id):
            return await query.answer(
                query.lang["play_already_paused"], show_alert=True
            )
        await anon.pause(chat_id)
        if qaction:
            return await query.edit_message_reply_markup(
                reply_markup=buttons.queue_markup(chat_id, query.lang["paused"], False)
            )
        status = query.lang["paused"]
        reply = query.lang["play_paused"].format(user)

    elif action == "resume":
        if await db.playing(chat_id):
            return await query.answer(query.lang["play_not_paused"], show_alert=True)
        await anon.resume(chat_id)
        if qaction:
            return await query.edit_message_reply_markup(
                reply_markup=buttons.queue_markup(chat_id, query.lang["playing"], True)
            )
        reply = query.lang["play_resumed"].format(user)

    elif action == "skip":
        await anon.play_next(chat_id)
        status = query.lang["skipped"]
        reply = query.lang["play_skipped"].format(user)

    elif action == "force":
        pos, media = queue.check_item(chat_id, args[3])
        if not media or pos == -1:
            return await query.edit_message_text(query.lang["play_expired"])

        m_id = queue.get_current(chat_id).message_id
        queue.force_add(chat_id, media, remove=pos)
        try:
            await app.delete_messages(
                chat_id=chat_id, message_ids=[m_id, media.message_id], revoke=True
            )
            media.message_id = None
        except Exception:
            pass

        msg = await app.send_message(chat_id=chat_id, text=query.lang["play_next"])
        if not media.file_path:
            media.file_path = await yt.download(
                media.id, video=media.video, title=media.title, stream=not media.video
            )
        media.message_id = msg.id
        return await anon.play_media(chat_id, msg, media)

    elif action == "replay":
        media = queue.get_current(chat_id)
        media.user = user
        await anon.replay(chat_id)
        status = query.lang["replayed"]
        reply = query.lang["play_replayed"].format(user)

    elif action == "autoplay":

        from opus.plugins.autoplay import is_autoplay as _is_ap, set_autoplay as _set_ap
        _current = await _is_ap(chat_id)
        _new_state = not _current
        await _set_ap(chat_id, _new_state)
        _ap_text = "ᴀᴜᴛᴏᴘʟᴀʏ: ᴏɴ ✅" if _new_state else "ᴀᴜᴛᴏᴘʟᴀʏ: ᴏꜰꜰ ❌"
        await query.answer("✅ AutoPlay ON" if _new_state else "❌ AutoPlay OFF", show_alert=True)
        try:
            if not await rich.update(chat_id, message_id=query.message.id):
                await query.edit_message_reply_markup(
                    reply_markup=buttons.controls(chat_id, autoplay_on=_new_state, autoplay_text=_ap_text)
                )
        except Exception:
            pass
        return

    elif action in ("delete", "close"):
        _st = rich._np.get(chat_id)
        if action == "delete" and _st and _st["msg"].id == query.message.id:
            rich.clear(chat_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif action == "stop":
        await anon.stop(chat_id)
        status = query.lang["stopped"]
        reply = query.lang["play_stopped"].format(user)

    try:
        if action in ["skip", "replay", "stop"]:
            await query.message.reply_text(reply)
            await query.message.delete()
        else:
            # Rich Message player: re-render blocks with the new status note
            if await rich.update(
                chat_id,
                message_id=query.message.id,
                note=reply,
                status=status if action != "resume" else None,
            ):
                return
            mtext = re.sub(
                r"\n\n<blockquote>.*?</blockquote>",
                "",
                query.message.caption.html or query.message.text.html,
                flags=re.DOTALL,
            )
            keyboard = buttons.controls(
                chat_id, status=status if action != "resume" else None
            )
        await query.edit_message_text(
            f"{mtext}\n\n<blockquote>{reply}</blockquote>", reply_markup=keyboard
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex("help") & ~app.bl_users)
@lang.language()
async def _help(_, query: types.CallbackQuery):
    data = query.data.split()
    if len(data) == 1:
        # "Help & Commands" button on /start (classic or rich message)
        try:
            if query.message and query.message.chat.type == enums.ChatType.PRIVATE:
                await query.answer()
                _text, _mk = query.lang["help_menu"], buttons.help_markup(query.lang)
                if await rich.send_menu(
                    query.message.chat.id, None, _text, _mk, flag=rich.HELP_ENABLED
                ):
                    return
                return await app.send_message(
                    query.message.chat.id, _text, reply_markup=_mk
                )
            return await query.answer(url=f"https://t.me/{app.username}?start=help")
        except Exception as e:
            logging.getLogger(__name__).warning("[Help] open failed: %r", e)
            try:
                return await query.answer(f"@{app.username}", show_alert=True)
            except Exception:
                return

    if data[1] == "close":
        try:
            await query.message.delete()
            return await query.message.reply_to_message.delete()
        except Exception:
            return

    if data[1] in ("back", "page"):  # main menu, page 1 or 2
        page = int(data[2]) if data[1] == "page" and len(data) > 2 and data[2].isdigit() else 1
        text, mk = query.lang["help_menu"], buttons.help_markup(query.lang, page=page)
    else:  # one help section; Back returns to the page it was opened from
        page = int(data[2]) if len(data) > 2 and data[2].isdigit() else 1
        text, mk = query.lang[f"help_{data[1]}"], buttons.help_markup(query.lang, True, page)

    if await rich.edit_menu(query, mk, text):
        return
    await query.edit_message_text(text=text, reply_markup=mk)


@app.on_callback_query(filters.regex("settings") & ~app.bl_users)
@lang.language()
@admin_check
async def _settings_cb(_, query: types.CallbackQuery):
    cmd = query.data.split()
    if len(cmd) == 1:
        return await query.answer()
    await query.answer(query.lang["processing"], show_alert=True)

    chat_id = query.message.chat.id
    _admin = await db.get_play_mode(chat_id)
    _delete = await db.get_cmd_delete(chat_id)
    _language = await db.get_lang(chat_id)

    if cmd[1] == "delete":
        _delete = not _delete
        await db.set_cmd_delete(chat_id, _delete)
    elif cmd[1] == "play":
        await db.set_play_mode(chat_id, _admin)
        _admin = not _admin
    await query.edit_message_reply_markup(
        reply_markup=buttons.settings_markup(
            query.lang,
            _admin,
            _delete,
            _language,
            chat_id,
        )
    )


# ============================================
# NEW: SOURCE MENU CALLBACKS
# Username-based ONLY - NO User ID system
# ============================================

@app.on_callback_query(filters.regex("^source_menu$") & ~app.bl_users)
@lang.language()
async def source_menu_cb(_, query: types.CallbackQuery):
    """Source button click - submenu dikhaye"""
    await query.answer(query.lang.get("processing", "📂 Source Menu"), show_alert=True)
    if await rich.edit_menu(query, buttons.source_markup(query.lang)):
        return
    await query.edit_message_reply_markup(
        reply_markup=buttons.source_markup(query.lang)
    )


@app.on_callback_query(filters.regex("^source_back$") & ~app.bl_users)
@lang.language()
async def source_back_cb(_, query: types.CallbackQuery):
    """Back button - wapas start menu pe le jaye"""
    await query.answer(query.lang.get("back", "🔙 Back"))
    if await rich.edit_menu(query, buttons.start_key(query.lang, private=True)):
        return
    await query.edit_message_reply_markup(
        reply_markup=buttons.start_key(query.lang, private=True)
        )


