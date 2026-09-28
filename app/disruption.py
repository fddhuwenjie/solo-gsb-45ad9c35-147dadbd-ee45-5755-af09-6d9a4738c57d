"""Disruption service replanning.

Given the replacement services of a disrupted line (each one an ordered list
of stops with UTC arrival/departure times and a rated segment capacity) and a
list of passenger groups (origin, destination, ready time and party size), the
planner assigns each group to the journey that arrives earliest at its
destination. A group is indivisible: it boards only when every adjacent segment
of every service leg still has enough capacity for the whole party, and once
assigned the capacity is deducted segment by segment. Groups are processed in
``(ready_at, group_id)`` order, so earlier-ready groups claim seats first.

Between two services a group may only change at a common station, and only
when the connecting service departs at least ``min_transfer_seconds`` after the
inbound service arrived. Journeys arriving at the same instant are ordered by
fewer inter-service transfers and, still tied, by the lexicographically
smaller sequence of service ids.

The search is a time-dependent earliest-arrival search over
``(service, stop index)`` states with a monotonic label order
``(time, transfers, service sequence)``; the order is a true dominance order,
so the first destination arrival popped from the priority queue is already
optimal under every tie-break. Each service may be used at most once per
journey (a train passing a station cannot be reboarded behind itself).
"""
from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .audit import canonical_json
from .settlement import iso_str, normalize_ts

# --- Inputs ---------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    station: str
    arrival: datetime | None
    departure: datetime | None


@dataclass(frozen=True)
class Service:
    service_id: str
    stops: tuple[Stop, ...]
    capacity: int
    index: dict[str, int] = field(compare=False)

    def station_at(self, i: int) -> str:
        return self.stops[i].station


@dataclass(frozen=True)
class Group:
    group_id: str
    origin: str
    destination: str
    ready_at: datetime
    party_size: int


class ValidationError(Exception):
    """Structural problem with the replan request (-> HTTP 422)."""


def validate_and_parse(body):
    """Validate the request and build normalised (UTC) domain objects.

    Structural validation covers per-service station uniqueness, endpoint
    timing requirements and the arrival/departure monotonicity along the stop
    sequence; it never touches persistence, so an invalid request never lands
    in the database.
    """
    if len({s.service_id for s in body.services}) != len(body.services):
        raise ValidationError("duplicate service_id in services")
    if len({g.group_id for g in body.groups}) != len(body.groups):
        raise ValidationError("duplicate group_id in groups")

    services: list[Service] = []
    for svc in body.services:
        stations = [stop.station for stop in svc.stops]
        if len(set(stations)) != len(stations):
            raise ValidationError(
                f"service '{svc.service_id}' visits the same station more than once"
            )
        n = len(svc.stops)
        times: list[Stop] = []
        for i, stop in enumerate(svc.stops):
            arr = None if stop.arrival_at is None else normalize_ts(stop.arrival_at)
            dep = None if stop.departure_at is None else normalize_ts(stop.departure_at)
            # Endpoint timing: the first stop must have a departure, the last
            # an arrival; every non-last stop needs a departure and every
            # non-first stop an arrival.
            if i == 0 and dep is None:
                raise ValidationError(
                    f"service '{svc.service_id}' first stop"
                    f" '{stop.station}' must have a departure_at"
                )
            if i == n - 1 and arr is None:
                raise ValidationError(
                    f"service '{svc.service_id}' last stop"
                    f" '{stop.station}' must have an arrival_at"
                )
            if i < n - 1 and dep is None:
                raise ValidationError(
                    f"service '{svc.service_id}' non-final stop"
                    f" '{stop.station}' must have a departure_at"
                )
            if i > 0 and arr is None:
                raise ValidationError(
                    f"service '{svc.service_id}' non-first stop"
                    f" '{stop.station}' must have an arrival_at"
                )
            # Dwell time at one station cannot be negative.
            if arr is not None and dep is not None and dep < arr:
                raise ValidationError(
                    f"service '{svc.service_id}' departs '{stop.station}' before"
                    " it arrives there (departure_at must be >= arrival_at)"
                )
            # Adjacent stops: the arrival here may not precede the previous
            # stop's departure. Equal timestamps (zero run/dwell time) are
            # legal; time must never move backwards along the sequence.
            if i > 0 and arr < times[i - 1].departure:
                raise ValidationError(
                    f"service '{svc.service_id}' arrival/departure times are not"
                    " monotonically non-decreasing along the stop sequence"
                    f" (arrival at '{stop.station}' precedes the departure at"
                    f" '{times[i - 1].station}')"
                )
            times.append(Stop(stop.station, arr, dep))

        services.append(
            Service(
                service_id=svc.service_id,
                stops=tuple(times),
                capacity=svc.capacity,
                index={st: i for i, st in enumerate(stations)},
            )
        )

    groups = [
        Group(
            group_id=g.group_id,
            origin=g.origin,
            destination=g.destination,
            ready_at=normalize_ts(g.ready_at),
            party_size=g.party_size,
        )
        for g in body.groups
    ]
    for g in groups:
        if g.origin == g.destination:
            raise ValidationError(
                f"group '{g.group_id}' origin and destination must differ"
            )
    # Groups are processed in (ready_at, group_id) order.
    groups.sort(key=lambda g: (g.ready_at, g.group_id))
    # Keep the service ordering input-independent: service id order.
    services.sort(key=lambda s: s.service_id)
    return services, groups


