# Copyright (c) 2025 OpusMusic
# Licensed under the MIT License.
# This file is part of OpusMusic


import os
import asyncio

from ntgcalls import (ConnectionNotFound, TelegramServerError,
                      RTMPStreamingUnsupported, ConnectionError)
from pyrogram.errors import (ChatSendMediaForbidden, ChatSendPhotosForbidden,
                             MessageIdInvalid)
from pyrogram.types import InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from opus import (app, config, db, lang, logger,
                   queue, thumb, userbot, yt)
from opus.helpers import Media, Track, buttons, rich


def _is_url(path) -> bool:
    return isinstance(path, str) and path.startswith(("http://", "https://"))


class TgCall(PyTgCalls):
    def __init__(self):
        self.clients = []

    async def pause(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=True)
        return await client.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=False)
        return await client.resume(chat_id)

    async def stop(self, chat_id: int) -> None:
        client = await db.get_assistant(chat_id)
        # Manual /stop pe poori pending queue chhoot jaati hai —
        # un sab tracks ki files bhi cleanup schedule kar dete hain,
        # warna woh disk pe orphan reh jaatein (kabhi delete na hon).
        for media in queue.get_queue(chat_id):
            if media.file_path:
                asyncio.create_task(self._schedule_cleanup(media.file_path))

        queue.clear(chat_id)
        rich.clear(chat_id)
        await db.remove_call(chat_id)
        await db.set_loop(chat_id, 0)

        try:
            await client.leave_call(chat_id, close=False)
        except Exception:
            pass


    async def play_media(
        self,
        chat_id: int,
        message: Message,
        media: Media | Track,
        seek_time: int = 0,
    ) -> None:
        client = await db.get_assistant(chat_id)
        _lang = await lang.get_lang(chat_id)
        _thumb = (
            await thumb.generate(media)
            if isinstance(media, Track)
            else config.DEFAULT_THUMB
        ) if config.THUMB_GEN else None

        if not media.file_path:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            return await self.play_next(chat_id)

        def _make_stream(path: str):
            return types.MediaStream(
                media_path=path,
                audio_parameters=types.AudioQuality.HIGH,
                video_parameters=types.VideoQuality.HD_720p,
                audio_flags=types.MediaStream.Flags.REQUIRED,
                video_flags=(
                    types.MediaStream.Flags.AUTO_DETECT
                    if media.video
                    else types.MediaStream.Flags.IGNORE
                ),
                ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
            )

        try:
            try:
                await client.play(
                    chat_id=chat_id,
                    stream=_make_stream(media.file_path),
                    config=types.GroupCallConfig(auto_start=False),
                )
            except (
                exceptions.NoActiveGroupCall,
                RTMPStreamingUnsupported,
                TelegramServerError,
                ConnectionNotFound,
                ConnectionError,
            ):
                raise
            except Exception as e:
                # Progressive stream URL fail hua (token expire, API error, ffmpeg
                # URL nahi khol paya) -> purana tareeka: file download karke play.
                if not _is_url(media.file_path):
                    raise
                logger.warning(f"[Stream] URL play failed ({e!r}), file download fallback")
                media.file_path = await yt.download(
                    media.id, video=media.video, title=media.title
                )
                if not media.file_path:
                    raise FileNotFoundError
                await client.play(
                    chat_id=chat_id,
                    stream=_make_stream(media.file_path),
                    config=types.GroupCallConfig(auto_start=False),
                )

            if _is_url(media.file_path):
                # Play shuru ho gaya; peeche asli file utaar lo taaki replay/loop/seek
                # (jahan URL ka token expire ho sakta hai) local file se chalein.
                asyncio.create_task(self._localize(media))
            if not seek_time:
                media.time = 1
                await db.add_call(chat_id)
                text = _lang["play_media"].format(
                    media.url,
                    media.title,
                    media.duration,
                    media.user,
                )
                # Rich Message player (Bot API 10.1+); falls back to the
                # classic inline keyboard below if unavailable / failed.
                sent_rich = await rich.send_player(
                    chat_id, message, _thumb, text, _lang, duration=media.duration_sec
                )
                if sent_rich:
                    media.message_id = sent_rich.id
                    return

                from opus.plugins.autoplay import is_autoplay as _is_autoplay
                _ap_on = await _is_autoplay(chat_id)
                _ap_text = "ᴀᴜᴛᴏᴘʟᴀʏ: ᴏɴ ✅" if _ap_on else "ᴀᴜᴛᴏᴘʟᴀʏ: ᴏꜰꜰ ❌"
                keyboard = buttons.controls(chat_id, autoplay_on=_ap_on, autoplay_text=_ap_text)
                try:
                    if _thumb:
                        await message.edit_media(
                            media=InputMediaPhoto(
                                media=_thumb,
                                caption=text,
                            ),
                            reply_markup=keyboard,
                        )
                    else:
                        await message.edit_text(text, reply_markup=keyboard)
                except (ChatSendMediaForbidden, ChatSendPhotosForbidden, MessageIdInvalid):
                    if _thumb:
                        sent = await app.send_photo(
                            chat_id=chat_id,
                            photo=_thumb,
                            caption=text,
                            reply_markup=keyboard,
                        )
                    else:
                        sent = await app.send_message(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=keyboard,
                        )
                    media.message_id = sent.id
        except FileNotFoundError:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            await self.play_next(chat_id)
        except exceptions.NoActiveGroupCall:
            await self.stop(chat_id)
            await message.edit_text(_lang["error_no_call"])
        except exceptions.NoAudioSourceFound:
            await message.edit_text(_lang["error_no_audio"])
            await self.play_next(chat_id)
        except (ConnectionError, ConnectionNotFound, TelegramServerError):
            await self.stop(chat_id)
            await message.edit_text(_lang["error_tg_server"])
        except RTMPStreamingUnsupported:
            await self.stop(chat_id)
            await message.edit_text(_lang["error_rtmp"])


    async def replay(self, chat_id: int) -> None:
        if not await db.get_call(chat_id):
            return

        media = queue.get_current(chat_id)
        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_again"])
        media.message_id = msg.id
        await self.play_media(chat_id, msg, media)


    async def play_next(self, chat_id: int) -> None:
        if loop := await db.get_loop(chat_id):
            await db.set_loop(chat_id, loop - 1)
            return await self.replay(chat_id)

        media = queue.get_next(chat_id)
        try:
            if media.message_id:
                await app.delete_messages(
                    chat_id=chat_id,
                    message_ids=media.message_id,
                    revoke=True,
                )
                media.message_id = 0
        except Exception:
            pass

        if not media:
            return await self.stop(chat_id)

        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_next"])
        if not media.file_path:
            media.file_path = await yt.download(
                media.id, video=media.video, title=media.title, stream=not media.video
            )
            if not media.file_path:
                await self.play_next(chat_id)
                return await msg.edit_text(
                    _lang["error_no_file"].format(config.SUPPORT_CHAT)
                )

        media.message_id = msg.id
        await self.play_media(chat_id, msg, media)


    async def ping(self) -> float:
        pings = [client.ping for client in self.clients]
        return round(sum(pings) / len(pings), 2)


    async def decorators(self, client: PyTgCalls) -> None:
        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:
            if isinstance(update, types.StreamEnded):
                if update.stream_type == types.StreamEnded.Type.AUDIO:
                    # Jo song abhi khatam hua uska file_path pehle
                    # nikal lo — `play_next()` andar hi queue se
                    # ise pop kar dega, uske baad reference nahi
                    # milega. Delete ka schedule loop ke bina hi
                    # karte hain (loop off ho to hi delete karna
                    # hai — loop on ho to yehi file dobara replay
                    # hogi).
                    finished = queue.get_current(update.chat_id)
                    is_looping = bool(await db.get_loop(update.chat_id))

                    await self.play_next(update.chat_id)

                    if finished and not is_looping:
                        asyncio.create_task(
                            self._schedule_cleanup(finished.file_path)
                        )
            elif isinstance(update, types.ChatUpdate):
                if update.status in [
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ]:
                    await self.stop(update.chat_id)

    async def _localize(self, media) -> None:
        """Stream-URL se play shuru hone ke baad asli file download karke
        media.file_path ko local file bana deta hai (best-effort)."""
        try:
            path = await yt.download(media.id, video=False, title=media.title)
            if path and not _is_url(path):
                media.file_path = path
        except Exception as e:
            logger.warning(f"[Stream] localize failed: {e}")

    async def _schedule_cleanup(self, file_path: str, delay: int = 1800) -> None:
        """
        Stream khatam hone ke `delay` seconds (default 30 min) baad
        downloaded file ko disk se hata deta hai — is dauraan Turso
        cache background save comfortably complete ho chuka hota hai.
        Sirf `downloads/` folder ke andar ki file delete hoti hai
        (safety check), taaki koi unrelated/uploaded file galti se
        na hat jaye. Best-effort hai — file already na ho ya delete
        fail ho, dono cases silently ignore hote hain.
        """
        if not file_path:
            return

        await asyncio.sleep(delay)

        try:
            real = os.path.realpath(file_path)
            downloads_dir = os.path.realpath("downloads")
            if not real.startswith(downloads_dir + os.sep):
                return
            if os.path.exists(real):
                os.remove(real)
                logger.info(f"[Cleanup] Deleted: {file_path}")
        except Exception as e:
            logger.warning(f"[Cleanup] Delete error for {file_path}: {e}")


    async def boot(self) -> None:
        PyTgCallsSession.notice_displayed = True
        for ub in userbot.clients:
            client = PyTgCalls(ub, cache_duration=100)
            await client.start()
            self.clients.append(client)
            await self.decorators(client)
        logger.info("PyTgCalls client(s) started.")

