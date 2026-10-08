"""Agent-side traversal tests; API capture schemas are tested separately."""
from importlib import import_module

from test_browser_collector import FakeBridge, FakeModel


class CaptureBridge(FakeBridge):
    def state(self):
        return {**super().state(),"mode":"background_text","sequence":len(self.calls)}

    def execute(self,operation,arguments):
        result=super().execute(operation,arguments)
        result['data']['capture']={"source_url":result['data']['note']['source_url'],"body_text":result['data']['note']['text'],"comments":[],"image_candidates":[],"comments_status":"partial","images_status":"complete","body_status":"complete"}
        result['data']['detail_targets']=[]
        return result


def test_capture_load_actions_max_25():
    bridge=CaptureBridge()
    initial=bridge.execute('observe',{})['data']
    result=import_module('app.agents.browser.capture').collect_public_note(bridge,initial)
    assert result['capture']['comments_status']=='partial'
    assert sum(op=='scroll_comments' for op,_ in bridge.calls)==25
    assert bridge.calls[-1][0]=='read_note'


def test_capture_stops_at_command_budget_and_reobserved_is_not_a_read():
    bridge=CaptureBridge()
    bridge.state=lambda:{'status':'running','sequence':118+len(bridge.calls),'mode':'background_text'}
    initial=bridge.execute('observe',{})['data']
    import_module('app.agents.browser.capture').collect_public_note(bridge,initial)
    assert [op for op,_ in bridge.calls]==['observe','read_note']
    original=bridge.execute
    def resumed(op,args):
        return {**original(op,args),'reobserved':True}
    bridge.execute=resumed
    import pytest
    with pytest.raises(ValueError):
        import_module('app.agents.browser.capture').collect_public_note(bridge,initial)


def test_background_summary_failure_still_saves_confirmed_original():
    bridge=CaptureBridge()
    original=bridge.execute
    def page(op,args):
        result=original(op,args)
        result['data']['capture']['comments_status']='complete'
        if op in {'observe','close_note'}:
            result['data']['kind']='results'
        return result
    bridge.execute=page
    model=FakeModel([('open_note',{'version':'v1','target_id':'test'}),('finish',{})])
    def summary(_observation):
        raise RuntimeError('model unavailable')
    model.summarize=summary
    result=import_module('app.agents.browser.collector').run_collection(bridge,lambda:model)
    assert result['status']=='completed'
    assert bridge.saved==[None]


def test_original_saved_before_optional_summary_at_deadline():
    bridge = CaptureBridge()
    now = [0]
    original = bridge.execute
    def page(op,args):
        result = original(op,args)
        result['data']['capture']['comments_status'] = 'complete'
        if op in {'observe','close_note'}:
            result['data']['kind'] = 'results'
        if op == 'read_note':
            now[0] = 590
        return result
    bridge.execute = page
    model = FakeModel([('open_note',{'version':'v1','target_id':'test'}),('finish',{})])
    early_summary = []
    def slow_summary(_observation):
        early_summary.append(not bool(bridge.saved))
        now[0] += 30
        raise TimeoutError('summary timed out')
    model.summarize = slow_summary
    import_module('app.agents.browser.capture').run_background_collection(bridge,lambda:model,clock=lambda:now[0])
    assert bridge.saved == [None]
    assert not any(early_summary)


def test_bridge_rechecks_tab_before_each_image_and_keeps_text_on_freeze():
    module=import_module('app.agents.browser.bridge_client')
    bridge=object.__new__(module.BridgeClient)
    bridge.deadline=float('inf')
    bridge.state=lambda:{'mode':'background_text','status':'running'}
    actions=[]
    reads=[0]
    def observe(op,args):
        reads[0]+=1
        if reads[0]==3:
            raise RuntimeError('frozen after first image')
        return {'ok':True,'data':{'kind':'note'}}
    bridge.execute=observe
    def call(path='',body=None):
        actions.append(path)
        return {'saved':True,'item_id':'item','image_indices':[0,1]} if path=='/save' else {}
    bridge.call=call
    assert bridge.save(None,'test')['saved']
    assert actions==['/save','/media/item/0']


def test_command_limit_does_not_block_already_confirmed_text_save():
    module=import_module('app.agents.browser.bridge_client')
    bridge=object.__new__(module.BridgeClient)
    bridge.deadline=float('inf')
    bridge.state=lambda:{'mode':'background_text','status':'running','sequence':120}
    bridge.execute=lambda *_: (_ for _ in ()).throw(AssertionError('no command budget left'))
    actions=[]
    def call(path='',body=None):
        actions.append(path)
        return {'saved':True,'item_id':'item','image_indices':[0]}
    bridge.call=call
    assert bridge.save(None,'test')['saved']
    assert actions==['/save']
