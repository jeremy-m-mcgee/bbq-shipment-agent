"""The run input file."""

from datetime import date, timedelta

import pytest

from bbq_shipment_agent.planning import DEFAULT_LANE, ShipDayError
from bbq_shipment_agent.recipients import (
    RosterError,
    default_ship_dates,
    load_roster,
)

MINIMAL = """
origin:
  name: BBQ HQ
  street1: 64 Divisadero St
  city: San Francisco
  state: CA
  zip: "94117"
recipients:
  - name: Ana Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""


def write(tmp_path, text, name="recipients.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_a_minimal_file_loads(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL))
        assert roster.packet_count == 1
        assert roster.origin.city == "San Francisco"
        assert roster.recipients[0].name == "Ana Ruiz"

    def test_the_key_defaults_to_a_slug_of_the_name(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL))
        assert roster.recipients[0].key == "ana-ruiz"

    def test_an_explicit_key_wins(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL + "    key: ana-2026\n"))
        assert roster.recipients[0].key == "ana-2026"

    def test_a_recipient_with_no_lane_gets_the_stated_default(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL))
        assert roster.recipients[0].lane is DEFAULT_LANE

    def test_a_declared_lane_is_attached(self, tmp_path):
        text = MINIMAL.replace(
            "recipients:", "lanes:\n  gulf:\n    ambient_c: 32.0\n    zone: 4\nrecipients:"
        )
        roster = load_roster(write(tmp_path, text + "    lane: gulf\n"))
        assert roster.recipients[0].lane.ambient_c == 32.0
        assert roster.recipients[0].lane.zone == 4

    def test_an_operator_pin_is_read_as_a_date(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL + "    ship_date: 2026-08-15\n"))
        assert roster.recipients[0].required_ship_date == date(2026, 8, 15)


class TestRefusals:
    def test_an_unquoted_zip_is_refused_with_the_fix(self, tmp_path):
        # YAML 1.1 reads 02134 as octal. Silently shipping to ZIP 1116 is the
        # exact failure B2 exists to prevent, arriving before B2 can see it.
        text = MINIMAL.replace('zip: "20500"', "zip: 02134")
        with pytest.raises(RosterError, match="Quote it"):
            load_roster(write(tmp_path, text))

    def test_a_missing_origin_is_refused_rather_than_defaulted(self, tmp_path):
        text = "\n".join(MINIMAL.splitlines()[7:])
        with pytest.raises(RosterError, match="origin"):
            load_roster(write(tmp_path, text))

    def test_a_duplicate_key_is_refused(self, tmp_path):
        # B4 would consolidate them silently; a repeated key is a typo.
        with pytest.raises(RosterError, match="appears twice"):
            load_roster(write(tmp_path, MINIMAL + MINIMAL.split("recipients:")[1]))

    def test_an_empty_recipient_list_is_refused(self, tmp_path):
        text = MINIMAL.split("recipients:")[0] + "recipients: []\n"
        with pytest.raises(RosterError, match="non-empty"):
            load_roster(write(tmp_path, text))

    def test_a_missing_address_field_names_the_field(self, tmp_path):
        text = MINIMAL.replace("    city: Washington\n", "")
        with pytest.raises(RosterError, match="city"):
            load_roster(write(tmp_path, text))

    def test_an_undeclared_lane_is_refused(self, tmp_path):
        with pytest.raises(RosterError, match="not defined"):
            load_roster(write(tmp_path, MINIMAL + "    lane: gulf\n"))

    def test_a_wednesday_ship_date_is_refused_by_name(self, tmp_path):
        # Better here than at C2, where it becomes a quietly smaller
        # enumeration in a file the operator has already stopped looking at.
        text = MINIMAL + "ship_dates:\n  - 2026-08-19\n"
        with pytest.raises(ShipDayError, match="Wednesday"):
            load_roster(write(tmp_path, text))

    def test_a_wednesday_pin_is_refused_too(self, tmp_path):
        with pytest.raises(ShipDayError, match="Wednesday"):
            load_roster(write(tmp_path, MINIMAL + "    ship_date: 2026-08-19\n"))

    def test_a_missing_file_reports_the_path(self, tmp_path):
        with pytest.raises(RosterError, match="nope.yaml"):
            load_roster(tmp_path / "nope.yaml")


class TestShipDates:
    def test_declared_dates_are_used_in_order(self, tmp_path):
        text = MINIMAL + "ship_dates:\n  - 2026-08-18\n  - 2026-08-15\n"
        roster = load_roster(write(tmp_path, text))
        assert roster.ship_dates == (date(2026, 8, 15), date(2026, 8, 18))

    def test_the_default_is_the_next_saturday_monday_and_tuesday(self, tmp_path):
        roster = load_roster(write(tmp_path, MINIMAL), today=date(2026, 8, 12))
        assert roster.ship_dates == (
            date(2026, 8, 15),
            date(2026, 8, 17),
            date(2026, 8, 18),
        )

    def test_the_default_never_includes_today(self, tmp_path):
        # A run planned on a Monday cannot ship that same Monday: the packets
        # are not packed yet, and quoting a same-day drop-off would put a date
        # on the manifest that has already partly passed.
        saturday = date(2026, 8, 15)
        assert saturday not in default_ship_dates(saturday)
        assert min(default_ship_dates(saturday)) > saturday

    def test_the_default_is_always_three_shippable_days(self):
        for offset in range(7):
            dates = default_ship_dates(date(2026, 8, 10) + timedelta(days=offset))
            assert len(dates) == 3
            assert {d.weekday() for d in dates} == {5, 0, 1}
