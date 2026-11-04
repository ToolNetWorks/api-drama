"""Provider abstraction — add DramaBox/NetShort/ShortMax without touching auth."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class UpstreamNotVerified(ProviderError):
    def __init__(self, detail: str = "Upstream endpoint path not verified from provider docs"):
        super().__init__("UPSTREAM_NOT_VERIFIED", detail, 502)


class UpstreamError(ProviderError):
    pass


class DramaProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def get_series(self, series_id: str) -> Optional[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_episode(self, series_id: str, episode_id: str) -> Optional[dict]:
        raise NotImplementedError


# Registry for future providers (dramabox, netshort, shortmax, ...).
REGISTRY: dict[str, DramaProvider] = {}


def register(provider: DramaProvider) -> DramaProvider:
    REGISTRY[provider.name] = provider
    return provider


def get_provider(name: str) -> Optional[DramaProvider]:
    return REGISTRY.get(name)
