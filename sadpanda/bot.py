import asyncio
from datetime import datetime, timezone
from io import BytesIO
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
from .eh_api import get_gallery_meta, gmetadata

try:
    from PIL import Image as Pillow
except ImportError:
    Pillow = None


class Image(NamedTuple):
    title: str
    url: ContentURI
    info: ImageInfo


def create_markdown_url(title: str, url: str):
    return f"[{title}]({url})"


def create_ex_url(gid: int, token: str):
    return f"https://exhentai.org/g/{gid}/{token}/"


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
        f'**{namespace}**<br>{", ".join(tags)}' for namespace, tags in tag_dict.items()
    ]
    return "<br>".join(parts)


def format_lite(galleries: list[gmetadata]) -> str:
    link_list = [
        create_markdown_url(g["title"], create_ex_url(g["gid"], g["token"]))
        for g in galleries
    ]
    return "<br>".join(link_list)


def format_full(gallery: gmetadata, thumb: Image | None = None):
    url = create_ex_url(gallery["gid"], gallery["token"])
    timestamp = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
    s = f"""\
{create_markdown_url(gallery["title"], url)}<br>
{gallery["title_jpn"]}<br>
{gallery["category"]} | {gallery["filecount"]} pages | {gallery["rating"]}★ | {timestamp}<br>
{format_tags(gallery["tags"])}"""

    if thumb:
        return f'<img src="{thumb.url}" title="{thumb.title}" alt="Gallery thumbnail"><br>\n{s}'
    else:
        return s


# Stolen from reactbot.
@dataclass
class FloodInfo:
    max: int
    delay: int
    count: int
    last_message: int

    def bump(self) -> bool:
        now = int(time())
        if self.last_message + self.delay < now:
            self.count = 0
        self.count += 1
        if self.count > self.max:
            return True
        self.last_message = now
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

    def is_flood(self, evt: MessageEvent) -> bool:
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
        galleries = await get_gallery_meta(self, content.body)
        if not galleries:
            return
        count = len(galleries)
        self.log.info(f"Responding with metadata of {count} galleries in {evt.room_id}")
        if count > 5:  # Titles only if the message has too many links.
            await evt.respond(format_lite(galleries), allow_html=True)
        else:
            tasks: list[Awaitable[Image]] = list()
            for gallery in galleries:
                tasks.append(self.get_thumb(gallery))

            thumbs = await asyncio.gather(*tasks)

            if self.config["inline_thumbs"]:
                body = "<br><br>".join(map(format_full, galleries, thumbs))
                await evt.respond(body, allow_html=True)
            else:
                for gallery, thumb in zip(galleries, thumbs):
                    image = MediaMessageEventContent(
                        body=thumb.title,
                        url=thumb.url,
                        info=thumb.info,
                        msgtype=MessageType.IMAGE,
                    )
                    body = format_full(gallery)
                    await evt.respond(image)
                    await evt.respond(body, allow_html=True)

    @command.passive(r"https?://e[-x]hentai\.org/(?:s|g|mpv)")
    async def handler(self, evt: MessageEvent, _match) -> None:
        assert isinstance(evt.content, TextMessageEventContent)
        if (
            evt.sender == self.client.mxid
            or evt.content.msgtype not in self.allowed_msgtypes
            or evt.content.get_edit()
            or self.is_flood(evt)
        ):
            return

        evt.content.trim_reply_fallback()
        await self.process_message(evt)
