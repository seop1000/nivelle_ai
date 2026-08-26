import pytest
from nivelle_link.agent import application, folder, note, reminder, search, text_file
from nivelle_protocol import tools
from pydantic import ValidationError


def test_link_execution_uses_the_shared_tool_argument_schemas() -> None:
    assert application.OpenApplicationArguments is tools.OpenApplicationArguments
    assert folder.OpenFolderArguments is tools.OpenFolderArguments
    assert note.CreateNoteArguments is tools.CreateNoteArguments
    assert reminder.SetReminderArguments is tools.SetReminderArguments
    assert search.SearchFilesArguments is tools.SearchFilesArguments
    assert text_file.ReadTextFileArguments is tools.ReadTextFileArguments

    assert application.OpenApplicationArguments(application_id="a" * 128).application_id == "a" * 128
    assert search.SearchFilesArguments(
        query="source", root_id="r" * 128, extensions=["c++"]
    ).extensions == ["c++"]
    assert text_file.ReadTextFileArguments(
        path_ref="root:reference", max_lines=10_000
    ).max_lines == 10_000


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (tools.SearchFilesArguments, {"query": "   ", "root_id": "root"}),
        (tools.CreateNoteArguments, {"title": "   ", "content": "value"}),
    ],
)
def test_shared_summary_fields_reject_whitespace_only_values(
    schema: object, payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="non-whitespace"):
        schema.model_validate(payload)
