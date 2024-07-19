from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("ratelimit.user.per_second")
        helper.copy("ratelimit.user.burst_count")
        helper.copy("ratelimit.room.per_second")
        helper.copy("ratelimit.room.burst_count")
        helper.copy("blacklist")
        helper.copy("inline_thumbs")
