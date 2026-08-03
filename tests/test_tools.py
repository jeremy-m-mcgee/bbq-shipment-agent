"""Design 6.4 mitigation 1: the tool contract, asserted at run start."""

import textwrap
from pathlib import Path

import pytest

from bbq_shipment_agent.agent_configs import AGENT_KEYS, AgentConfig
from bbq_shipment_agent.agents.tools import (
    TOOL_NAMES,
    ToolContractError,
    assert_tool_contract,
    build_tools,
)
from bbq_shipment_agent.ledger import RunRecord, iter_records
from bbq_shipment_agent.planning import Address
from bbq_shipment_agent.recipients import RecordedAddressValidator
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
VALIDATIONS = FIXTURES / "shippo-addresses.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "standard"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


def config(agent_key, *, tools=(), available=True):
    return AgentConfig(
        agent_key=agent_key,
        enabled=available,
        instructions="do the thing" if available else None,
        model="claude-sonnet-5",
        declared_tools=tuple(tools),
        source="launchdarkly",
        reason="FALLTHROUGH",
    )


class Source:
    def __init__(self, configs):
        self._configs = configs

    def fetch(self, agent_key, context):
        return self._configs.get(agent_key, config(agent_key, available=False))


def run_a1(tmp_path, configs):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG), encoding="utf-8"
    )
    return initialize_run(
        ledger_root=tmp_path / "ledger",
        config_path=tmp_path / "capabilities.yaml",
        agent_source=Source(configs),
        snapshot_path=tmp_path / "snap.json",
    )


class TestTheContract:
    def test_every_agent_has_a_contract(self):
        # A retrieved config with no entry here has no answer to "what may it
        # do", and silence is the wrong default for that question.
        assert set(TOOL_NAMES) == set(AGENT_KEYS)

    def test_manifest_verification_offers_nothing_and_should_stay_that_way(self):
        # Design 6.2 grants it "manifest read, read-only", and design 10
        # settles the apparent contradiction in section 4 in favour of that.
        assert TOOL_NAMES["manifest-verification"] == frozenset()

    def test_the_contract_matches_what_can_actually_be_built(self):
        # The contract describes what Python has, not what the design intends
        # it to have eventually. A promised-but-unbuilt tool would let the
        # assertion pass for an instruction that cannot work.
        #
        # Every dependency any builder takes is supplied, so a tool that is
        # simply never constructed cannot pass by omission. The session is a
        # placeholder: builders check it is present, and the tool bodies are
        # closures that nothing invokes here.
        dependencies = {
            "validator": RecordedAddressValidator.from_file(VALIDATIONS),
            "session": object(),
        }
        for agent_key, declared in TOOL_NAMES.items():
            built = {t.name for t in build_tools(agent_key, **dependencies)}
            assert built == set(declared), agent_key


class TestTheAssertion:
    def test_a_matching_contract_passes(self):
        assert_tool_contract({"address-repair": config("address-repair",
                                                       tools=["validate_address"])})

    def test_declaring_fewer_tools_than_python_offers_is_fine(self):
        # Python may offer something the instructions never mention. Requiring
        # equality would make adding a tool a breaking change to four configs.
        assert_tool_contract({"address-repair": config("address-repair")})

    def test_a_tool_python_does_not_offer_is_refused(self):
        with pytest.raises(ToolContractError, match="read_image_region"):
            assert_tool_contract(
                {"address-repair": config("address-repair",
                                          tools=["read_image_region"])}
            )

    def test_the_message_names_the_agent_and_both_sides(self):
        with pytest.raises(ToolContractError) as exc:
            assert_tool_contract(
                {
                    "infeasibility-remediation": config(
                        "infeasibility-remediation", tools=["read_ledger"]
                    )
                }
            )
        message = str(exc.value)
        assert "infeasibility-remediation" in message
        assert "read_ledger" in message
        assert "offers nothing" in message

    def test_the_message_lists_what_is_offered_when_there_is_something(self):
        with pytest.raises(ToolContractError) as exc:
            assert_tool_contract(
                {"review-narrator": config("review-narrator", tools=["resolve_from_c5"])}
            )
        # The design's own name for D2's tool is "re-solve trigger"; Python
        # splits it into propose and confirm, so the message has to show what
        # the config should have said rather than only what it got wrong.
        assert "propose_edit" in str(exc.value)

    def test_an_unavailable_config_is_skipped(self):
        # It has no instructions, so it cannot be referencing a tool, and 6.10
        # makes unavailable a normal path rather than a failure.
        assert_tool_contract(
            {"address-repair": config("address-repair", tools=["nonsense"],
                                      available=False)}
        )

    def test_every_mismatch_is_reported_not_just_the_first(self):
        with pytest.raises(ToolContractError) as exc:
            assert_tool_contract(
                {
                    "address-repair": config("address-repair", tools=["bogus_one"]),
                    "review-narrator": config("review-narrator", tools=["bogus_two"]),
                }
            )
        assert "bogus_one" in str(exc.value) and "bogus_two" in str(exc.value)


