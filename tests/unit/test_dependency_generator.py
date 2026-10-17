"""Dependency generation must use the repository's committed output path."""

import builtins
import json
from pathlib import Path
from unittest.mock import patch

from tools import focused_config_dependencies as generator


def test_dependency_generator_preserves_committed_content():
    expected = Path(generator.__file__).resolve().with_suffix(".json")
    assert (
        generator.FocusedConfigDependencies().generate_conditional_guide()
        == json.loads(expected.read_text(encoding="utf-8"))
    )


def test_dependency_generator_uses_repository_output(capsys):
    expected = (
        Path(generator.__file__).resolve().parent / "focused_config_dependencies.json"
    )
    opened = []

    def capture_output(path, *args, **kwargs):
        assert Path(path) == expected
        opened.append(Path(path))
        # Capture the generated data without touching a different checkout.
        from io import StringIO

        return StringIO()

    with patch.object(builtins, "open", capture_output):
        generator.main()
    assert opened == [expected]
