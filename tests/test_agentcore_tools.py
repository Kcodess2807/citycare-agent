import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentcore import Done, Failed, Unknown
from agentcore.tools import Tool, ToolRegistry, ToolSpecError, tool_result_block


class AddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int = Field(1, description="second number")
    label: str | None = None


async def _add(inp: AddIn, ctx):
    return {"sum": inp.a + inp.b}


def tool(name="add", **kw):
    return Tool(name=name, description=kw.pop("description", "Adds two numbers."), input_model=kw.pop("input_model", AddIn), run=_add, **kw)


# ---- defining tools


def test_register_and_get():
    reg = ToolRegistry()
    t = reg.register(tool())
    assert reg.get("add") is t and reg.get("nope") is None and reg.names() == ["add"]


@pytest.mark.parametrize("bad_name", ["", "has space", "x" * 65, "dot.name"])
def test_bad_tool_names_are_rejected(bad_name):
    with pytest.raises(ToolSpecError):
        tool(name=bad_name)


def test_empty_description_is_rejected():
    with pytest.raises(ToolSpecError):
        tool(description="   ")


def test_input_model_must_forbid_extra_fields():
    class Loose(BaseModel):
        a: int

    with pytest.raises(ToolSpecError, match="extra"):
        tool(input_model=Loose)


def test_duplicate_names_are_rejected():
    reg = ToolRegistry()
    reg.register(tool())
    with pytest.raises(ToolSpecError):
        reg.register(tool())


# ---- the schema the model sees


def test_schemas_have_the_api_shape_and_keep_registration_order():
    reg = ToolRegistry()
    reg.register(tool("zeta"))
    reg.register(tool("alpha", description="Other."))
    schemas = reg.schemas()
    assert [s["name"] for s in schemas] == ["zeta", "alpha"]  # not sorted: stable, so the cache keeps hitting
    s = schemas[0]
    assert set(s) == {"name", "description", "input_schema"}
    props = s["input_schema"]["properties"]
    assert set(props) == {"a", "b", "label"} and s["input_schema"]["required"] == ["a"]
    assert s["input_schema"]["additionalProperties"] is False
    assert props["b"]["description"] == "second number"
    json.dumps(schemas)  # must be serialisable as is


def test_schemas_are_identical_across_calls():
    reg = ToolRegistry()
    reg.register(tool())
    assert json.dumps(reg.schemas()) == json.dumps(reg.schemas())


# ---- validating what the model sent


def test_valid_input_gives_a_model_instance_with_defaults():
    reg = ToolRegistry()
    reg.register(tool())
    t, inp = reg.parse("add", {"a": 2})
    assert isinstance(inp, AddIn) and (inp.a, inp.b) == (2, 1) and t.name == "add"


def test_errors_are_readable_and_name_the_field():
    reg = ToolRegistry()
    reg.register(tool())
    missing = reg.parse("add", {})
    wrong_type = reg.parse("add", {"a": "not a number"})
    extra = reg.parse("add", {"a": 1, "phone": "123"})
    for r, field_name in ((missing, "a"), (wrong_type, "a"), (extra, "phone")):
        assert isinstance(r, Failed) and r.reason.startswith("Invalid input for 'add'") and field_name in r.reason


def test_error_text_does_not_echo_the_bad_value():
    reg = ToolRegistry()
    reg.register(tool())
    r = reg.parse("add", {"a": "TOP-SECRET-VALUE"})
    assert isinstance(r, Failed) and "TOP-SECRET-VALUE" not in r.reason


def test_unknown_tool_lists_the_available_ones():
    reg = ToolRegistry()
    reg.register(tool("one"))
    reg.register(tool("two"))
    r = reg.parse("three", {})
    assert isinstance(r, Failed) and "Unknown tool 'three'" in r.reason and "one, two" in r.reason


def test_unknown_tool_in_an_empty_registry():
    assert "none" in ToolRegistry().parse("x", {}).reason


@pytest.mark.parametrize("raw", ["a string", None, [1, 2], 5])
def test_input_that_is_not_an_object_is_a_failed_result_not_a_crash(raw):
    reg = ToolRegistry()
    reg.register(tool())
    r = reg.parse("add", raw)
    assert isinstance(r, Failed) and "JSON object" in r.reason


# ---- result blocks


def test_result_blocks():
    ok = tool_result_block("t1", Done({"sum": 3}))
    assert ok == {"type": "tool_result", "tool_use_id": "t1", "content": '{"sum": 3}'}
    failed = tool_result_block("t2", Failed("no good"))
    assert failed == {"type": "tool_result", "tool_use_id": "t2", "content": "no good", "is_error": True}
    unknown = tool_result_block("t3", Unknown("could not tell"))
    assert unknown["is_error"] is True and unknown["content"] == "could not tell"


def test_done_value_that_json_cannot_encode_is_stringified_not_a_crash():
    block = tool_result_block("t", Done({"when": object}))
    assert "is_error" not in block and "object" in block["content"]
