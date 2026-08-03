"""C1-C3 against real recorded Shippo quotes.

The fixture is 154 quotes really returned by the live API for one lane. It is
a *recording*, not a rate table: nothing in these tests is a number anyone
chose, which is the property the live-rates rework existed to establish.
"""

from datetime import date
from pathlib import Path

import pytest

from bbq_shipment_agent.planning import (
    BOXES,
    MAX_ARRIVAL_TEMP_C,
    MAX_GEL_PACKS,
    Address,
    BoxSize,
    Lane,
    LumpedCapacitanceModel,
    QuotingUnavailable,
    RecordedQuoter,
    ShipDay,
    ShipDayError,
    define_load,
    enumerate_configurations,
    evaluate_configurations,
    heaviest_variant,
    parcel_variants,
    smallest_fitting_box,
    ship_day_for,
    thermal_gate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shippo-quotes-sf-dc.json"
ORIGIN = Address("BBQ Kitchen", "64 Divisadero St", "San Francisco", "CA", "94117")
DEST = Address("Recipient One", "1600 Pennsylvania Ave NW", "Washington", "DC", "20500")

SATURDAY = date(2026, 8, 8)
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
WEDNESDAY = date(2026, 8, 12)
ALL_DAYS = (SATURDAY, MONDAY, TUESDAY)


@pytest.fixture
def quoter():
    return RecordedQuoter.from_file(FIXTURE)


@pytest.fixture
def load():
    return define_load("r0")


def enumerate_all(load, quoter, dates=ALL_DAYS):
    return enumerate_configurations(load, ORIGIN, DEST, dates, quoter)


class TestShipDays:
    def test_it_classifies_the_three_shippable_days(self):
        assert ship_day_for(SATURDAY) is ShipDay.SATURDAY
        assert ship_day_for(MONDAY) is ShipDay.MONDAY
        assert ship_day_for(TUESDAY) is ShipDay.TUESDAY

    def test_a_non_shipping_day_is_refused_not_skipped(self):
        with pytest.raises(ShipDayError, match="Wednesday"):
            ship_day_for(WEDNESDAY)


class TestBoxGeometry:
    def test_the_larger_box_leaks_faster(self):
        # Design 5's central claim at 1.5 lb: more surface area is strictly
        # worse, with no compensating ballast because the mass is not there.
        assert BOXES[BoxSize.LARGE].ua_w_k > BOXES[BoxSize.SMALL].ua_w_k

    def test_ua_is_computed_from_geometry_not_stored(self):
        box = BOXES[BoxSize.SMALL]
        assert box.ua_w_k == pytest.approx(
            box.conductivity_w_mk * box.surface_area_m2 / box.wall_m
        )

    def test_thicker_walls_barely_help(self):
        # Recorded because it is counterintuitive and someone will try it: at
        # a fixed inner cavity, wall thickness grows outer surface area almost
        # as fast as it grows resistance, so UA hardly moves.
        from dataclasses import replace

        thin = BOXES[BoxSize.SMALL]
        thick = replace(thin, wall_m=thin.wall_m + 0.02)
        assert thick.ua_w_k > thin.ua_w_k * 0.9  # <10% better despite +40% wall
        assert thick.volume_cm3 > thin.volume_cm3 * 1.3  # but much bulkier


class TestParcelVariants:
    def test_it_enumerates_gel_counts_for_one_box(self, load):
        # Gel count is a real tradeoff and is crossed exhaustively. Box size
        # is not: design 5's larger box is dominated on cost and on thermal
        # margin at once, so quoting it buys nothing.
        variants = parcel_variants(load)
        assert len(variants) == MAX_GEL_PACKS + 1

    def test_only_the_smallest_fitting_box_is_offered(self, load):
        assert smallest_fitting_box(load) is BOXES[BoxSize.SMALL]
        assert {v.box_size for v in parcel_variants(load)} == {BoxSize.SMALL}

    def test_a_load_too_big_for_the_small_box_takes_the_larger_one(self, load):
        # The rule is "smallest box that fits", not "always the small box".
        # A load that outgrows the small cavity still gets quoted.
        from dataclasses import replace

        bulky = replace(load, dimensions_m=(0.2, 0.2, 0.2))
        assert not bulky.fits_in(BOXES[BoxSize.SMALL])
        assert smallest_fitting_box(bulky) is BOXES[BoxSize.LARGE]
        assert {v.box_size for v in parcel_variants(bulky)} == {BoxSize.LARGE}

    def test_a_load_that_fits_nothing_enumerates_nothing(self, load):
        from dataclasses import replace

        enormous = replace(load, dimensions_m=(2.0, 2.0, 2.0))
        assert smallest_fitting_box(enormous) is None
        assert parcel_variants(enormous) == ()

    def test_gel_packs_only_change_weight(self, load):
        small = [v for v in parcel_variants(load) if v.box_size is BoxSize.SMALL]
        assert len({(v.length_cm, v.width_cm, v.height_cm) for v in small}) == 1
        assert len({v.weight_kg for v in small}) == len(small)

    def test_the_pin_reference_is_the_bulkiest_parcel(self, load):
        variants = parcel_variants(load)
        heaviest = heaviest_variant(variants)
        # A carrier that takes the worst case takes the rest; pinning off the
        # smallest can turn a genuine restriction into a hard failure. With one
        # box in play the worst case is its fullest gel load.
        assert heaviest.box_size is BoxSize.SMALL
        assert heaviest.gel_packs == MAX_GEL_PACKS


class TestEnumeration:
    def test_carriers_are_discovered_not_declared(self, load, quoter):
        # The whole point of the rework: the carrier set comes back as an
        # answer rather than going in as an assumption.
        enumeration = enumerate_all(load, quoter)
        assert enumeration.carriers <= enumeration.pinned_carriers
        assert enumeration.carriers  # something really quoted

    def test_every_configuration_carries_its_own_quote(self, load, quoter):
        for configuration in enumerate_all(load, quoter).configurations:
            assert configuration.cost > 0
            assert configuration.service_name

    def test_transit_comes_from_the_carrier_not_a_constant(self, load, quoter):
        days = {c.transit_days for c in enumerate_all(load, quoter).configurations}
        # Real services land on more than the optimistic 1/2/3 that was
        # originally assumed.
        assert len(days) >= 4

    def test_business_day_estimates_become_elapsed_days(self, load, quoter):
        # UPS quotes "1-5 business days"; USPS quotes "delivery in 2 to 5
        # days". Treating both as calendar understated UPS transit whenever
        # the journey crossed a weekend -- and C3 gates on this as elapsed
        # time, so it was asking the gel packs to last longer than it thought.
        stretched = {
            (c.carrier, c.service_name, c.transit_days, c.elapsed_transit_days)
            for c in enumerate_all(load, quoter).configurations
            if c.transit_days != c.elapsed_transit_days
        }
        assert ("UPS", "Ground", 5, 7) in stretched

    def test_a_carrier_that_states_no_terms_is_read_as_calendar(self, load, quoter):
        # Known gap, and the reason it is tolerable: UPS Ground Saver comes
        # back with an empty duration_terms, so it is treated as calendar days
        # and its transit is understated by two. It never clears the thermal
        # gate at six days, so nothing currently rides on it -- but a carrier
        # that quotes business days silently would be a real hole.
        quiet = [
            c
            for c in enumerate_all(load, quoter).configurations
            if not c.quote.duration_terms
        ]
        assert quiet
        assert all(c.transit_days == c.elapsed_transit_days for c in quiet)

    def test_saturday_is_usps_only(self, load, quoter):
        saturday = [
            c
            for c in enumerate_all(load, quoter).configurations
            if c.ship_date == SATURDAY
        ]
        assert saturday
        assert {c.carrier for c in saturday} == {"USPS"}

    def test_weekdays_keep_every_quoted_carrier(self, load, quoter):
        enumeration = enumerate_all(load, quoter)
        weekday = {c.carrier for c in enumeration.configurations if c.ship_date == MONDAY}
        assert weekday == enumeration.carriers

    def test_a_non_shipping_date_is_refused(self, load, quoter):
        with pytest.raises(ShipDayError):
            enumerate_all(load, quoter, (MONDAY, WEDNESDAY))

    def test_no_dates_is_an_error(self, load, quoter):
        with pytest.raises(ValueError, match="no candidate ship dates"):
            enumerate_all(load, quoter, ())

    def test_carrier_messages_are_carried_out_of_the_stage(self, load, quoter):
        # Without these, a carrier silently missing is indistinguishable from
        # one with no service -- which is how a rate limiter passed for a
        # service restriction until it was looked for.
        assert isinstance(enumerate_all(load, quoter).messages, tuple)

    def test_an_unrecorded_lane_raises_rather_than_inventing(self, load, quoter):
        elsewhere = Address("Nobody", "1 Nowhere Rd", "Fargo", "ND", "58102")
        with pytest.raises(QuotingUnavailable, match="no recorded quotes"):
            enumerate_configurations(load, ORIGIN, elsewhere, (MONDAY,), quoter)


class TestThermalGate:
    def test_the_threshold_is_the_documented_constant(self):
        assert MAX_ARRIVAL_TEMP_C == 4.4

    def test_it_admits_some_and_refuses_most(self, load, quoter):
        configurations = enumerate_all(load, quoter).configurations
        feasible = thermal_gate(load, configurations)
        assert 0 < len(feasible) < len(configurations)

    def test_zero_gel_packs_never_survives(self, load, quoter):
        feasible = thermal_gate(load, enumerate_all(load, quoter).configurations)
        assert all(f.configuration.gel_packs > 0 for f in feasible)

    def test_more_gel_packs_never_arrives_warmer(self, load, quoter):
        # Monotonicity should survive recalibration; no constant should.
        model = LumpedCapacitanceModel()
        configurations = enumerate_all(load, quoter, (MONDAY,)).configurations
        service = max(
            configurations, key=lambda c: c.cost
        ).service_name  # any single real service
        subset = [
            c
            for c in configurations
            if c.service_name == service and c.box_size is BoxSize.SMALL
        ]
        by_gel = {c.gel_packs: c for c in subset}
        temps = [
            model.predict_arrival_temp_c(load, by_gel[n], Lane("l", 22.0))
            for n in sorted(by_gel)
        ]
        assert temps == sorted(temps, reverse=True)

    def test_the_larger_box_is_never_thermally_better(self, load, quoter):
        # A property of the boxes, not of what C2 happens to enumerate. C2 now
        # quotes only the smallest box that fits, so pairing enumerated
        # configurations by box size would compare nothing and pass vacuously.
        # Holding the service and the transit estimate fixed and swapping the
        # box isolates the geometry, which is what the claim is about.
        from dataclasses import replace

        model = LumpedCapacitanceModel()
        lane = Lane("l", 22.0)
        configurations = enumerate_all(load, quoter, (MONDAY,)).configurations
        compared = 0
        for configuration in configurations:
            larger = replace(configuration, box_size=BoxSize.LARGE)
            compared += 1
            assert model.predict_arrival_temp_c(
                load, larger, lane
            ) >= model.predict_arrival_temp_c(load, configuration, lane)
        assert compared, "nothing was enumerated; comparison was vacuous"

    def test_a_warmer_lane_is_never_easier(self, load, quoter):
        configurations = enumerate_all(load, quoter).configurations
        cool = thermal_gate(load, configurations, Lane("cool", 10.0))
        warm = thermal_gate(load, configurations, Lane("warm", 35.0))
        assert len(cool) >= len(warm)

    def test_a_full_gel_load_survives_extreme_heat_overnight(self, load, quoter):
        # Worth pinning because it is the reason design 3 can use gel packs at
        # all rather than dry ice: six packs carry enough latent budget to
        # hold ~26 hours even against 60C ambient, so overnight service still
        # clears. The refrigerant, not the insulation, is doing the work.
        feasible = thermal_gate(
            load, enumerate_all(load, quoter).configurations, Lane("desert", 60.0)
        )
        assert feasible
        assert {f.configuration.gel_packs for f in feasible} == {MAX_GEL_PACKS}
        assert all(f.configuration.transit_days == 1 for f in feasible)

    def test_a_hot_enough_lane_gates_everything_out(self, load, quoter):
        # C3 comes back empty sometimes; design 4 routes that to C4 rather
        # than treating it as an error. Past ~66C ambient the latent budget
        # cannot cover even one day.
        configurations = enumerate_all(load, quoter).configurations
        assert thermal_gate(load, configurations, Lane("furnace", 80.0)) == ()

    def test_margin_is_headroom_below_the_threshold(self, load, quoter):
        configurations = enumerate_all(load, quoter, (MONDAY,)).configurations
        for evaluated in evaluate_configurations(load, configurations):
            assert evaluated.thermal_margin_c == pytest.approx(
                MAX_ARRIVAL_TEMP_C - evaluated.predicted_arrival_temp_c
            )
            assert evaluated.feasible == (evaluated.thermal_margin_c >= 0)

    def test_margins_actually_vary(self, load, quoter):
        # This inverts an earlier test. With the old invented transit times
        # every feasible margin was identical, leaving D1's thin-margin check
        # and C5's minimum-margin ranking with nothing to discriminate on.
        # Against real transit estimates the field carries information.
        feasible = thermal_gate(load, enumerate_all(load, quoter).configurations)
        margins = {round(f.thermal_margin_c, 2) for f in feasible}
        assert len(margins) > 1, "margin has no variance; D1 has nothing to check"

    def test_a_model_cannot_widen_the_threshold(self, load, quoter):
        class JustOverTheLine:
            def predict_arrival_temp_c(self, load, configuration, lane):
                return MAX_ARRIVAL_TEMP_C + 0.01

        configurations = enumerate_all(load, quoter).configurations
        assert thermal_gate(load, configurations, model=JustOverTheLine()) == ()


class TestSplittingDoesNotHelp:
    """Design 4 used to offer C4 a 'split the shipment' move. It is dead.

    Kept as a test rather than only a note, because the claim is unintuitive
    and someone will propose it again.
    """

    def _arrival(self, load, gel, days, ambient):
        from dataclasses import replace as _replace

        from bbq_shipment_agent.planning import ParcelSpec, Quote
        from bbq_shipment_agent.planning.configurations import Configuration

        parcel = ParcelSpec.build(load, BOXES[BoxSize.SMALL], gel)
        quote = Quote(
            carrier="X", service_name="s", service_token="s", amount=1.0,
            currency="USD", estimated_days=days,
            duration_terms="Delivery in N days.", parcel=parcel,
        )
        configuration = Configuration(
            box_size=BoxSize.SMALL, gel_packs=gel, ship_date=MONDAY, quote=quote
        )
        return LumpedCapacitanceModel().predict_arrival_temp_c(
            load, configuration, Lane("l", ambient)
        )

    def test_a_split_parcel_never_arrives_colder(self, load):
        from dataclasses import replace as _replace

        for ambient in (27.0, 32.0, 36.0, 40.0):
            for days in (1, 2, 3, 4):
                whole = self._arrival(load, 6, days, ambient)
                half = self._arrival(
                    _replace(load, mass_kg=load.mass_kg / 2), 6, days, ambient
                )
                assert half >= whole - 1e-9, (ambient, days, whole, half)

    def test_and_is_strictly_worse_where_it_matters(self, load):
        # Not merely no better: at the margin where a remediation would be
        # attempted, halving the ballast costs two degrees.
        from dataclasses import replace as _replace

        whole = self._arrival(load, 6, 2, 36.0)
        half = self._arrival(_replace(load, mass_kg=load.mass_kg / 2), 6, 2, 36.0)
        assert half > whole + 1.0

    def test_because_hold_time_does_not_depend_on_the_load(self, load):
        # The reason, pinned separately from the symptom: hold time is
        # latent_budget / leak_rate, and neither term contains product mass.
        # Splitting leaves it identical and removes ballast, so it can only
        # hurt -- section 5's own claim, applied to the split.
        from dataclasses import replace as _replace

        light = _replace(load, mass_kg=load.mass_kg / 8)
        # Deep into gel exhaustion, both converge on ambient from the same
        # hold time; the lighter load simply gets there sooner.
        assert self._arrival(light, 6, 5, 32.0) >= self._arrival(load, 6, 5, 32.0)
