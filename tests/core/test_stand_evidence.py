import os
import sys
from pathlib import Path

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.domain.stand_evidence import deployment_inputs


def test_environment_identity_uses_private_hmac_and_detects_values(tmp_path):
    runtime=tmp_path/'.greatminds'
    runtime.mkdir()
    profile=tmp_path/'profile.yaml'
    profile.write_text('[]')
    kwargs={'runtime':runtime,'workspace':tmp_path,'profile':profile,'argv':[sys.executable],
            'environment':{'PATH':os.defpath},'extra_vars':{'secret':'first-private-value'}}
    before=deployment_inputs(**kwargs)
    assert before==deployment_inputs(**kwargs)
    kwargs['extra_vars']={'secret':'second-private-value'}
    after=deployment_inputs(**kwargs)
    assert before['environment']['environment_hmac']!=after['environment']['environment_hmac']
    assert before['source']==after['source']
    assert 'private-value' not in str(before)
    assert (runtime/'.runtime/evidence.key').stat().st_mode & 0o777==0o600
    kwargs['environment']['__greatminds_stand_extra_vars']='actual-process-variable'
    changed_environment=deployment_inputs(**kwargs)
    assert changed_environment['environment']['environment_hmac']!=after['environment']['environment_hmac']


@pytest.mark.parametrize('document',['[]','null','stand: []','stand: null','stand: {environment_revision: 2}','stand: {environment_revision: " "}'])
def test_invalid_environment_revision_fails_with_domain_error(tmp_path,document):
    from greatminds.domain.stand_evidence import environment_revision
    (tmp_path/'coordination').mkdir()
    (tmp_path/'coordination/execution.yaml').write_text(document)
    with pytest.raises(GreatMindsError):
        environment_revision(tmp_path/'.greatminds')
