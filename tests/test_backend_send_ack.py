import time
from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.cdp_driver import AuthExpiredError, CDPDriver, RateLimitError
from chatgpt_web2api.turn_anchor import TurnAnchor


def node(text, created):
    return {'id':'user-new','role':'user','text':text,'create_time':created,'children':[]}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('fresh',True), ('stale',False), ('wrong_text',False), ('ambiguous',False), ('degraded',False), ('wrong_conversation',False), ('absent',False)])
async def test_backend_send_ack_is_correlated_and_fresh(kind, expected, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr('chatgpt_web2api.cdp_driver.asyncio.sleep',no_wait)
    now=time.time()
    d=CDPDriver(instance_id='backend-send-unit')
    anchor=TurnAnchor(sent_text='exact prompt',mode='fresh_chat',pre_send_wall_time=now)
    nodes={'user-new':node('exact prompt',now)}
    if kind=='stale':
        nodes['user-new']['create_time']=now-60
    elif kind=='wrong_text':
        nodes['user-new']['text']='different prompt'
    elif kind=='ambiguous':
        nodes['other']=dict(nodes['user-new'],id='other')
    elif kind=='degraded':
        anchor=TurnAnchor(sent_text='exact prompt',mode='degraded_existing',pre_send_wall_time=now,conversation_id_at_capture='real-conv')
    elif kind=='wrong_conversation':
        anchor=TurnAnchor(sent_text='exact prompt',mode='existing_conversation',pre_send_wall_time=now,conversation_id_at_capture='other-conv')
    elif kind=='absent':
        nodes={}
    d._conversation_id_from_url=AsyncMock(return_value='real-conv')
    d._backend_client._fetch_recent_conversation_projection=AsyncMock(return_value={'nodes':nodes})
    d.click_send=AsyncMock()
    assert await d._verify_backend_send_acknowledged(anchor) is expected
    d.click_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[AuthExpiredError(),RateLimitError()])
async def test_backend_send_ack_preserves_auth_and_rate_errors(error):
    d=CDPDriver(instance_id='backend-send-auth-unit')
    d._conversation_id_from_url=AsyncMock(return_value='real-conv')
    d._backend_client._fetch_recent_conversation_projection=AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await d._verify_backend_send_acknowledged(TurnAnchor(sent_text='prompt',mode='fresh_chat',pre_send_wall_time=time.time()))
