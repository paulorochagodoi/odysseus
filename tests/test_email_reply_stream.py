import json
import pytest
from src.email_reply_stream import reply_body, stream_reply


def test_analysis_is_not_a_reply():
    assert reply_body('The user wants me to draft a reply. Context Analysis: ...', complete=True) == ''
    assert reply_body('<think>analysis</think>', complete=True) == ''
    assert reply_body('<<<REPLY>>>Hi Jonathan', complete=True) == ''
    assert reply_body('<<<REPLY>>><think>analysis</think>Hi<<<END>>>', complete=True) == ''
    assert reply_body('analysis\n<<<REPLY>>>Hi Jonathan,\nThanks.<<<END>>>', complete=True) == 'Hi Jonathan,\nThanks.'


@pytest.mark.asyncio
async def test_stream_filters_reasoning_and_split_markers(monkeypatch):
    from src import llm_core
    calls, events = [], []
    async def model(*args, **kwargs):
        calls.append(kwargs)
        for delta in ['Private analysis.', '<<<REP', 'LY>>>Hi Jonathan,', '\nThanks.', '<<<EN', 'D>>>']:
            yield 'data: ' + json.dumps({'delta': delta}) + '\n\n'
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    raw, used = await stream_reply([('http://local', 'Ajax', {})], [], emit)
    assert used == 'Ajax'
    assert calls[0]['thinking_mode'] == 'off'
    assert events[-1]['text'] == 'Hi Jonathan,\nThanks.'
    assert all('analysis' not in e['text'] and '<<<' not in e['text'] for e in events)


@pytest.mark.asyncio
async def test_incomplete_stream_clears_partial_draft(monkeypatch):
    from src import llm_core
    events = []
    async def model(*args, **kwargs):
        yield 'data: ' + json.dumps({'delta': '<<<REPLY>>>Partial'}) + '\n\n'
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    with pytest.raises(ValueError, match='incomplete'):
        await stream_reply([('http://local', 'Ajax', {})], [], emit)
    assert events[-1]['text'] == ''


async def _run_with(monkeypatch, chunks):
    from src import llm_core
    events = []
    async def model(*args, **kwargs):
        for chunk in chunks:
            yield chunk
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    return await stream_reply([('https://chatgpt.com/backend-api/codex/responses', 'gpt-5.6-luna', {})], [], emit), events


@pytest.mark.asyncio
@pytest.mark.parametrize('with_event_line', [True, False])
async def test_chatgpt_auth_error_surfaces_real_message(monkeypatch, with_event_line):
    text = 'ChatGPT Subscription credentials expired or were rejected. Reconnect the provider.'
    chunk = ('event: error\n' if with_event_line else '') + 'data: ' + json.dumps({'status': 401, 'text': text}) + '\n\n'
    with pytest.raises(ValueError) as info:
        await _run_with(monkeypatch, [chunk])
    assert 'credentials expired' in str(info.value)
    assert 'analysis' not in str(info.value)


@pytest.mark.asyncio
async def test_unreachable_host_error_surfaces_real_message(monkeypatch):
    chunk = 'event: error\ndata: ' + json.dumps({'error': 'Cannot reach host', 'status': 503}) + '\n\n'
    with pytest.raises(ValueError, match='Cannot reach host') as info:
        await _run_with(monkeypatch, [chunk])
    assert 'analysis' not in str(info.value)


@pytest.mark.asyncio
async def test_analysis_without_markers_still_rejected(monkeypatch):
    chunk = 'data: ' + json.dumps({'delta': 'The user wants a reply. Context Analysis: be polite.'}) + '\n\n'
    with pytest.raises(ValueError, match='analysis or an incomplete reply'):
        await _run_with(monkeypatch, [chunk])
