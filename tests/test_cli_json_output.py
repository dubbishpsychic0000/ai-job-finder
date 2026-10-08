from __future__ import annotations

import json

from app.cli import _print_json


def test_json_output_is_not_wrapped_inside_string_values(capsys):
    report = {"limitation": "OpenStreetMap coverage is incomplete; " + "company " * 20}

    _print_json(report)

    assert json.loads(capsys.readouterr().out) == report