# --- Capacity state -------------------------------------------------------


@dataclass
class Capacity:
    """Remaining seats on each adjacent segment of each service."""

    remaining: dict[str, list[int]]

    @classmethod
    def for_services(cls, services: list[Service]) -> "Capacity":
        return cls(
            {s.service_id: [s.capacity] * (len(s.stops) - 1) for s in services}
        )

    def fits(self, svc: Service, board_i: int, alight_i: int, party: int) -> bool:
        seg = self.remaining[svc.service_id]
        return all(seg[k] >= party for k in range(board_i, alight_i))

    def deduct(self, svc: Service, board_i: int, alight_i: int, party: int) -> None:
        seg = self.remaining[svc.service_id]
        for k in range(board_i, alight_i):
            seg[k] -= party


# --- Search ---------------------------------------------------------------


@dataclass(frozen=True)
class _Leg:
    service: Service
    board_i: int
    alight_i: int

    def as_dict(self) -> dict:
        return {
            "service_id": self.service.service_id,
            "board_station": self.service.station_at(self.board_i),
            "alight_station": self.service.station_at(self.alight_i),
            "board_at": iso_str(self.service.stops[self.board_i].departure),
            "alight_at": iso_str(self.service.stops[self.alight_i].arrival),
        }


def _dominates(label_a: tuple, label_b: tuple) -> bool:
    """Label dominance at the same physical state.

    A label is ``(time, transfers, svc_tuple)``. A dominates B when it can
    reproduce every continuation B has -- earlier/equal time and a used-service
    set that is a subset, so every service B can still board remains available
    to A -- *and* its service sequence is lexicographically no larger, so B
    could not win the equal-arrival tie-break either. Identical tuples are the
    same label (``a == b`` also dominates: the push is simply de-duplicated).
    """
    ta, _ra, sa = label_a
    tb, _rb, sb = label_b
    if ta > tb:
        return False
    if not set(sa).issubset(set(sb)):
        return False
    return sa <= sb


