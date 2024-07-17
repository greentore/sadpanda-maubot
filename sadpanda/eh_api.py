import re
from typing import NamedTuple, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .bot import SadPanda

pattern = re.compile(
    r"https?://e[-x]hentai\.org/(?:(?:g|mpv)/(?P<gid>\d+)/(?P<token>[\da-f]{10})|s/(?P<token_p>[\da-f]{10})/(?P<gid_p>\d+)-(?P<page>\d+))"
)

EH_API = "https://api.e-hentai.org/api.php"


class gid_tuple(NamedTuple):
    gid: int
    token: str


class page_gid_tuple(NamedTuple):
    gid: int
    token: str
    page: int


class torrent(TypedDict):
    hash: str
    added: int
    name: str
    tsize: int
    fsize: int


class gmetadata(TypedDict):
    gid: int
    token: str
    archiver_key: str
    title: str
    title_jpn: str
    category: str
    thumb: str
    uploader: str
    posted: int
    filecount: int
    filesize: int
    expunged: bool
    rating: float
    torrentcount: int
    torrents: list[torrent]
    tags: list[str]
    parent_gid: int
    parent_key: str
    first_gid: int
    first_key: str


async def _gallery_api(bot: "SadPanda", gid_list: list[gid_tuple]) -> list[gmetadata]:
    """Query EH API with a list of gid and gallery token tuples to get gallery metadata."""
    payload = {"method": "gdata", "gidlist": gid_list, "namespace": 1}
    async with bot.http.post(EH_API, json=payload) as r:
        j = await r.json(content_type=None)
        filtered: list[gmetadata] = []
        for entry in j["gmetadata"]:
            if "error" in entry:
                bot.log.error(f"{entry['gid']} errored with `{entry['error']}`")
            else:
                filtered.append(entry)
        return filtered


async def _page_api(
    bot: "SadPanda", page_list: list[page_gid_tuple]
) -> list[gid_tuple]:
    """Query EH API with a list of gid, page token and page number tuples to get gallery tokens."""
    payload = {"method": "gtoken", "pagelist": page_list}
    async with bot.http.post(EH_API, json=payload) as r:
        j = await r.json(content_type=None)
        filtered: list[gid_tuple] = []
        for entry in j["tokenlist"]:
            if "error" in entry:
                bot.log.error(f"{entry['gid']} errored with `{entry['error']}`")
            else:
                filtered.append(gid_tuple(entry["gid"], entry["token"]))
        return filtered


async def _get_gidlist(bot: "SadPanda", message: str) -> list[gid_tuple]:
    """Get all gids and tokens in the message."""
    gallery_dict: dict[int, str | None] = {}
    page_list: list[page_gid_tuple] = []
    results = [m.groupdict() for m in pattern.finditer(message)]
    # EH API allows up to 25 entries per request. More than that seems abusive, so no support.
    if not results or len(results) > 25:
        return []

    for r in results:
        gid = int(r["gid"] or r["gid_p"])
        token = r["token"] or r["token_p"]
        page = r["page"]
        if not page:
            gallery_dict[gid] = token
        else:
            if gid not in gallery_dict:
                gallery_dict[gid] = None  # Reserving slots to preserve order.
                page_list.append(page_gid_tuple(gid, token, int(page)))

    if page_list:
        # Skip pages we already got gallery tokens for from gallery links.
        filtered = [p for p in page_list if not gallery_dict.get(p.gid)]
        if filtered:
            results = await _page_api(bot, filtered)
            for gid, token in results:
                gallery_dict[gid] = token

    return [gid_tuple(gid, token) for gid, token in gallery_dict.items() if token]


async def get_gallery_meta(bot: "SadPanda", message: str) -> list[gmetadata] | None:
    """Get gallery metadata for all EH links in the message."""
    gid_list = await _get_gidlist(bot, message)
    if gid_list:
        return await _gallery_api(bot, gid_list)
