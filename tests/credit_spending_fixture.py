#!/usr/bin/env python3
"""Actual Credits controls on synthetic accounts; all network/state is isolated.

python3 tests/credit_spending_fixture.py --port 18327 --state-dir ~/.cache/agent-scratch/personal-codex-router/credits-fixture
POST /fixture {"state":"weekly|five-hour|both|weekly-only-positive|due|fresh|only|multiple|failure"} with X-Router-Request:1.
"""
import argparse
import copy
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from fast_mode_fixture import build_state, app


def build(directory):
    state=build_state(directory)
    records=app.api('/auth-files')['files']
    state.data['accounts'][:]=state.data['accounts'][:2]
    records[:]=records[:2]
    state.credit_spending=app.account_access.CreditSpending(directory/'credit-spending-controls.json')
    state.resets.is_paused=state.credit_spending.marked
    state.fixture={'failure':False,'only':False,'records':records}
    for a in state.data['accounts']:
        a['usage_credits']=dict(balance=60247.83,unlimited=False,limit_reached=False)
        a['windows'][0].update(remaining=89,used=11)
    real_api=app.api
    def api(path,body=None,method=None,timeout=None):
        return real_api(path,body,method,timeout)
    app.api=api
    saved=state.set_credit_spending
    def setting(*args,**kw):
        if state.fixture.pop('failure',False):
            raise OSError('Synthetic save failure')
        return saved(*args,**kw)
    state.set_credit_spending=setting
    original=state.snapshot
    def snapshot():
        state.data['checked_at']=app.utcnow()
        for a,r in zip(state.data['accounts'],records):
            app.account_access.AccountAccess().project(r,a)
            a['availability']=app.classify(r,a)
            a.update(disabled=r['disabled'],active=not r['disabled'])
            state.credit_spending.project(r,a)
        value=original()
        if state.fixture['only']:
            value['accounts']=value['accounts'][:1]
            value['inventory']=[r for r in value['inventory'] if r['type']!='codex' or r['auth_index']==value['accounts'][0]['id']]
            value['pool']=app.pool_status(value)
        return value
    state.snapshot=snapshot
    return state


def transition(state,kind):
    if kind in ('only','multiple'):
        state.fixture['only']=kind=='only';return
    if kind=='failure':state.fixture['failure']=True;return
    a=state.data['accounts'][0];r=state.fixture['records'][0];now=time.time()
    a.update(quota_error=None,quota_checked_at=app.utcnow(),quota_attempted_at=app.utcnow())
    weekly=dict(label='Weekly',slot='primary_window',seconds=604800,remaining=89,used=11,resets_at=now+5*86400+21*3600)
    a['windows']=[weekly]
    if kind in ('weekly','both'):weekly.update(remaining=0,used=100)
    if kind in ('five-hour','both','fresh'):
        a['windows'].append(dict(label='5-hour',slot='secondary_window',seconds=18000,
                                 remaining=80 if kind=='fresh' else 0,
                                 used=20 if kind=='fresh' else 100,resets_at=now+8040))
    if kind=='due':
        weekly.update(remaining=0,used=100,resets_at=now-600)
        a['quota_checked_at']=app.fast_mode.stamp(now-1000)
        marker=copy.deepcopy(state.credit_spending.state['accounts'][r['email']]['paused'])
        marker['resets_at']=now-600
        state.credit_spending.update(r['email'],paused=marker)
    state.apply_credit_spending(r,a)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18327)
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    state=build(args.state_dir)
    class Handler(app.Handler):
        def do_POST(self):
            if self.path=='/fixture':
                if not self.allowed(True):return
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                transition(state,data['state']);return self.send(200,b'{"ok":true}')
            if self.path not in ('/api/accounts/credit-spending','/api/accounts/routing','/api/refresh'):
                return self.send(404,b'{}')
            return super().do_POST()
        def do_GET(self):
            if urlsplit(self.path).path=='/app.js' and self.allowed():
                script=(app.ROOT/'static/app.js').read_bytes().replace(b"window.addEventListener('DOMContentLoaded'",
                    b"const fixtureRender=render;render=()=>{if(current && new URLSearchParams(location.search).has('read-only'))current.read_only=true;fixtureRender();};\nwindow.addEventListener('DOMContentLoaded'",1)
                return self.send(200,script,'text/javascript')
            return super().do_GET()
    server=app.ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.state=state
    print(f'Credits fixture: http://127.0.0.1:{args.port}/',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();state.signins.close()


if __name__=='__main__':main()