def _find_journey(
    group: Group,
    services: list[Service],
    capacity: Capacity | None,
    min_transfer: timedelta,
):
    """Earliest-arrival journey for one group.

    ``capacity`` of ``None`` runs the capacity-ignorant search, used purely to
    distinguish "no journey exists at all" from "capacity is exhausted".

    This is a label-setting, time-dependent earliest-arrival search over
    ``(service, stop index, kind)`` states where ``kind`` 0 means "just
    alighted" and 1 means "aboard, about to ride". Labels are
    ``(time, transfers, used-service tuple)``; edges never move time backwards
    (FIFO), so the first non-dominated destination arrival popped from the
    heap is globally optimal under the tie-break
    ``(arrival time, transfers, service sequence)``. Each service is used at
    most once per journey: a train cannot be reboarded behind itself.
    """
    served = set()
    for svc in services:
        served.update(svc.index)
    if group.origin not in served:
        return "origin_not_served"
    if group.destination not in served:
        return "destination_not_served"

    ready = group.ready_at
    party = group.party_size
    counter = 0
    heap: list = []
    # Pareto bag of non-dominated labels per physical state.
    bags: dict[tuple, list[tuple]] = {}

    def push(time, transfers, svc_tuple, kind, si, stop_i, legs):
        nonlocal counter
        label = (time, transfers, svc_tuple)
        key = (si, stop_i, kind)
        bag = bags.setdefault(key, [])
        if any(_dominates(other, label) for other in bag):
            return
        # The new label supersedes some previously viable ones.
        bags[key] = [other for other in bag if not _dominates(label, other)]
        bags[key].append(label)
        heapq.heappush(
            heap,
            (time, transfers, svc_tuple, kind, si, stop_i, counter, legs),
        )
        counter += 1

    # Initial boardings: any service departing the origin at/after ready.
    for si, svc in enumerate(services):
        oi = svc.index.get(group.origin)
        if oi is None:
            continue
        dep = svc.stops[oi].departure
        if dep is None or oi == len(svc.stops) - 1:
            continue
        if dep < ready:
            continue
        if capacity is not None:
            # Feasibility of boarding: the first adjacent segment must have
            # room; later segments are checked as the ride expands.
            if not capacity.fits(svc, oi, oi + 1, party):
                continue
        push(dep, 0, (svc.service_id,), 1, si, oi, ())

    while heap:
        time, transfers, svc_tuple, kind, si, stop_i, _, legs = heapq.heappop(heap)
        label = (time, transfers, svc_tuple)
        if label not in bags.get((si, stop_i, kind), []):
            continue  # pruned after this heap entry was stamped
        svc = services[si]

        if kind == 0:
            # Arrival label: the group has alighted here.
            if svc.station_at(stop_i) == group.destination:
                return legs
            # Transfer: board another, never-before-used service at this stop.
            for tj, other in enumerate(services):
                if tj == si or other.service_id in svc_tuple:
                    continue
                bj = other.index.get(svc.station_at(stop_i))
                if bj is None or bj == len(other.stops) - 1:
                    continue
                dep = other.stops[bj].departure
                if dep is None:
                    continue
                if dep < time + min_transfer:
                    continue
                if capacity is not None and not capacity.fits(
                    other, bj, bj + 1, party
                ):
                    continue
                push(
                    dep,
                    transfers + 1,
                    svc_tuple + (other.service_id,),
                    1,
                    tj,
                    bj,
                    legs,
                )
            # Stay on board: continue riding this service.
            push(time, transfers, svc_tuple, 1, si, stop_i, legs)
        else:
            # Boarding label at stop_i: ride one adjacent segment forward.
            if stop_i + 1 >= len(svc.stops):
                continue
            j = stop_i + 1
            if capacity is not None and not capacity.fits(svc, j - 1, j, party):
                continue
            arr = svc.stops[j].arrival
            current_leg = legs[-1] if legs and legs[-1][0] == si else None
            if current_leg is not None:
                new_legs = legs[:-1] + ((si, current_leg[1], j),)
            else:
                new_legs = legs + ((si, stop_i, j),)
            # Alight here; "stay on board" is reached by expanding the arrival
            # label into the boarding label at this same stop.
            push(arr, transfers, svc_tuple, 0, si, j, new_legs)

    return None


# --- Orchestration --------------------------------------------------------


_STRANDING_MESSAGES = {
    "origin_not_served": "origin is not served by any replacement service",
    "destination_not_served": (
        "destination is not served by any replacement service"
    ),
    "no_departure_after_ready": (
        "no replacement service reaches the destination after the group is ready"
    ),
    "no_feasible_connection": (
        "no sequence of replacement services connects origin to destination"
        " within the minimum transfer time"
    ),
    "insufficient_capacity": (
        "the group cannot be carried as a whole: every connecting journey is"
        " full on at least one adjacent segment"
    ),
}


def _assignment(group: Group, status: str, **kw) -> dict:
    base = {
        "group_id": group.group_id,
        "origin": group.origin,
        "destination": group.destination,
        "ready_at": iso_str(group.ready_at),
        "party_size": group.party_size,
        "status": status,
    }
    base.update(kw)
    return base


