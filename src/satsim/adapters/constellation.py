"""Default :class:`~satsim.ports.constellation.ConstellationClient` fakes.

One ``FakeConstellation`` per fleet. Capacity is deterministic and step-aware:

* before ``online_at_step`` the fleet is not yet in service (empty snapshot) — this is how
  the *capacity-expansion mid-run* scenario is driven;
* ``fail_from_step`` makes the client start raising :class:`ConstellationError`, driving
  the circuit-breaker / fleet-failover scenario.

Beam *coverage* (which region/band each beam illuminates) is derived from the regulatory
table so beams only appear where the fleet is actually licensed — keeping the fakes and the
regulatory policy mutually consistent.
"""

from __future__ import annotations

from satsim.config import SimulationConfig
from satsim.domain.enums import Band, FleetId, Region
from satsim.domain.models import Beam, ConstellationSnapshot, Satellite
from satsim.ports.constellation import ConstellationError


class FakeConstellation:
    """A deterministic, license-free constellation client for one fleet."""

    def __init__(
        self,
        fleet: FleetId,
        num_satellites: int,
        beams_per_satellite: int,
        capacity_per_beam: float,
        coverage: tuple[tuple[Region, Band], ...],
        online_at_step: int = 0,
        fail_from_step: int | None = None,
    ) -> None:
        self._fleet = fleet
        self._num_satellites = num_satellites
        self._beams_per_satellite = beams_per_satellite
        self._capacity_per_beam = capacity_per_beam
        self._coverage = coverage
        self._online_at_step = online_at_step
        self._fail_from_step = fail_from_step

    def fleet_id(self) -> FleetId:
        return self._fleet

    def visible_resources(self, step: int) -> ConstellationSnapshot:
        if self._fail_from_step is not None and step >= self._fail_from_step:
            raise ConstellationError(f"{self._fleet.value} unreachable at step {step}")
        if step < self._online_at_step or not self._coverage:
            return ConstellationSnapshot(step=step)

        satellites: list[Satellite] = []
        for i in range(self._num_satellites):
            sat_id = f"{self._fleet.value}-s{i}"
            beams: list[Beam] = []
            for j in range(self._beams_per_satellite):
                idx = (i * self._beams_per_satellite + j) % len(self._coverage)
                region, band = self._coverage[idx]
                beams.append(
                    Beam(
                        beam_id=f"{sat_id}-b{j}",
                        fleet=self._fleet,
                        satellite_id=sat_id,
                        region=region,
                        band=band,
                        capacity_units=self._capacity_per_beam,
                    )
                )
            satellites.append(Satellite(satellite_id=sat_id, fleet=self._fleet, beams=tuple(beams)))
        return ConstellationSnapshot(step=step, satellites=tuple(satellites))


def _coverage_for_fleet(
    config: SimulationConfig, fleet: FleetId
) -> tuple[tuple[Region, Band], ...]:
    """Region/band pairs where ``fleet`` is licensed, from the regulatory table."""
    pairs: list[tuple[Region, Band]] = []
    for rule in config.region_rules:
        if fleet not in rule.allowed_fleets:
            continue
        pairs.extend((rule.region, band) for band in rule.allowed_bands)
    return tuple(pairs)


def build_fake_constellations(config: SimulationConfig) -> tuple[FakeConstellation, ...]:
    """Build one :class:`FakeConstellation` per configured fleet."""
    return tuple(
        FakeConstellation(
            fleet=fc.fleet,
            num_satellites=fc.num_satellites,
            beams_per_satellite=fc.beams_per_satellite,
            capacity_per_beam=fc.capacity_per_beam,
            coverage=_coverage_for_fleet(config, fc.fleet),
            online_at_step=fc.online_at_step,
            fail_from_step=fc.fail_from_step,
        )
        for fc in config.fleets
    )
