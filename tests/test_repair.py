"""B3, replaying a real repair loop.

The conversation in `b3-repairs.json` was recorded live against the configured
model, on the correctable-and-failed set that B1 and B2 actually produced from
the screenshot fixtures. Tool results are recomputed rather than recorded, so
the validator and the image cropper are genuinely exercised on every run.
"""

import json
import textwrap
from pathlib import Path

import pytest

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.agents.model import Completion, ModelUnavailable, ToolCall
from bbq_shipment_agent.agents.tools import ToolError, ToolImage, build_tools
from bbq_shipment_agent.capabilities import ValidationMode
from bbq_shipment_agent.ledger import AgentInvocationRecord, iter_records
from bbq_shipment_agent.recipients import (
    RecordedAddressValidator,
    RepairUnavailable,
    extract_from_images,
    repair_addresses,
    validate_recipients,
)
from bbq_shipment_agent.recipients.repair import CONFIG_KEY
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
SHOTS = FIXTURES / "screenshots"
IMAGES = tuple(sorted(SHOTS.glob("*.png")))
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "strict"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


class Scripted:
    """Replays recorded assistant turns, including their tool calls."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    def converse(self, invocation, messages, tools=()):
        self.calls += 1
        self.tools_offered = tools
        if not self.turns:
            return Completion(text='{"repairs": [], "escalations": []}')
        turn = self.turns.pop(0)
        return Completion(
            text=turn["text"],
            input_tokens=turn["input_tokens"],
            output_tokens=turn["output_tokens"],
            tool_calls=tuple(
                ToolCall(id=t["id"], name=t["name"], arguments=t["arguments"])
                for t in turn["tool_calls"]
            ),
        )


@pytest.fixture
def run(tmp_path):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG), encoding="utf-8"
    )
    return initialize_run(
        ledger_root=tmp_path / "ledger",
        config_path=tmp_path / "capabilities.yaml",
        agent_source=SnapshotAgentConfigs(SNAPSHOT),
        snapshot_path=tmp_path / "snap.json",
    )


@pytest.fixture
def validator():
    return RecordedAddressValidator.from_file(FIXTURES / "shippo-addresses.json")


@pytest.fixture
def needing_repair(run, validator, tmp_path):
    replies = json.loads((FIXTURES / "b1-extractions.json").read_text())["replies"]
    order = [p.name for p in IMAGES]

    class ReplayVision:
        def converse(self, invocation, messages, tools=()):
            row = replies[order.pop(0)]
            return Completion(text=row["text"])

    extracted = extract_from_images(
        run, IMAGES, model=ReplayVision(), ledger_root=tmp_path / "ledger"
    )
    return validate_recipients(
        extracted.recipients, validator, ValidationMode.STRICT
    ).for_repair


@pytest.fixture
def result(run, validator, needing_repair, tmp_path):
    turns = json.loads((FIXTURES / "b3-repairs.json").read_text())["turns"]
    return repair_addresses(
        run,
        needing_repair,
        validator=validator,
        model=Scripted(turns),
        screenshots=SHOTS,
        ledger_root=tmp_path / "ledger",
    )


class TestTheLoop:
    def test_it_repairs_most_and_escalates_the_rest(self, result, needing_repair):
        assert result.repaired
        assert result.escalated
        assert len(result.repaired) + len(result.escalated) == len(needing_repair)

    def test_nobody_is_silently_dropped(self, result, needing_repair):
        # Design 4: escalated to a human queue, never silently dropped. A
        # repair loop that loses a record is worse than one that repairs none.
        accounted = {r.key for r in result.repaired} | {
            e.recipient_key for e in result.escalated
        }
        assert accounted == {r.key for r in needing_repair}

    def test_it_reads_the_image_before_proposing(self, result):
        # B3 exists because the extraction was wrong, so re-reading the
        # extracted text would ask the question that already got the wrong
        # answer. The provenance pointer is for this.
        assert "read_image_region" in result.tools_called
        assert "validate_address" in result.tools_called

    def test_the_tools_offered_match_the_contract(self, run, validator, needing_repair, tmp_path):
        from bbq_shipment_agent.agents.tools import TOOL_NAMES

        model = Scripted([])
        repair_addresses(
            run, needing_repair, validator=validator, model=model,
            screenshots=SHOTS, ledger_root=tmp_path / "ledger",
        )
        assert {t.name for t in model.tools_offered} == TOOL_NAMES[CONFIG_KEY]

    def test_an_empty_set_does_no_work(self, run, validator, tmp_path):
        model = Scripted([])
        result = repair_addresses(
            run, (), validator=validator, model=model,
            screenshots=SHOTS, ledger_root=tmp_path / "ledger",
        )
        assert result.repaired == () and result.escalated == ()
        assert model.calls == 0


class TestTheValidatorAdjudicates:
    def test_only_proposals_the_validator_accepted_became_repairs(self, result, validator):
        # The agent reports `validated: true`. That claim is not trusted --
        # "the model said it checked" and "it checks out" are different facts,
        # so every proposal is put to the validator here.
        #
        # Checked against what the agent actually proposed, taken from the
        # recorded turns. Re-validating the *repaired* address would be a
        # different question: by then it is the validator's own canonical
        # form, which is not what was adjudicated.
        from bbq_shipment_agent.planning import Address
        from bbq_shipment_agent.recipients import ValidationOutcome

        turns = json.loads((FIXTURES / "b3-repairs.json").read_text())["turns"]
        final = json.loads(turns[-1]["text"])
        repaired = {r.key for r in result.repaired}
        proposals = {
            row["recipient_key"]: row["proposed"]
            for row in final["repairs"]
            if row["recipient_key"] in repaired
        }
        assert proposals, "the recording contains no accepted repairs"
        for key, p in proposals.items():
            outcome = validator.validate(
                Address(key, p["street1"], p["city"], p["state"], p["zip"])
            ).outcome
            assert outcome is not ValidationOutcome.FAILED, (key, p)

    def test_a_repair_carries_the_validators_form_not_the_agents(self, result):
        # Same rule as B2: the canonical address is what gets quoted, so the
        # submitted one cannot be priced by accident.
        assert any(r.address.zip and "-" in r.address.zip for r in result.repaired)

    def test_an_unvalidatable_proposal_is_refused(self, run, validator, needing_repair, tmp_path):
        key = needing_repair[0].key
        bogus = json.dumps({
            "repairs": [{
                "recipient_key": key,
                "proposed": {"street1": "99999 Nowhere Blvd", "city": "Fargo",
                             "state": "ND", "zip": "58102"},
                "validated": True,
            }],
            "escalations": [],
        })
        result = repair_addresses(
            run, needing_repair[:1], validator=validator,
            model=Scripted([{"text": bogus, "tool_calls": [],
                             "input_tokens": 1, "output_tokens": 1}]),
            screenshots=SHOTS, ledger_root=tmp_path / "ledger",
        )
        assert result.repaired == ()
        assert result.rejected == (key,)
        assert "did not validate" in result.escalated[0].reason


class TestItDoesNotInvent:
    def test_an_obscured_digit_is_escalated_not_guessed(self, result):
        # Live, two records had ZIPs the image does not show -- one under an
        # emoji, one cut off at the screen edge. Both were escalated. A
        # validated address for the wrong doorstep is the worst outcome
        # available to this loop and is invisible downstream.
        escalated = {e.recipient_key for e in result.escalated}
        assert {"farah", "yuki-tanaka"} <= escalated

    def test_an_undeliverable_address_stays_escalated(self, result):
        # Gus Whitfield: the misspelling is obvious and fixing it still does
        # not validate, because the building takes no mail. B3 proposes and
        # the validator adjudicates.
        assert "gus-whitfield" in {e.recipient_key for e in result.escalated}

    def test_every_escalation_says_why(self, result):
        assert all(len(e.reason) > 20 for e in result.escalated)


class TestProvenanceSurvives:
    def test_a_repaired_record_still_knows_its_image(self, result):
        for r in result.repaired:
            assert r.provenance is not None and r.provenance.source_image


class TestTheImageTool:
    def test_it_crops_to_the_region(self, validator):
        (crop, _) = build_tools(
            CONFIG_KEY, validator=validator, screenshots=SHOTS
        )
        out = crop.run(source_image=IMAGES[0].name, x=75, y=940, width=694, height=112)
        assert isinstance(out, ToolImage)
        assert out.block["type"] == "image"
        assert "region" in out.note

    def test_no_region_returns_the_whole_screenshot(self, validator):
        (crop, _) = build_tools(CONFIG_KEY, validator=validator, screenshots=SHOTS)
        assert "full image" in crop.run(source_image=IMAGES[0].name).note

    def test_it_refuses_to_read_outside_the_screenshot_directory(self, validator):
        # A model told to read `../../.env` reads `.env` unless something
        # stops it. The filename comes from generated text.
        (crop, _) = build_tools(CONFIG_KEY, validator=validator, screenshots=SHOTS)
        with pytest.raises(ToolError, match="outside the screenshot directory"):
            crop.run(source_image="../../../.env")

    def test_a_missing_screenshot_is_reported_not_raised_out(self, validator):
        (crop, _) = build_tools(CONFIG_KEY, validator=validator, screenshots=SHOTS)
        with pytest.raises(ToolError, match="no such screenshot"):
            crop.run(source_image="99-nonexistent.png")

    def test_the_tool_is_absent_without_a_screenshot_directory(self, validator):
        # A run from a hand-written roster has no images, and offering a tool
        # that cannot work is worse than offering nothing.
        names = {t.name for t in build_tools(CONFIG_KEY, validator=validator)}
        assert names == {"validate_address"}


class TestTheLedger:
    def test_the_invocation_is_recorded(self, result, tmp_path):
        # B1 also writes one -- the fixture runs extraction to build the set
        # -- so this asserts B3's record is present rather than that it is
        # the only one.
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        mine = [r for r in records if r.agent_key == CONFIG_KEY]
        assert len(mine) == 1
        assert mine[0].outcome.startswith("repaired:")
        assert mine[0].iterations == result.iterations
        assert "screenshot-extraction" in {r.agent_key for r in records}

    def test_the_ledger_line_carries_the_tool_trace(self, result, tmp_path):
        # The same trace `RepairResult` returns, on the durable record: the
        # result object is gone when the process exits and the question
        # "which tools did this run use" is asked months later.
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        mine = [r for r in records if r.agent_key == CONFIG_KEY][0]
        assert mine.tools_called == list(result.tools_called)
        assert set(mine.tools_offered) == {"validate_address", "read_image_region"}

    def test_a_run_with_no_images_records_the_tool_it_lacked(
        self, run, validator, needing_repair, tmp_path
    ):
        # Offered is per run, not the registry: without screenshots there is
        # no `read_image_region`, and a line claiming otherwise would make an
        # unread image look like a choice the model made.
        repair_addresses(
            run, needing_repair, validator=validator, model=Scripted([]),
            ledger_root=tmp_path / "ledger",
        )
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        mine = [r for r in records if r.agent_key == CONFIG_KEY][0]
        assert mine.tools_offered == ["validate_address"]
        assert mine.tools_called == []

    def test_b1_records_no_tool_columns_at_all(self, result, tmp_path):
        # B1 has no tool loop (design 6.3), so it says nothing about tools
        # rather than saying it was offered none.
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        b1 = [r for r in records if r.agent_key == "screenshot-extraction"]
        assert b1 and all(r.tools_offered is None and r.tools_called is None for r in b1)


class TestRefusals:
    def test_an_unavailable_config_does_not_silently_pass_the_set(
        self, run, validator, needing_repair, tmp_path
    ):
        run.agent_configs.pop(CONFIG_KEY)
        with pytest.raises(RepairUnavailable, match="no config"):
            repair_addresses(
                run, needing_repair, validator=validator, model=Scripted([]),
                screenshots=SHOTS, ledger_root=tmp_path / "ledger",
            )

    def test_an_unreachable_model_raises_rather_than_repairing_nothing(
        self, run, validator, needing_repair, tmp_path
    ):
        class Dead:
            def converse(self, *a, **k):
                raise ModelUnavailable("no key")

        with pytest.raises(RepairUnavailable):
            repair_addresses(
                run, needing_repair, validator=validator, model=Dead(),
                screenshots=SHOTS, ledger_root=tmp_path / "ledger",
            )

    def test_an_unparseable_reply_escalates_everyone_rather_than_losing_them(
        self, run, validator, needing_repair, tmp_path
    ):
        result = repair_addresses(
            run, needing_repair, validator=validator,
            model=Scripted([{"text": "I could not do it", "tool_calls": [],
                             "input_tokens": 1, "output_tokens": 1}]),
            screenshots=SHOTS, ledger_root=tmp_path / "ledger",
        )
        assert result.repaired == ()
        assert len(result.escalated) == len(needing_repair)
        assert "no verdict" in result.escalated[0].reason
