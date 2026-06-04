from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_user_agent: str = "drama-clip-scout/0.1 by YOUR_REDDIT_USERNAME"
    x_bearer_token: str = ""
    x_target_accounts: str = ""
    database_url: str = "sqlite:////data/clips.db"
    default_subreddit: str = "LivestreamFail"
    api_host: str = "0.0.0.0"
    api_port: int = 8787
    hermes_gateway_internal_url: str = "http://hermes:8642"
    known_streamer_names: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def x_accounts(self) -> List[str]:
        return [item.strip().lstrip("@") for item in self.x_target_accounts.split(",") if item.strip()]

    @property
    def streamers(self) -> List[str]:
        return [item.strip().lower() for item in self.known_streamer_names.split(",") if item.strip()]

    @property
    def reddit_configured(self) -> bool:
        return all(
            [
                self.reddit_client_id,
                self.reddit_client_secret,
                self.reddit_username,
                self.reddit_password,
                self.reddit_user_agent,
            ]
        )

    @property
    def x_configured(self) -> bool:
        return bool(self.x_bearer_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
