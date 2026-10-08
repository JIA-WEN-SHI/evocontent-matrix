const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

async function popup(statusReply, failure='') {
  const elements=new Map(),calls=[];
  function element(id) {
    if(!elements.has(id)) elements.set(id,{value:'test-pairing-code',checked:false,hidden:false,textContent:'',disabled:false,listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return element('button');}});
    return elements.get(id);
  }
  const context={document:{getElementById:element,querySelector:()=>element('button')},chrome:{runtime:{id:'a'.repeat(32),getManifest:()=>({version:'1.1.1'}),sendMessage:async input=>{
    calls.push(input);
    if(input.type==='status') return statusReply;
    if(failure) throw new Error(failure);
    if(input.type==='pair-info') return {protocol_version:2,mode:'background_text',provider:'test',model_name:'test'};
    return {connected:true};
  }}}};
  vm.runInNewContext(fs.readFileSync('apps/chrome-bridge/popup.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {element,calls,submit:()=>element('pair').listeners.submit({preventDefault(){},target:element('pair')})};
}

test('missing worker metadata blocks pairing without claiming a confirmed old version',async()=>{
  const p=await popup({connected:false});
  await p.submit();
  assert.match(p.element('status').textContent,/未返回.*版本/);
  assert.doesNotMatch(p.element('status').textContent,/旧版/);
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
});

test('status error survives the initial check and a later submit',async()=>{
  const p=await popup({error:'会话存储不可用'});
  assert.equal(p.element('status').textContent,'会话存储不可用');
  await p.submit();
  assert.equal(p.element('status').textContent,'会话存储不可用');
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
});

test('missing response is reported without a JavaScript property access error',async()=>{
  const p=await popup(undefined);
  await p.submit();
  assert.match(p.element('status').textContent,/未返回.*响应/);
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
});

test('status transport failure survives a later submit without sending a code',async()=>{
  const p=await popup(Promise.reject(new Error('Could not establish connection. Receiving end does not exist.')));
  await p.submit();
  assert.match(p.element('status').textContent,/Receiving end does not exist/);
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
});

test('submit waits for the initial worker check instead of reporting an old worker',async()=>{
  let resolve;
  const pending=new Promise(r=>{resolve=r;});
  const p=await popup(pending);
  const submitting=p.submit();
  assert.doesNotMatch(p.element('status').textContent,/旧版/);
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
  resolve({connected:false,protocol_version:2,extension_version:'1.1.1'});
  await submitting;
  assert.match(p.element('scope').textContent,/后台文本/);
});

test('version mismatch shows actual package and worker identity without exposing code',async()=>{
  const p=await popup({connected:false,protocol_version:1,extension_version:'1.1.0',token:'secret-token'});
  await p.submit();
  assert.match(p.element('status').textContent,/不匹配/);
  const diagnostic=p.element('diagnostics').textContent;
  assert.match(diagnostic,/1\.1\.1/);
  assert.match(diagnostic,/1\.1\.0/);
  assert.ok(diagnostic.includes('a'.repeat(32)));
  assert.doesNotMatch(diagnostic,/test-pairing-code|secret-token/);
  assert.deepEqual(p.calls.map(c=>c.type),['status']);
});

test('matching worker needs preview and explicit consent before pairing',async()=>{
  const p=await popup({connected:false,protocol_version:2,extension_version:'1.1.1'});
  await p.submit();
  assert.match(p.element('scope').textContent,/后台文本/);
  await p.submit();
  assert.ok(!p.calls.some(c=>c.type==='pair'));
  p.element('consent').checked=true;
  await p.submit();
  assert.equal(p.calls.filter(c=>c.type==='pair').length,1);
});

test('popup keeps the concrete runtime error instead of generic service failure',async()=>{
  const p=await popup({connected:false,protocol_version:2,extension_version:'1.1.1'},'具体配对错误');
  await p.submit();
  assert.equal(p.element('status').textContent,'具体配对错误');
  assert.deepEqual(p.calls.map(c=>c.type),['status','pair-info']);
});
