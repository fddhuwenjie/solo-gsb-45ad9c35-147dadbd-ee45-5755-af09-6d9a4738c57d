"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TapEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    passenger_id: str = Field(min_length=1, max_length=128)
    station_id: str = Field(min_length=1, max_length=64)
    tap_type: Literal["in", "out"]
    occurred_at: datetime  # any ISO-8601 offset accepted, normalised to UTC


class Adjustment(BaseModel):
    trip_id: str
    day_key: str | None
    week_key: str | None
    old_amount_cents: int
    new_amount_cents: int
    delta_cents: int
    basis: dict


class EventResult(BaseModel):
    event_id: str
    status: Literal["processed", "duplicate", "corrected"]
    event_pairing: Literal["paired", "pending"]
    pending_reason: str | None = None
    adjustments: list[Adjustment]


class PendingEvent(BaseModel):
    event_id: str
    passenger_id: str
    station_id: str
    tap_type: str
    occurred_at: str
    reason: str


class TripOut(BaseModel):
    trip_id: str
    from_station: str
    to_station: str
    from_zone: int
    to_zone: int
    started_at: str
    ended_at: str
    day_key: str
    week_key: str
    base_fare_cents: int
    transfer_discount_cents: int
    amount_cents: int
    basis: dict


class PeriodTotal(BaseModel):
    key: str
    amount_cents: int
    cap_cents: int
    trip_count: int


class Bill(BaseModel):
    passenger_id: str
    currency: str
    trips: list[TripOut]
    daily_totals: list[PeriodTotal]
    weekly_totals: list[PeriodTotal]
    total_cents: int


class ReconRecordIn(BaseModel):
    serial_no: str = Field(min_length=1, max_length=128)
    passenger_id: str = Field(min_length=1, max_length=128)
    charged_at: datetime  # any ISO-8601 offset accepted, normalised to UTC
    amount_cents: int = Field(ge=0)
    trip_id: str | None = Field(default=None, min_length=1, max_length=256)


class ReconBatchIn(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)
    window_start: datetime  # half-open UTC window [window_start, window_end)
    window_end: datetime    # on the internal trips' tap-out time (ended_at)
    tolerance_seconds: int = Field(ge=0)
    records: list[ReconRecordIn] = Field(default_factory=list)


class ReconResultItem(BaseModel):
    category: Literal[
        "matched", "overcharged", "undercharged",
        "internal_only", "external_only", "ambiguous",
    ]
    passenger_id: str
    trip_id: str | None = None
    serial_no: str | None = None
    internal_amount_cents: int | None = None
    external_amount_cents: int | None = None
    difference_cents: int | None = None  # external - internal
    internal_ended_at: str | None = None
    external_charged_at: str | None = None
    match_method: Literal["trip_id", "tolerance"] | None = None
    reason: str


class ReconSummary(BaseModel):
    matched: int
    overcharged: int
    undercharged: int
    internal_only: int
    external_only: int
    ambiguous: int
    snapshot_trip_count: int
    external_record_count: int


class ReconBatchOut(BaseModel):
    batch_id: str
    status: Literal["created", "duplicate"]
    window_start: str
    window_end: str
    tolerance_seconds: int
    currency: str
    summary: ReconSummary
    results: list[ReconResultItem]


AuditOperation = Literal[
    "event_processed", "event_corrected", "recon_batch_created",
    "network_validated", "network_snapshot_created",
    "dispute_opened", "dispute_resolved",
]


class AuditEntry(BaseModel):
    seq: int
    operation: AuditOperation
    occurred_at: str
    request_summary: dict
    result_summary: dict
    prev_hash: str
    record_hash: str


class AuditList(BaseModel):
    count: int
    entries: list[AuditEntry]


class AuditVerification(BaseModel):
    valid: bool
    head_seq: int | None
    head_hash: str | None
    broken_seq: int | None
    reason: str
    count: int


# ---------------------------------------------------------------------------
# Transfer network validation
# ---------------------------------------------------------------------------


class NetworkSegmentIn(BaseModel):
    line_id: str = Field(min_length=1, max_length=64)
    segment_id: str = Field(min_length=1, max_length=64)
    from_station: str = Field(min_length=1, max_length=64)
    to_station: str = Field(min_length=1, max_length=64)
    start_at: datetime   # half-open active interval [start_at, end_at), UTC-normalised
    end_at: datetime


class NetworkTransferIn(BaseModel):
    from_station: str = Field(min_length=1, max_length=64)
    to_station: str = Field(min_length=1, max_length=64)
    start_at: datetime
    end_at: datetime


class NetworkValidateIn(BaseModel):
    validation_id: str = Field(min_length=1, max_length=128)
    effective_start: datetime  # half-open UTC window [effective_start, effective_end)
    effective_end: datetime
    hubs: list[str] = Field(min_length=1, max_length=64)
    stations: list[str] = Field(min_length=1, max_length=64)
    segments: list[NetworkSegmentIn] = Field(default_factory=list)
    transfers: list[NetworkTransferIn] = Field(default_factory=list)


