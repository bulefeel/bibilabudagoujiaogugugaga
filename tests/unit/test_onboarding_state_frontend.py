import shutil
import subprocess
from pathlib import Path
import pytest

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for frontend behavior tests")
def test_onboarding_state_frontend():
    result=subprocess.run([shutil.which("node"),"--test",str(Path(__file__).with_name("onboarding_state_frontend.test.cjs"))],capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