def plan_replan(
    services: list[Service], groups: list[Group], min_transfer_seconds: int
) -> dict:
    """Assign every group in processing order and return the frozen result."""
    min_transfer = timedelta(seconds=min_transfer_seconds)
    capacity = Capacity.for_services(services)
    assignments: list[dict] = []
    assigned_passengers = 0
    stranded_passengers = 0

    for group in groups:
        outcome = _find_journey(group, services, capacity, min_transfer)

        if outcome in ("origin_not_served", "destination_not_served"):
            code = outcome
        elif outcome is None:
            # Capacity-ignorant search explains *why* nothing was found.
            free = _find_journey(group, services, None, min_transfer)
            if free in ("origin_not_served", "destination_not_served"):
                code = free
            elif free is None:
                # Distinguish "the network itself offers nothing at/after
                # ready" from "a journey exists but only earlier".
                any_time = _find_journey(
                    Group(
                        group.group_id,
                        group.origin,
                        group.destination,
                        datetime.fromtimestamp(0, tz=timezone.utc),
                        group.party_size,
                    ),
                    services,
                    None,
                    min_transfer,
                )
                code = (
                    "no_feasible_connection"
                    if any_time is None
                    else "no_departure_after_ready"
                )
            else:
                code = "insufficient_capacity"
        else:
            code = None

        if code is not None:
            stranded_passengers += group.party_size
            assignments.append(
                _assignment(
                    group,
                    "stranded",
                    arrived_at=None,
                    transfers=0,
                    services=[],
                    itinerary=[],
                    stranding={
                        "code": code,
                        "message": _STRANDING_MESSAGES[code],
                    },
                )
            )
            continue

        raw_legs = outcome
        legs = [
            _Leg(
                service=services[si],
                board_i=board_i,
                alight_i=alight_i,
            )
            for si, board_i, alight_i in raw_legs
        ]
        for leg in legs:
            capacity.deduct(leg.service, leg.board_i, leg.alight_i, group.party_size)
        assigned_passengers += group.party_size
        service_ids = [
            services[si].service_id for si, _, _ in raw_legs
        ]
        assignments.append(
            _assignment(
                group,
                "assigned",
                arrived_at=iso_str(
                    services[raw_legs[-1][0]]
                    .stops[raw_legs[-1][2]]
                    .arrival
                ),
                transfers=len(legs) - 1,
                services=service_ids,
                itinerary=[leg.as_dict() for leg in legs],
                stranding=None,
            )
        )

    service_view = []
    for svc in services:
        segs = capacity.remaining[svc.service_id]
        service_view.append(
            {
                "service_id": svc.service_id,
                "segment_capacities": [
                    {
                        "from_station": svc.stops[i].station,
                        "to_station": svc.stops[i + 1].station,
                        "capacity": svc.capacity,
                        "remaining": segs[i],
                    }
                    for i in range(len(svc.stops) - 1)
                ],
            }
        )

    total_passengers = sum(g.party_size for g in groups)
    return {
        "min_transfer_seconds": min_transfer_seconds,
        "summary": {
            "group_count": len(groups),
            "assigned_count": sum(
                1 for a in assignments if a["status"] == "assigned"
            ),
            "stranded_count": sum(
                1 for a in assignments if a["status"] == "stranded"
            ),
            "total_passengers": total_passengers,
            "assigned_passengers": assigned_passengers,
            "stranded_passengers": stranded_passengers,
        },
        "services": service_view,
        "assignments": assignments,
    }


# --- Canonical payload ----------------------------------------------------


def canonical_replan_payload(body, services, groups) -> dict:
    """Canonical request: timestamps normalised to UTC, services sorted by
    service id (stop order preserved), groups sorted by (ready_at, group_id),
    so semantically identical submissions hash identically regardless of
    array order or offset notation."""
    svc_by_id = {s.service_id: s for s in services}
    return {
        "replan_id": body.replan_id,
        "min_transfer_seconds": body.min_transfer_seconds,
        "services": [
            {
                "service_id": sid,
                "capacity": svc_by_id[sid].capacity,
                "stops": [
                    {
                        "station": stop.station,
                        "arrival_at": (
                            iso_str(stop.arrival) if stop.arrival is not None else None
                        ),
                        "departure_at": (
                            iso_str(stop.departure)
                            if stop.departure is not None
                            else None
                        ),
                    }
                    for stop in svc_by_id[sid].stops
                ],
            }
            for sid in sorted(svc_by_id)
        ],
        "groups": [
            {
                "group_id": g.group_id,
                "origin": g.origin,
                "destination": g.destination,
                "ready_at": iso_str(g.ready_at),
                "party_size": g.party_size,
            }
            for g in groups
        ],
    }


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