class NetworkSlice(BaseModel):
    index: int
    start: str
    end: str


class NetworkHop(BaseModel):
    from_station: str
    to_station: str
    edge_kind: Literal["segment", "transfer"]
    line_id: str | None = None
    segment_id: str | None = None


class NetworkConflict(BaseModel):
    code: Literal[
        "self_loop_edge",
        "overlapping_interval",
        "duplicate_edge",
        "transfer_asymmetry",
        "unreachable_station",
    ]
    slice: NetworkSlice
    message: str
    edge: dict | None = None
    edges: list[dict] | None = None
    hub: str | None = None
    station: str | None = None
    evidence_path: list[NetworkHop] | None = None


class NetworkValidateOut(BaseModel):
    validation_id: str
    status: Literal["created", "duplicate"]
    passed: bool
    effective_start: str
    effective_end: str
    slice_count: int
    slices: list[NetworkSlice]
    conflict: NetworkConflict | None


class NetworkSnapshotIn(BaseModel):
    validation_id: str = Field(min_length=1, max_length=128)


class NetworkSnapshotOut(BaseModel):
    snapshot_id: str
    status: Literal["created", "duplicate"]
    effective_start: str
    effective_end: str
    frozen_at: str
    validation: NetworkValidateOut


# ---------------------------------------------------------------------------
# Fare disputes and refund settlement
# ---------------------------------------------------------------------------


class DisputeIn(BaseModel):
    dispute_id: str = Field(min_length=1, max_length=128)
    passenger_id: str = Field(min_length=1, max_length=128)
    trip_id: str = Field(min_length=1, max_length=256)
    claimed_amount_cents: int = Field(ge=0)  # requested refund, <= snapshot receivable
    reason: str = Field(min_length=1, max_length=2000)


class DisputeResolveIn(BaseModel):
    expected_version: int = Field(ge=1)  # optimistic version the review was based on
    approved_amount_cents: int = Field(ge=0)  # may be 0 (rejected) up to the claim
    review_note: str | None = Field(default=None, max_length=2000)


class ChargeSnapshot(BaseModel):
    trip_id: str
    passenger_id: str
    amount_cents: int               # frozen receivable at dispute creation
    base_fare_cents: int
    transfer_discount_cents: int
    basis: dict
    frozen_at: str


class RefundRecord(BaseModel):
    refund_id: str
    dispute_id: str
    passenger_id: str
    trip_id: str
    amount_cents: int               # immutable, written once on resolution
    review_note: str | None
    created_at: str


class RecomputedSettlement(BaseModel):
    current_amount_cents: int       # live trip charge after event corrections
    # settled_amount_cents = frozen receivable - approved refund (net the
    # passenger actually paid). Present only for resolved disputes.
    settled_amount_cents: int | None = None
    difference_cents: int           # settled_amount - current; positive = clawback, negative = top-up
    direction: Literal["none", "top_up_due", "clawback_due"]


class DisputeOut(BaseModel):
    dispute_id: str
    status: Literal["created", "duplicate"]  # request replay marker
    state: Literal["open", "resolved"]
    version: int                    # 1 while open, 2 once resolved (optimistic)
    passenger_id: str
    trip_id: str
    claimed_amount_cents: int
    reason: str
    created_at: str
    resolved_at: str | None
    snapshot: ChargeSnapshot
    refund: RefundRecord | None
    recomputed: RecomputedSettlement


# ---------------------------------------------------------------------------
# Tap-event quality scans
# ---------------------------------------------------------------------------


class EventQualityScanIn(BaseModel):
    scan_id: str = Field(min_length=1, max_length=128)
    window_start: datetime  # half-open UTC window [window_start, window_end)
    window_end: datetime    # on the events' occurred_at
    passengers: list[str] | None = Field(
        default=None, min_length=1,
        description="Optional passenger allow-list; absent scans everyone",
    )
    duplicate_window_seconds: int = Field(
        ge=0,
        description="Adjacent same-type taps within this window are duplicates",
    )
    min_travel_seconds: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            "Directed station-pair minimum travel seconds, keyed "
            "{tap_out_station: {tap_in_station: seconds}}"
        ),
    )


class QualityEvidenceEvent(BaseModel):
    event_id: str
    passenger_id: str
    station_id: str
    tap_type: Literal["in", "out"]
    occurred_at: str
    seq: int


class QualityAlert(BaseModel):
    event_id: str                   # the triggering (later) event
    passenger_id: str
    rule: Literal["duplicate_tap", "min_travel_violation"]
    actual_interval_seconds: float  # elapsed gap between the evidence events
    threshold_seconds: int          # configured bound that was breached
    evidence: list[QualityEvidenceEvent]  # ordered chain, oldest first


