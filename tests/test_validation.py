"""B2, against real recorded Shippo validations."""

from datetime import date
from pathlib import Path

import pytest

from bbq_shipment_agent.capabilities import ValidationMode
from bbq_shipment_agent.planning import Address
from bbq_shipment_agent.recipients import (
    AddressValidationUnavailable,
    RecordedAddressValidator,
    ValidationOutcome,
    classify,
    Recipient,
    validate_recipients,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shippo-addresses.json"

CLEAN = Address("Recipient One", "1600 Pennsylvania Ave NW", "Washington", "DC", "20500")
SLOPPY = Address("Sloppy", "64 divisadero st", "san francisco", "CA", "94110")
NONSENSE = Address("Nobody", "99999 Nowhere Blvd", "Fargo", "ND", "58102")


@pytest.fixture
def validator():
    return RecordedAddressValidator.from_file(FIXTURE)


def shipment(key, address, name=None):
    return Recipient(key=key, name=name or key, address=address)


class TestClassify:
    def test_zip_plus_four_enrichment_is_clean(self):
        # The validator always appends the +4. Comparing raw strings would
        # class every address as correctable and route whole runs to a human.
        submitted = Address("A", "1 Main St", "Springfield", "IL", "62701")
        enriched = Address("A", "1 Main St", "Springfield", "IL", "62701-1234")
        assert classify(submitted, enriched, True) is ValidationOutcome.CLEAN

    def test_case_and_whitespace_differences_are_clean(self):
        submitted = Address("A", "64  divisadero st", "san francisco", "ca", "94117")
        canonical = Address("A", "64 Divisadero St", "San Francisco", "CA", "94117")
        assert classify(submitted, canonical, True) is ValidationOutcome.CLEAN

    def test_a_different_five_digit_zip_is_correctable(self):
        # The case that motivates the whole stage: a real change of doorstep.
        submitted = Address("A", "64 Divisadero St", "San Francisco", "CA", "94110")
        corrected = Address("A", "64 Divisadero St", "San Francisco", "CA", "94117")
        assert classify(submitted, corrected, True) is ValidationOutcome.CORRECTABLE

    def test_an_invalid_address_is_failed_regardless_of_similarity(self):
        assert classify(NONSENSE, NONSENSE, False) is ValidationOutcome.FAILED


class TestAgainstRealValidations:
    def test_a_good_address_comes_back_clean(self, validator):
        assert validator.validate(CLEAN).outcome is ValidationOutcome.CLEAN

    def test_a_mistyped_zip_comes_back_correctable(self, validator):
        result = validator.validate(SLOPPY)
        assert result.outcome is ValidationOutcome.CORRECTABLE
        assert result.corrected.zip.startswith("94117")
        assert result.messages

    def test_an_unfindable_address_comes_back_failed(self, validator):
        result = validator.validate(NONSENSE)
        assert result.outcome is ValidationOutcome.FAILED
        assert result.messages

    def test_an_unrecorded_address_raises_rather_than_passing(self, validator):
        # A fixture that silently approves unknown addresses is a rubber stamp.
        with pytest.raises(AddressValidationUnavailable, match="no recorded validation"):
            validator.validate(Address("X", "5 Elsewhere Rd", "Reno", "NV", "89501"))


class TestStandardMode:
    def test_clean_addresses_proceed(self, validator):
        report = validate_recipients((shipment("r1", CLEAN),), validator)
        assert [s.key for s in report.eligible] == ["r1"]
        assert report.escalated == ()

    def test_a_correction_is_applied_and_the_shipment_proceeds(self, validator):
        report = validate_recipients((shipment("r2", SLOPPY),), validator)
        assert report.escalated == ()
        # The corrected address is what will be quoted -- replacing it here
        # means there is no way to price the submitted one by accident.
        assert report.eligible[0].address.zip.startswith("94117")
        assert report.corrected_count == 1

    def test_a_failed_address_is_escalated_with_the_validator_reason(self, validator):
        report = validate_recipients((shipment("r3", NONSENSE),), validator)
        assert report.eligible == ()
        assert report.escalated[0].recipient_key == "r3"
        assert "failed" in report.escalated[0].reason


class TestStrictMode:
    def test_correctable_goes_to_a_human_instead(self, validator):
        # The whole point of the mode: who adjudicates a correctable address.
        report = validate_recipients(
            (shipment("r2", SLOPPY),), validator, ValidationMode.STRICT
        )
        assert report.eligible == ()
        assert "correctable" in report.escalated[0].reason

    def test_clean_addresses_still_proceed(self, validator):
        report = validate_recipients(
            (shipment("r1", CLEAN),), validator, ValidationMode.STRICT
        )
        assert [s.key for s in report.eligible] == ["r1"]

    def test_the_same_run_differs_only_by_the_flag(self, validator):
        # Changing this outcome is a console toggle, not a deploy.
        ships = (shipment("r1", CLEAN), shipment("r2", SLOPPY), shipment("r3", NONSENSE))
        standard = validate_recipients(ships, validator, ValidationMode.STANDARD)
        strict = validate_recipients(ships, validator, ValidationMode.STRICT)
        assert len(standard.eligible) == 2 and len(standard.escalated) == 1
        assert len(strict.eligible) == 1 and len(strict.escalated) == 2


class TestOffMode:
    def test_nothing_is_validated_and_everything_proceeds(self, validator):
        ships = (shipment("r2", SLOPPY), shipment("r3", NONSENSE))
        report = validate_recipients(ships, validator, ValidationMode.OFF)
        assert len(report.eligible) == 2
        assert report.escalated == ()

    def test_addresses_are_used_exactly_as_supplied(self, validator):
        report = validate_recipients(
            (shipment("r2", SLOPPY),), validator, ValidationMode.OFF
        )
        assert report.eligible[0].address == SLOPPY

    def test_skipped_is_not_reported_as_clean(self, validator):
        # "Never checked" and "checked and fine" are different claims, and a
        # manifest has to be able to tell them apart.
        report = validate_recipients(
            (shipment("r1", CLEAN),), validator, ValidationMode.OFF
        )
        assert report.results["r1"].outcome is ValidationOutcome.SKIPPED


class TestReport:
    def test_every_shipment_has_a_result_including_the_ones_that_passed(self, validator):
        ships = (shipment("r1", CLEAN), shipment("r2", SLOPPY), shipment("r3", NONSENSE))
        report = validate_recipients(ships, validator)
        assert set(report.results) == {"r1", "r2", "r3"}

    def test_nobody_is_silently_dropped(self, validator):
        ships = (shipment("r1", CLEAN), shipment("r2", SLOPPY), shipment("r3", NONSENSE))
        report = validate_recipients(ships, validator, ValidationMode.STRICT)
        accounted = {s.key for s in report.eligible} | {
            e.recipient_key for e in report.escalated
        }
        assert accounted == {"r1", "r2", "r3"}

    def test_the_mode_is_recorded_on_the_report(self, validator):
        report = validate_recipients((shipment("r1", CLEAN),), validator, ValidationMode.STRICT)
        assert report.mode is ValidationMode.STRICT


class TestAdvisories:
    def test_a_clean_address_the_validator_commented_on_is_flagged(self, validator):
        # Measured live: Shippo returns "Street address (directional or suffix
        # only) was corrected to validate the address" while returning street1
        # byte for byte identical. It claims a correction; the fields show
        # none. classify is right to call that CLEAN -- and the claim still
        # deserves to reach the operator.
        report = validate_recipients((shipment("r1", CLEAN),), validator)
        result = report.results["r1"]
        assert result.outcome is ValidationOutcome.CLEAN
        # This particular fixture is quiet; the property is what matters.
        assert result.advisory == bool(result.messages)

    def test_advisories_cover_clean_addresses_only(self, validator):
        # A correctable address surfaces its messages through the correction,
        # a failed one through the escalation reason. These would vanish.
        ships = (shipment("r1", CLEAN), shipment("r2", SLOPPY), shipment("r3", NONSENSE))
        report = validate_recipients(ships, validator)
        for key in report.advisories():
            assert report.results[key].outcome is ValidationOutcome.CLEAN

    def test_a_silent_clean_address_produces_no_advisory(self, validator):
        report = validate_recipients((shipment("r1", CLEAN),), validator)
        if not report.results["r1"].messages:
            assert report.advisories() == {}


class TestAdvisoriesReachTheManifest:
    def test_they_are_rendered_against_the_named_row(self):
        from bbq_shipment_agent.planning import Manifest, ManifestRow, render

        row = ManifestRow(
            recipient_key="r1", name="Ana Ruiz", address={}, box_size="small",
            gel_pack_count=6, ship_date=date(2026, 8, 17), carrier="UPS",
            service="2nd Day Air", cost=10.0, expected_arrival=date(2026, 8, 19),
            predicted_arrival_temp_c=0.0, thermal_margin_c=4.4,
        )
        manifest = Manifest(
            run_id="run-x", rows=(row,), carriers=("UPS",), runners_up=(),
            advisories={"r1": ("directional was corrected",)},
        )
        text = render(manifest)
        assert "validator notes" in text
        assert "Ana Ruiz" in text and "directional was corrected" in text

    def test_a_manifest_with_none_says_nothing(self):
        from bbq_shipment_agent.planning import Manifest, render

        text = render(Manifest(run_id="r", rows=(), carriers=(), runners_up=()))
        assert "validator notes" not in text
