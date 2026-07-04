import base64

import pytest

from azdo_backup.client import AzDoClient, AzDoError


def make_client(org="https://dev.azure.com/myorg"):
    return AzDoClient(org, pat="secret-pat")


def test_requires_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.delenv("AZDO_PAT", raising=False)
    with pytest.raises(AzDoError):
        AzDoClient("https://dev.azure.com/myorg")


def test_org_name_from_dev_azure_url():
    assert make_client().org_name == "myorg"


def test_org_name_from_visualstudio_url():
    c = make_client("https://myorg.visualstudio.com/")
    assert c.org_name == "myorg"


def test_full_url_with_and_without_project():
    c = make_client()
    assert c._full_url("_apis/projects") == "https://dev.azure.com/myorg/_apis/projects"
    assert (c._full_url("_apis/git/repositories", project="Proj")
            == "https://dev.azure.com/myorg/Proj/_apis/git/repositories")
    passthrough = "https://dev.azure.com/other/x"
    assert c._full_url(passthrough) == passthrough


def test_git_auth_args_do_not_contain_raw_pat():
    c = make_client()
    args = c.git_auth_args()
    assert args[0] == "-c"
    assert "secret-pat" not in " ".join(args)
    expected = base64.b64encode(b":secret-pat").decode()
    assert expected in args[1]


def test_strip_url_credentials():
    assert (AzDoClient.strip_url_credentials("https://user:tok@dev.azure.com/o/p/_git/r")
            == "https://dev.azure.com/o/p/_git/r")
    clean = "https://dev.azure.com/o/p/_git/r"
    assert AzDoClient.strip_url_credentials(clean) == clean
    ssh = "git@ssh.dev.azure.com:v3/o/p/r"
    assert AzDoClient.strip_url_credentials(ssh) == ssh
