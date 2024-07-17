from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("antispam.user.max")
        helper.copy("antispam.user.delay")
        helper.copy("antispam.room.max")
        helper.copy("antispam.room.delay")
        helper.copy("inline_thumbs")
