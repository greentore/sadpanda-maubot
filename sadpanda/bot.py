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
    per_second: ClassVar[float]
    burst_count: ClassVar[int]

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


class UserBucket(Bucket):
    pass


class RoomBucket(Bucket):
    pass


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
    api_ratelimit = API_Bucket()
    user_ratelimit: DefaultDict[UserID, UserBucket] = defaultdict(UserBucket)
    room_ratelimit: DefaultDict[RoomID, RoomBucket] = defaultdict(RoomBucket)
    config: BaseProxyConfig  # type:ignore - dunno

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.on_external_config_update()

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        UserBucket.per_second = self.config["ratelimit.user.per_second"]
        UserBucket.burst_count = self.config["ratelimit.user.burst_count"]
        RoomBucket.per_second = self.config["ratelimit.room.per_second"]
        RoomBucket.burst_count = self.config["ratelimit.room.burst_count"]

    def ratelimit_ok(
        self, evt: MessageEvent, gallery_count: int, api_req_count: int
    ) -> bool:
        if not self.api_ratelimit.ok(api_req_count):
            self.log.warning(
                f"""API rate limit exceeded in {evt.room_id} by {evt.sender}
{pluralize(api_req_count, "token")} needed, but only {self.api_ratelimit.tokens} remaining"""
            )
        elif not self.user_ratelimit[evt.sender].ok(gallery_count):
            self.log.warning(
                f"""User rate limit exceeded in {evt.room_id} by {evt.sender}
{pluralize(gallery_count, "token")} needed, but only {self.user_ratelimit[evt.sender].tokens} remaining"""
            )
        elif not self.room_ratelimit[evt.room_id].ok(gallery_count):
            self.log.warning(
                f"""Room rate limit exceeded in {evt.room_id} by {evt.sender}
{pluralize(gallery_count, "token")} needed, but only {self.room_ratelimit[evt.room_id].tokens} remaining"""
            )
        else:
            return True

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
        if (
            evt.sender == self.client.mxid
            or evt.content.msgtype not in self.allowed_msgtypes
            or evt.content.get_edit()
        ):
            return

        evt.content.trim_reply_fallback()
        gid_dict, page_list = get_gids(evt.content.body)
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
        collapsed = repeat(gallery_count > 1)

        # Respond to messages with 3+ galleries in collapsed form with inlined thumbs regardless of setting to prevent spam.
        if self.config["inline_thumbs"] or gallery_count > 2:
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