class TestItAbortsTheRun:
    def test_a1_refuses_to_start(self, tmp_path):
        with pytest.raises(ToolContractError):
            run_a1(tmp_path, {"address-repair": config("address-repair",
                                                       tools=["read_image_region"])})

    def test_an_aborted_run_writes_no_ledger_row(self, tmp_path):
        # The ledger is append-only: a row written for a run that could not
        # legally proceed cannot be taken back.
        with pytest.raises(ToolContractError):
            run_a1(tmp_path, {"address-repair": config("address-repair",
                                                       tools=["read_image_region"])})
        assert list(iter_records(tmp_path / "ledger", RunRecord)) == []

    def test_an_aborted_run_writes_no_snapshot(self, tmp_path):
        # Caching the config of a run that was refused would make the next
        # offline run inherit the same broken contract from disk.
        with pytest.raises(ToolContractError):
            run_a1(tmp_path, {"address-repair": config("address-repair",
                                                       tools=["read_image_region"])})
        assert not (tmp_path / "snap.json").exists()

    def test_a_clean_contract_starts_normally(self, tmp_path):
        run = run_a1(tmp_path, {"address-repair": config("address-repair",
                                                         tools=["validate_address"])})
        assert run.run_id
        assert len(list(iter_records(tmp_path / "ledger", RunRecord))) == 1


class TestTheValidateAddressTool:
    def test_it_is_offered_only_when_a_validator_is_injected(self):
        # A1 asserts the contract before any live dependency exists, so the
        # builder has to tolerate being called with nothing.
        assert build_tools("address-repair") == ()

    def test_it_validates_through_the_injected_seam(self):
        validator = RecordedAddressValidator.from_file(VALIDATIONS)
        (tool,) = build_tools("address-repair", validator=validator)
        result = tool.run(
            street1="64 divisadero st", city="san francisco", state="CA", zip="94110"
        )
        assert result["outcome"] == "correctable"
        assert result["validated_address"]["zip"].startswith("94117")

    def test_a_failed_address_is_reported_not_raised(self):
        # The agent should be told and given the chance to try again. Raising
        # would turn every bad argument into a dead run.
        validator = RecordedAddressValidator.from_file(VALIDATIONS)
        (tool,) = build_tools("address-repair", validator=validator)
        result = tool.run(
            street1="99999 Nowhere Blvd", city="Fargo", state="ND", zip="58102"
        )
        assert result["outcome"] == "failed"

    def test_the_schema_is_what_the_model_api_expects(self):
        validator = RecordedAddressValidator.from_file(VALIDATIONS)
        (tool,) = build_tools("address-repair", validator=validator)
        api = tool.to_api()
        assert set(api) == {"name", "description", "input_schema"}
        assert api["input_schema"]["required"] == ["street1", "city", "state", "zip"]
        # The description carries the three-way outcome, because the model
        # cannot read design section 4.
        assert "correctable" in api["description"]
