import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from itertools import repeat
from time import time
from typing import Awaitable, ClassVar, DefaultDict, NamedTuple

from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.types import (
    ContentURI,
    EventType,
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


class Bucket:
    per_second: ClassVar[float] = 0
    burst_count: ClassVar[int] = 0

    tokens: int
    last_update: int

    def __init__(self):
        self.tokens = self.burst_count
        self.last_update = int(time())

    def _update_tokens(self) -> None:
        now = int(time())
        if self.tokens != self.burst_count:
            self.tokens = min(
                self.burst_count,
                self.tokens + int((now - self.last_update) * self.per_second),
            )
        self.last_update = now

    def ok(self, count: int) -> bool:
        self._update_tokens()
        if self.tokens >= count:
            self.tokens -= count
            return True
        else:
            return False


# Following load limiting recommendations from https://ehwiki.org/wiki/API
class API_Bucket(Bucket):
    per_second = 0.2
    burst_count = 5


# Rate limit shared among all instances.
api_ratelimit = API_Bucket()


def BucketFactory():
    class BucketSubclass(Bucket):
        pass

    return BucketSubclass


def pluralize(num: int, str: str):
    return f'{num} {str}{"s" if num > 1 else ""}'


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
    pages = pluralize(int(gallery["filecount"]), "page")
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
{tags}
{wrapper[1]}"""


class SadPanda(Plugin):
    allowed_msgtypes: tuple[MessageType, ...] = (MessageType.TEXT, MessageType.EMOTE)
    UserBucket: type[Bucket]
    RoomBucket: type[Bucket]
    user_ratelimit: DefaultDict[UserID, Bucket]
    room_ratelimit: DefaultDict[RoomID, Bucket]
    blacklist: list[str]
    config: BaseProxyConfig  # type:ignore - dunno

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.UserBucket = BucketFactory()
        self.RoomBucket = BucketFactory()
        self.user_ratelimit = defaultdict(self.UserBucket)
        self.room_ratelimit = defaultdict(self.RoomBucket)
        self.on_external_config_update()

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self.UserBucket.per_second = self.config["ratelimit.user.per_second"]
        self.UserBucket.burst_count = self.config["ratelimit.user.burst_count"]
        self.RoomBucket.per_second = self.config["ratelimit.room.per_second"]
        self.RoomBucket.burst_count = self.config["ratelimit.room.burst_count"]
        self.blacklist = [self.client.mxid] + self.config["blacklist"]

    def ratelimit_ok(
        self, evt: MessageEvent, gallery_count: int, api_req_count: int
    ) -> bool:
        if not api_ratelimit.ok(api_req_count):
            kind, count, tokens = "API", api_req_count, api_ratelimit.tokens
        elif not self.user_ratelimit[evt.sender].ok(gallery_count):
            kind, count, tokens = (
                "User",
                gallery_count,
                self.user_ratelimit[evt.sender].tokens,
            )
        elif not self.room_ratelimit[evt.room_id].ok(gallery_count):
            kind, count, tokens = (
                "Room",
                gallery_count,
                self.room_ratelimit[evt.room_id].tokens,
            )
        else:
            return True

        self.log.warning(
            f"""{kind} rate limit exceeded in {evt.room_id} by {evt.sender}
{pluralize(count, "token")} needed, but only {tokens} remaining"""
        )
        return False

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

    @command.passive(r"https?://e[-x]hentai\.org/(?:s|g|mpv)")
    async def handler(self, evt: MessageEvent, _match):
        assert isinstance(evt.content, TextMessageEventContent)
        assert self.client.state_store
        can_send = await self.client.state_store.has_power_level(
            evt.room_id, self.client.mxid, EventType.ROOM_MESSAGE
        )
        if (
            evt.sender in self.blacklist
            or evt.content.msgtype not in self.allowed_msgtypes
            or evt.content.get_edit()
        ):
            return
        if not can_send:
            self.log.warning(f"Not allowed to send messages in {evt.room_id}")
            return

        evt.content.trim_reply_fallback()
        gid_dict, page_list = get_gids(evt.content.body)
        if not gid_dict:
            return
        gallery_count = len(gid_dict)
        api_req_count = 2 if page_list else 1
        if not self.ratelimit_ok(evt, gallery_count, api_req_count):
            return
        gid_list = await resolve_page_gids(self, gid_dict, page_list)
        galleries = await gallery_api(self, gid_list)
        if not galleries:
            return

        self.log.info(
            f"Responding with metadata of {gallery_count} galleries in {evt.room_id}"
        )

        tasks: list[Awaitable[Image]] = list()
        for gallery in galleries:
            tasks.append(self.get_thumb(gallery))

        thumbs = await asyncio.gather(*tasks)
        collapsed = repeat(gallery_count >= self.config["collapse_thresh"])

        if gallery_count >= self.config["inline_thresh"]:
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
