from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.models import Actor, PipelineTaskCreateRequest
from app.services.pipeline_service import create_pipeline_task
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
REF = {'name': 'Fooocus', 'source_url': 'https://github.com/lllyasviel/Fooocus',
       'summary': '基于 SDXL 生成图片', 'advantages': '离线运行', 'input': '文字', 'output': '图片',
       'technology_keywords': ['SDXL'], 'checked_at': '2026-10-07T08:00:00Z'}


def database():
    db = MemoryStore()
    db.tables['domains'][0]['slug'] = 'ai_content'
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'channel': 'xiaohongshu', 'is_active': True,
        'config_jsonb': {'strategy_profile': {'prompt_overrides': {
            'content_branch': 'project_observer', 'project_reference': deepcopy(REF)}}}}]
    return db


def create(db, intent=None, domain='ai_content'):
    return create_pipeline_task(db, PipelineTaskCreateRequest(domain_slug=domain, account_id=ACCOUNT,
        intent_jsonb=intent or {}), Actor(user_id='qa', role='admin'))


def test_created_task_freezes_account_branch_and_source():
    db = database()
    result = create(db)
    assert result['intent_jsonb']['content_branch'] == 'project_observer'
    assert result['intent_jsonb']['project_reference'] == REF
    db.tables['channel_accounts'][0]['config_jsonb']['strategy_profile']['prompt_overrides']['content_branch'] = 'tutorial'
    assert result['intent_jsonb']['content_branch'] == 'project_observer'


def test_explicit_tutorial_overrides_account_branch():
    result = create(database(), {'content_branch': 'tutorial'})
    assert result['intent_jsonb'] == {'content_branch': 'tutorial'}


def test_tutorial_default_is_snapshotted_before_account_changes():
    db = database()
    db.tables['channel_accounts'][0]['config_jsonb']['strategy_profile']['prompt_overrides'] = {}
    assert create(db)['intent_jsonb']['content_branch'] == 'tutorial'


@pytest.mark.parametrize('intent', [{'content_branch': 'made_up'},
                                   {'content_branch': 'project_observer', 'project_reference': {}}])
def test_invalid_branch_or_missing_project_does_not_create_task(intent):
    db = database()
    with pytest.raises(HTTPException) as error:
        create(db, intent)
    assert error.value.status_code == 422
    assert not db.tables.get('pipeline_tasks')
