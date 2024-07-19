import asyncio
from datetime import datetime, timezone
from io import BytesIO
from itertools import repeat
from time import time
from typing import Awaitable, NamedTuple, cast

from attr import dataclass
from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.types import (
    ContentURI,
    ImageInfo,
    MediaMessageEventContent,
    MessageType,
    RoomID,
    TextMessageEventContent,
    UserID,
)
from mautrix.util.config import BaseProxyConfig

from .config import Config
from .eh_api import gallery_api, get_gids, gmetadata, resolve_page_gids

try:
    from PIL import Image as Pillow
except ImportError:
    Pillow = None


class Image(NamedTuple):
    title: str
    url: ContentURI
    info: ImageInfo


def create_ex_url(title: str, gid: int, token: str):
    return f'<a href="https://exhentai.org/g/{gid}/{token}/">{title}</a>'


def format_tags(tags: list[str]):
    tag_dict: dict[str, list[str]] = {}
    for tag in tags:
        if ":" in tag:
            namespace, tag = tag.split(":")
        else:
            namespace = "misc"
        if namespace in tag_dict:
            tag_dict[namespace].append(tag)
        else:
            tag_dict[namespace] = [tag]

    parts = [
        f'<strong>{namespace}</strong><br>{", ".join(tags)}'
        for namespace, tags in tag_dict.items()
    ]
    return "<br>".join(parts)


def format_msg(gallery: gmetadata, thumb: Image | None = None, collapsed: bool = False):
    title = create_ex_url(gallery["title"], gallery["gid"], gallery["token"])
    title_jpn = gallery["title_jpn"]
    category = gallery["category"]
    pages = f'{gallery["filecount"]} pages'
    rating = f'{gallery["rating"]}★'
    timestamp = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
    tags = format_tags(gallery["tags"])
    wrapper = ("<details>", "</details>") if collapsed else ("<p>", "</p>")
    img = (
        f'<img src="{thumb.url}" title="{thumb.title}" alt="Gallery thumbnail"><br>'
        if thumb
        else ""
    )
    header = f"<summary>{title}</summary>\n{img}" if collapsed else f"{img}{title}<br>"

    return f"""\
{wrapper[0]}
{header}
{title_jpn}<br>
{category} | {pages} | {rating} | {timestamp}<br>
{tags}<br>
{wrapper[1]}"""


# Stolen from reactbot.
@dataclass
class FloodInfo:
    max: int
    delay: int
    count: int
    last_message: int

    def bump(self, count: int = 1) -> bool:
        now = int(time())
        if self.last_message + self.delay < now:
            self.count = 0
        if self.count + count > self.max:
            return True
        else:
            self.last_message = now
            self.count += count
            return False


class SadPanda(Plugin):
    allowed_msgtypes: tuple[MessageType, ...] = (MessageType.TEXT, MessageType.EMOTE)
    user_flood: dict[UserID, FloodInfo]
    room_flood: dict[RoomID, FloodInfo]
    config: BaseProxyConfig  # type:ignore - dunno

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.user_flood = {}
        self.room_flood = {}
        self.on_external_config_update()

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        for fi in self.user_flood.values():
            fi.max = self.config["antispam.user.max"]
            fi.delay = self.config["antispam.user.delay"]
        for fi in self.room_flood.values():
            fi.max = self.config["antispam.room.max"]
            fi.delay = self.config["antispam.room.delay"]

    def _make_flood_info(self, for_type: str) -> FloodInfo:
        return FloodInfo(
            max=self.config[f"antispam.{for_type}.max"],
            delay=self.config[f"antispam.{for_type}.delay"],
            count=0,
            last_message=0,
        )

    def _get_flood_info(self, flood_map: dict, key: str, for_type: str) -> FloodInfo:
        try:
            return flood_map[key]
        except KeyError:
            fi = flood_map[key] = self._make_flood_info(for_type)
            return fi

    def is_flood(self, evt: MessageEvent, count: int = 1) -> bool:
        return (
            self._get_flood_info(self.user_flood, evt.sender, "user").bump()
            or self._get_flood_info(self.room_flood, evt.room_id, "room").bump()
        )

    async def get_thumb(self, gallery: gmetadata):
        info = ImageInfo()
        async with self.http.get(gallery["thumb"]) as r:
            data = await r.read()
            info.size = len(data)
            info.mimetype = r.headers["Content-Type"]
            title = r.url.path.split("/")[-1]
            if Pillow:
                img = Pillow.open(BytesIO(data))
                info.width, info.height = img.size
            mxc = await self.client.upload_media(data, info.mimetype)
            return Image(url=mxc, info=info, title=title)

    async def process_message(self, evt: MessageEvent):
        """Process EH links in the message and respond with gallery metadata."""

        content = cast("TextMessageEventContent", evt.content)
        gid_dict, page_list = get_gids(content.body)
        count = len(gid_dict)
        if self.is_flood(evt, count):
            self.log.warning(f"Flood detected in {evt.room_id}")
            return

        gid_list = await resolve_page_gids(self, gid_dict, page_list)
        galleries = await gallery_api(self, gid_list)
        if not galleries:
            return

        self.log.info(f"Responding with metadata of {count} galleries in {evt.room_id}")

        tasks: list[Awaitable[Image]] = list()
        for gallery in galleries:
            tasks.append(self.get_thumb(gallery))

        thumbs = await asyncio.gather(*tasks)
        collapsed = repeat(count > 1)

        # Respond to messages with 3+ links in collapsed form with inlined thumbs regardless of setting to prevent spam.
        if self.config["inline_thumbs"] or count > 2:
            body = "".join(map(format_msg, galleries, thumbs, collapsed))
            await evt.respond(body, allow_html=True)
        else:
            for gallery, thumb in zip(galleries, thumbs):
                image = MediaMessageEventContent(
                    body=thumb.title,
                    url=thumb.url,
                    info=thumb.info,
                    msgtype=MessageType.IMAGE,
                )
                await evt.respond(image)
                await evt.respond(format_msg(gallery), allow_html=True)

    @command.passive(r"https?://e[-x]hentai\.org/(?:s|g|mpv)")
    async def handler(self, evt: MessageEvent, _match) -> None:
        assert isinstance(evt.content, TextMessageEventContent)
        if (
            evt.sender == self.client.mxid
            or evt.content.msgtype not in self.allowed_msgtypes
            or evt.content.get_edit()
        ):
            return

        evt.content.trim_reply_fallback()
        await self.process_message(evt)
