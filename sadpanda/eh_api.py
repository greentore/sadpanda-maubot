import re
from typing import NamedTuple, TYPE_CHECKING, TypedDict

import aiohttp

if TYPE_CHECKING:
    from .bot import SadPanda

pattern = re.compile(
    r"https?://e[-x]hentai\.org/(?:(?:g|mpv)/(?P<gid>\d+)/(?P<token>[\da-f]{10})|s/(?P<token_p>[\da-f]{10})/(?P<gid_p>\d+)-(?P<page>\d+))"
)

EH_API = "https://api.e-hentai.org/api.php"


class gallery_tuple(NamedTuple):
    gid: int
    token: str


class page_tuple(NamedTuple):
    gid: int
    token: str
    page: int


class gid_result(NamedTuple):
    gid_dict: dict[int, str]
    """A dict of gids found in the message. Value is an empty string for page links, and token otherwise."""
    page_list: list[page_tuple]
    """A list of tuples with page data needed for requesting gallery tokens from the API."""


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


timeout = aiohttp.ClientTimeout(total=30)


async def gallery_api(
    bot: "SadPanda", gid_list: list[gallery_tuple]
) -> list[gmetadata]:
    """Queries EH API with a list of (gid, gallery_token) tuples to get gallery metadata."""

    payload = {"method": "gdata", "gidlist": gid_list, "namespace": 1}
    filtered: list[gmetadata] = []
    async with bot.http.post(EH_API, json=payload, timeout=timeout) as r:
        if r.ok:
            j = await r.json(content_type=None)
            for entry in j["gmetadata"]:
                if "error" in entry:
                    bot.log.error(f"{entry['gid']} errored with `{entry['error']}`")
                else:
                    filtered.append(entry)

    return filtered


async def _page_api(
    bot: "SadPanda", page_list: list[page_tuple]
) -> list[gallery_tuple]:
    """Queries EH API with a list of (gid, page_token, page_number) tuples to get gallery tokens."""

    payload = {"method": "gtoken", "pagelist": page_list}
    filtered: list[gallery_tuple] = []
    async with bot.http.post(EH_API, json=payload, timeout=timeout) as r:
        if r.ok:
            j = await r.json(content_type=None)
            for entry in j["tokenlist"]:
                if "error" in entry:
                    bot.log.error(f"{entry['gid']} errored with `{entry['error']}`")
                else:
                    filtered.append(gallery_tuple(entry["gid"], entry["token"]))

    return filtered


async def resolve_page_gids(
    bot: "SadPanda", gid_dict: dict[int, str], page_list: list[page_tuple]
) -> list[gallery_tuple]:
    """Fetches gallery tokens for gids that are missing them and returns a list of (gid, gallery_token) tuples."""

    if page_list:
        filtered = [p for p in page_list if not gid_dict.get(p.gid)]
        if filtered:
            results = await _page_api(bot, filtered)
            for gid, token in results:
                gid_dict[gid] = token

    return [gallery_tuple(gid, token) for gid, token in gid_dict.items() if token]


def get_gids(message: str) -> gid_result:
    """Searches the message for EH links and returns gids in a half-assed format to do ratelimiting before firing API calls."""

    gid_dict: dict[int, str] = {}
    page_list: list[page_tuple] = []
    results = [m.groupdict() for m in pattern.finditer(message)]
    # EH API allows up to 25 entries per request. More than that seems abusive, so no support.
    if len(results) <= 25:
        for r in results:
            gid = int(r["gid"] or r["gid_p"])
            token = r["token"] or r["token_p"]
            page = r["page"]
            if not page:
                gid_dict[gid] = token
            else:
                if gid not in gid_dict:
                    gid_dict[gid] = ""  # Reserving slots to preserve order.
                    page_list.append(page_tuple(gid, token, int(page)))

    return gid_result(gid_dict, page_list)