class EventQualityScanOut(BaseModel):
    scan_id: str
    status: Literal["created", "duplicate"]
    window_start: str
    window_end: str
    passengers: list[str] | None    # null = all passengers scanned
    duplicate_window_seconds: int
    min_travel_seconds: dict[str, dict[str, int]]
    event_count: int
    alert_count: int
    alerts: list[QualityAlert]


# ---------------------------------------------------------------------------
# Anonymous k-anonymous trajectory exports
# ---------------------------------------------------------------------------


class PrivacyExportIn(BaseModel):
    export_id: str = Field(min_length=1, max_length=128)
    window_start: datetime  # half-open UTC window [window_start, window_end)
    window_end: datetime    # on the trips' tap-in time (started_at)
    bucket_minutes: int = Field(
        ge=1,
        description="Fixed-width UTC time bucket in minutes, epoch-anchored",
    )
    station_groups: dict[str, str] = Field(
        min_length=1,
        description=(
            "Station id -> group label; the same mapping applies to the trip's"
            " origin (from_station) and destination (to_station)"
        ),
    )
    k: int = Field(
        ge=1,
        description=(
            "Minimum distinct passengers for an equivalence class"
            " (bucket, origin group, destination group) to be retained"
        ),
    )

    @field_validator("station_groups")
    @classmethod
    def _non_empty_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        empty_stations = [station for station, group in value.items()
                          if not station or not group]
        if empty_stations:
            raise ValueError(
                "station_groups keys and values must be non-empty strings"
            )
        return value


class PrivacyExportTrip(BaseModel):
    pseudonym: str          # HMAC-SHA256 hex of passenger_id under PRIVACY_SALT
    bucket_start: str       # start of the epoch-anchored UTC bucket, ISO-8601
    origin_group: str
    destination_group: str


class PrivacyEquivalenceClass(BaseModel):
    bucket_start: str
    origin_group: str
    destination_group: str
    passenger_count: int    # distinct pseudonymised passengers in this class
    trip_count: int         # retained trips in this class


class PrivacyExportOut(BaseModel):
    export_id: str
    status: Literal["created", "duplicate"]
    window_start: str
    window_end: str
    bucket_minutes: int
    k: int
    station_groups: dict[str, str]
    input_trip_count: int
    equivalence_class_count: int
    retained_class_count: int
    suppressed_class_count: int
    retained_trip_count: int
    suppressed_trip_count: int
    classes: list[PrivacyEquivalenceClass]
    trips: list[PrivacyExportTrip]


# ---------------------------------------------------------------------------
# Disruption service replanning
# ---------------------------------------------------------------------------


class ReplanServiceStopIn(BaseModel):
    station: str = Field(min_length=1, max_length=64)
    arrival_at: datetime | None = None    # None only at the first stop
    departure_at: datetime | None = None  # None only at the last stop


class ReplanServiceIn(BaseModel):
    service_id: str = Field(min_length=1, max_length=64)
    stops: list[ReplanServiceStopIn] = Field(min_length=2)
    capacity: int = Field(ge=1, description="Rated capacity on every adjacent segment")


class ReplanGroupIn(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)
    origin: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    ready_at: datetime  # earliest UTC time the whole group can board
    party_size: int = Field(ge=1)  # the group is indivisible and rides together


class ReplanIn(BaseModel):
    replan_id: str = Field(min_length=1, max_length=128)
    min_transfer_seconds: int = Field(ge=0)
    services: list[ReplanServiceIn] = Field(min_length=1)
    groups: list[ReplanGroupIn] = Field(min_length=1)


class ReplanLeg(BaseModel):
    service_id: str
    board_station: str
    alight_station: str
    board_at: str
    alight_at: str


class ReplanStranding(BaseModel):
    code: Literal[
        "origin_not_served",
        "destination_not_served",
        "no_departure_after_ready",
        "no_feasible_connection",
        "insufficient_capacity",
    ]
    message: str


class ReplanAssignment(BaseModel):
    group_id: str
    origin: str
    destination: str
    ready_at: str
    party_size: int
    status: Literal["assigned", "stranded"]
    arrived_at: str | None
    transfers: int          # number of inter-service changes along the itinerary
    services: list[str]     # ordered service id sequence
    itinerary: list[ReplanLeg]
    stranding: ReplanStranding | None


class ReplanSegmentCapacity(BaseModel):
    from_station: str
    to_station: str
    capacity: int           # rated capacity
    remaining: int          # seats left after every assigned group was deducted


class ReplanServiceCapacity(BaseModel):
    service_id: str
    segment_capacities: list[ReplanSegmentCapacity]


class ReplanSummary(BaseModel):
    group_count: int
    assigned_count: int
    stranded_count: int
    total_passengers: int
    assigned_passengers: int
    stranded_passengers: int


class ReplanOut(BaseModel):
    replan_id: str
    status: Literal["created", "duplicate"]
    min_transfer_seconds: int
    created_at: str
    summary: ReplanSummary
    services: list[ReplanServiceCapacity]
    assignments: list[ReplanAssignment]
