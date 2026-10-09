from pathlib import Path
import pytest

from ziniao_automation.config import PROJECT_ROOT
from ziniao_automation.workflows.store_setup.policy import OnboardingPolicy

POLICY = PROJECT_ROOT / 'onboarding-policy.toml'


def test_policy_has_no_implicit_defaults():
    config = OnboardingPolicy.load(POLICY)
    assert config.stable_sample_count == 2
    assert config.total_deadline_seconds == 1800
    assert config.cleanup_grace_seconds == 15
    with pytest.raises(ValueError):
        OnboardingPolicy.model_validate({})


@pytest.mark.parametrize('change', [
    {'stable_sample_count': 1}, {'sample_interval_ms': 0},
    {'schema_version': 2}, {'schema_version': True}, {'candidate_ready_timeout_ms': 400},
    {'total_deadline_seconds': 1}, {'extra_secret': 'SENSITIVE_VALUE'},
    {'diagnostic_candidate_limit': 0}, {'stable_sample_count': '2'},
])
def test_invalid_policy_rejected(change):
    data = OnboardingPolicy.load(POLICY).model_dump()
    data.update(change)
    with pytest.raises(ValueError):
        OnboardingPolicy.model_validate(data)


def test_invalid_file_error_does_not_echo_contents_or_path(tmp_path):
    path = tmp_path / 'private-path.toml'
    for content in (None, 'password = "SENSITIVE_VALUE"', 'invalid TOML SENSITIVE_VALUE'):
        if content is not None:
            path.write_text(content, encoding='utf-8')
        with pytest.raises(ValueError) as raised:
            OnboardingPolicy.load(path)
        assert 'onboarding-policy.toml' in str(raised.value)
        assert 'SENSITIVE_VALUE' not in str(raised.value)
        assert 'private-path' not in str(raised.value)


def test_installer_includes_policy():
    source = (PROJECT_ROOT / 'installer/build.ps1').read_text(encoding='utf-8')
    assert "'onboarding-policy.toml'" in source
