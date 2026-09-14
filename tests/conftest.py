import pytest


@pytest.fixture(autouse=True)
def plumi_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[organization]\n"
        'name = "acme"\n'
        'domain = "acme.com"\n',
    )
    monkeypatch.chdir(workspace)
