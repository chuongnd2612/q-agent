"""Every settings field actually round-trips (#717).

`PUT /settings` used to hand-map each field into the store's camelCase keys, and
`dryRun` was simply missing from that map — so the request returned **200** with the
change silently discarded, and the SPA's "unsaved changes" bar never cleared because
the response still carried the old value. A 200 that dropped the write is the worst
shape this could take: nothing in the network tab, nothing in the logs.

So this asserts the property rather than the field: anything `SettingsUpdate` accepts
must come back changed. A test naming only `dryRun` would have passed the day before
the next setting was added and dropped in exactly the same way.
"""

from __future__ import annotations

import pytest

from app.schemas import SettingsUpdate
from app.services import settings_store


def _sample_value(name: str, annotation: object) -> object:
    """A value distinguishable from the default, per field type."""
    current = settings_store.DEFAULTS.get(name)
    text = str(annotation)
    if "bool" in text:
        return not bool(current)
    if "int" in text:
        return int(current or 0) + 3
    if "float" in text:
        return float(current or 0) + 1.5
    if "dict" in text:
        return {"requirement-analyst": "claude-haiku-4-5"}
    return None  # str fields need a valid value; handled by the caller's skip list


#: str settings are enumerations — an arbitrary string would be stored but is not a
#: meaningful round-trip, so each names a real alternative to its default.
_STRING_VALUES = {
    "claudeModel": "claude-opus-5",
    "executionTarget": "server",
    "authoringMode": "live-harness",
    "healMode": "live-harness",
    "authoringLogVerbosity": "verbose",
}


def _updatable_fields():
    for name, field in SettingsUpdate.model_fields.items():
        yield field.alias or name, field.annotation


@pytest.mark.parametrize("alias,annotation", list(_updatable_fields()))
def test_every_settings_field_round_trips(client, workspace_dir, alias, annotation):
    value = _STRING_VALUES.get(alias, _sample_value(alias, annotation))
    if value is None:
        pytest.skip(f"no sample value defined for {alias}")

    resp = client.put("/settings", json={alias: value})

    assert resp.status_code == 200
    assert resp.json()[alias] == value, f"{alias} was accepted and discarded"
    # ...and it survived the request, rather than only living in the response.
    assert settings_store.load_settings()[alias] == value


def test_dry_run_specifically_persists(client, workspace_dir):
    """The reported bug, pinned by name as well as by the property above."""
    assert client.put("/settings", json={"dryRun": True}).json()["dryRun"] is True

    assert client.get("/settings").json()["dryRun"] is True
    assert settings_store.load_settings()["dryRun"] is True


def test_an_unchanged_field_is_left_alone(client, workspace_dir):
    """`exclude_none` is what makes a partial update partial — without it every absent
    field would be written as null and wipe the rest of the workspace's settings."""
    client.put("/settings", json={"dryRun": True})

    client.put("/settings", json={"parallel": 7})

    saved = client.get("/settings").json()
    assert saved["dryRun"] is True, "an unrelated update cleared another setting"
    assert saved["parallel"] == 7
