from __future__ import annotations

from abc import ABC, abstractmethod

from praha_predictor.schemas import (
    NormalizedListing,
    RawSnapshot,
    RejectReason,
    RunContext,
    SourceProbeReport,
)


class ListingSourceAdapter(ABC):
    source_name: str

    @abstractmethod
    def discover_listing_urls(self, run_context: RunContext) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def fetch_listing(self, url: str, run_context: RunContext) -> RawSnapshot:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_snapshot: RawSnapshot) -> NormalizedListing | RejectReason:
        raise NotImplementedError

    @abstractmethod
    def probe_source(self, sample_size: int) -> SourceProbeReport:
        raise NotImplementedError

