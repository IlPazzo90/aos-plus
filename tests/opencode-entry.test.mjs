import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';

const plugin = new URL('../opencode/aos-plugin.mjs', import.meta.url);
const bridge = new URL('../opencode/aos-bridge.mjs', import.meta.url);

test('OpenCode plugin entry and bridge exist', () => {
  assert.ok(existsSync(plugin));
  assert.ok(existsSync(bridge));
});

{
  const { createHooks } = await import(bridge);
  const schema = { string: () => ({ describe() { return this; } }) };
  const tool = Object.assign((v) => v, { schema });
  const decision = (executor = 'open') => ({ classification: { tier: 'T1', risk: 'LOW', domain: 'general' },
    decision: { executor, model: 'vercel/test', backend: 'codex', verify: 'targeted' } });
  const setup = async (executor = 'open') => {
    const calls = [];
    const client = { session: { messages: async () => ({ data: [] }) },
      tui: { showToast: async () => ({}) } };
    const hooks = await createHooks({ client, directory: '/tmp' }, tool, async (action, payload) => {
      calls.push([action, payload]);
      return action === 'classify' ? decision(executor) : { reply: 'result', backend: 'codex' };
    });
    return { hooks, calls };
  };
  const message = (id) => ({ message: { id, model: { providerID: 'old', modelID: 'old' } },
    parts: [{ type: 'text', text: 'hello' }] });

  test('every new user message is classified and open model is selected', async () => {
    const { hooks, calls } = await setup();
    for (const id of ['one', 'two']) {
      const out = message(id);
      await hooks['chat.message']({ sessionID: 's', agent: 'build' }, out);
      assert.deepEqual(out.message.model, { providerID: 'vercel', modelID: 'test' });
    }
    assert.equal(calls.length, 2);
  });

  test('premium gate prevents native shell from bypassing the selected executor', async () => {
    const { hooks } = await setup('premium');
    await hooks['chat.message']({ sessionID: 's' }, message('one'));
    await assert.rejects(hooks['tool.execute.before']({ sessionID: 's', tool: 'bash' }, { args: {} }), /aos_execute/);
    await hooks['tool.execute.before']({ sessionID: 's', tool: 'aos_execute' }, { args: {} });
  });

  test('duplicate premium tool calls cannot execute the same task twice', async () => {
    const { hooks, calls } = await setup('premium');
    await hooks['chat.message']({ sessionID: 's' }, message('one'));
    const ctx = { sessionID: 's', directory: '/tmp', metadata() {}, ask: async () => {} };
    await hooks.tool.aos_execute.execute({}, ctx);
    await hooks.tool.aos_execute.execute({}, ctx);
    assert.equal(calls.filter(([action]) => action === 'execute').length, 1);
  });

  test('missing routing state fails closed rather than permitting tools', async () => {
    const { hooks } = await setup();
    await assert.rejects(hooks['tool.execute.before']({ sessionID: 'unknown', tool: 'bash' }, { args: {} }), /AOS/);
  });

  test('AOS instructions are reinjected into every system prompt', async () => {
    const { hooks } = await setup();
    await hooks['chat.message']({ sessionID: 's' }, message('one'));
    for (let i = 0; i < 2; i++) {
      const out = { system: [] };
      await hooks['experimental.chat.system.transform']({ sessionID: 's' }, out);
      assert.ok(out.system.join('\n').includes('AOS'));
    }
  });

  test('completed premium result survives restart without a second execution', async () => {
    let executions = 0;
    const saved = [{ info: { id: 'one', role: 'user', agent: 'build' }, parts: [{ type: 'text', text: 'task' }] },
      { info: { id: 'reply', role: 'assistant' }, parts: [{ type: 'tool', tool: 'aos_execute',
        state: { status: 'completed', output: '{"reply":"already done"}' } }] }];
    const hooks = await createHooks({ directory: '/tmp', client: { session: { messages: async () => ({ data: saved }) } } }, tool,
      async (action) => { if (action === 'execute') executions++; return decision('premium'); });
    assert.equal(await hooks.tool.aos_execute.execute({}, { sessionID: 's', directory: '/tmp', metadata() {} }), '{"reply":"already done"}');
    assert.equal(executions, 0);
  });

  test('unavailable host capability cannot produce a fabricated final answer', async () => {
    const { hooks } = await setup('unavailable');
    await hooks['chat.message']({ sessionID: 's' }, message('one'));
    const out = { text: 'I used the Codex app successfully' };
    await hooks['experimental.text.complete']({ sessionID: 's' }, out);
    assert.match(out.text, /non disponibile/);
    assert.doesNotMatch(out.text, /successfully/);
  });

  test('critical rejection starts no executor and remains retryable for approval', async () => {
    let executions = 0;
    const hooks = await createHooks({ directory: '/tmp', client: { session: { messages: async () => ({ data: [] }) } } }, tool,
      async action => { if (action === 'execute') { executions++; return { reply: 'done' }; }
        return { ...decision('main'), decision: { executor: 'main', backend: 'codex', verify: 'needs_approval' } }; });
    await hooks['chat.message']({ sessionID: 's', agent: 'build' }, message('one'));
    const ctx = { sessionID: 's', directory: '/tmp', metadata() {}, ask: async () => { throw Error('denied'); } };
    await assert.rejects(hooks.tool.aos_execute.execute({}, ctx), /denied/);
    assert.equal(executions, 0);
    await hooks.tool.aos_execute.execute({}, { ...ctx, ask: async () => {} });
    assert.equal(executions, 1);
  });

  test('actual route is present in final output regardless of stale TUI model label', async () => {
    const { hooks } = await setup();
    await hooks['chat.message']({ sessionID: 's' }, message('one'));
    const out = { text: '42' };
    await hooks['experimental.text.complete']({ sessionID: 's' }, out);
    assert.match(out.text, /AOS.*T1\/LOW.*vercel\/test/);
    assert.ok(out.text.endsWith('42'));
  });

  test('an open turn can hand off a discovered host requirement through AOS', async () => {
    const calls = [];
    const client = { session: { messages: async () => ({ data: [{ info: { role: 'user', id: 'one' }, parts: [{ type: 'text', text: 'task' }] }] }) } };
    const hooks = await createHooks({ directory: '/tmp', client }, tool, async (action, payload) => {
      calls.push([action, payload]);
      return action === 'execute' ? { reply: 'host result' } : decision(calls.length > 1 ? 'premium' : 'open');
    });
    await hooks['chat.message']({ sessionID: 's', agent: 'build' }, message('one'));
    const result = await hooks.tool.aos_handoff.execute({ reason: 'Loaded skill requires a Claude runtime tool unavailable here. No changes made.' },
      { sessionID: 's', directory: '/tmp', metadata() {}, ask: async () => {} });
    assert.match(result, /host result/);
    assert.deepEqual(calls.map(c => c[0]), ['classify', 'classify', 'execute']);
    await assert.rejects(hooks['tool.execute.before']({ sessionID: 's', tool: 'bash' }), /aos_execute/);
  });

  test('Plan mode never starts a premium executor', async () => {
    const { hooks, calls } = await setup('premium');
    await hooks['chat.message']({ sessionID: 's', agent: 'plan' }, message('one'));
    await assert.rejects(hooks.tool.aos_execute.execute({}, { sessionID: 's' }), /Plan mode/);
    assert.equal(calls.filter(c => c[0] === 'execute').length, 0);
  });

  test('an aborted or failed executor cannot be silently retried', async () => {
    let executions = 0;
    const hooks = await createHooks({ directory: '/tmp', client: { session: { messages: async () => ({ data: [] }) } } }, tool,
      async action => { if (action === 'execute') { executions++; throw Error('aborted'); } return decision('premium'); });
    await hooks['chat.message']({ sessionID: 's', agent: 'build' }, message('one'));
    const context = { sessionID: 's', directory: '/tmp', metadata() {}, ask: async () => {} };
    await assert.rejects(hooks.tool.aos_execute.execute({}, context), /aborted/);
    await assert.rejects(hooks.tool.aos_execute.execute({}, context), /inspect state/);
    assert.equal(executions, 1);
  });

  test('a simultaneous premium call and a new turn cannot duplicate running work', async () => {
    let complete;
    const hooks = await createHooks({ directory: '/tmp', client: { session: { messages: async () => ({ data: [] }) } } }, tool,
      async action => action === 'execute' ? new Promise(resolve => { complete = resolve; }) : decision('premium'));
    await hooks['chat.message']({ sessionID: 's', agent: 'build' }, message('one'));
    const context = { sessionID: 's', directory: '/tmp', metadata() {}, ask: async () => {} };
    const first = hooks.tool.aos_execute.execute({}, context);
    await new Promise(resolve => setImmediate(resolve));
    await assert.rejects(hooks.tool.aos_execute.execute({}, context), /already running/);
    await assert.rejects(hooks['chat.message']({ sessionID: 's' }, message('two')), /still running/);
    complete({ reply: 'done' });
    await first;
  });

  test('compaction preserves the task constraints and approvals', async () => {
    const { hooks } = await setup();
    const output = { context: [] };
    await hooks['experimental.session.compacting']({}, output);
    assert.match(output.context.join('\n'), /approvals/);
    assert.match(output.context.join('\n'), /not treat a summary as a new authorization/);
  });

  test('OpenCode permission denial prevents launching a premium CLI', async () => {
    const { hooks, calls } = await setup('premium');
    await hooks['chat.message']({ sessionID: 's', agent: 'build' }, message('one'));
    const ctx = { sessionID: 's', directory: '/tmp', metadata() {}, ask: async ({ permission }) => {
      assert.equal(permission, 'aos_execute'); throw Error('host denied');
    } };
    await assert.rejects(hooks.tool.aos_execute.execute({}, ctx), /host denied/);
    assert.equal(calls.filter(c => c[0] === 'execute').length, 0);
  });
}


test('large tool histories remain bounded without losing user constraints or latest request', async () => {
  const { conversationText } = await import(bridge);
  const messages = [{ info: { role: 'user' }, parts: [{ type: 'text', text: 'Never deploy without approval.' }] },
    ...Array.from({length: 80}, () => ({ info: { role: 'assistant' }, parts: [{ type: 'tool', tool: 'write',
      state: { status: 'completed', input: { content: 'x'.repeat(10000) }, output: 'ok' } }] })),
    { info: { role: 'user' }, parts: [{ type: 'text', text: 'Now inspect the file.' }] }];
  const text = conversationText(messages);
  assert.ok(text.length <= 140000);
  assert.match(text, /Never deploy without approval/);
  assert.match(text, /Now inspect the file/);
  assert.match(text, /omitted/);
});


test('saved compaction summary replaces old user history for routing', async () => {
  const { conversationText } = await import(bridge);
  const text = conversationText([
    { info: { role: 'user' }, parts: [{ type: 'text', text: 'x'.repeat(100000) }] },
    { info: { role: 'assistant', summary: true }, parts: [{ type: 'text', text: 'Task summary: never deploy without approval.' }] },
    { info: { role: 'user' }, parts: [{ type: 'text', text: 'y'.repeat(100000) }] },
  ]);
  assert.ok(text.length <= 140000);
  assert.match(text, /never deploy without approval/);
  assert.ok(!text.includes('xxxx'));
});

test('premium clarification remains visible with an explicit incomplete status', async () => {
  const { createHooks } = await import(bridge);
  const tool = Object.assign(v => v, {schema:{string:()=>({describe(){return this;}})}});
  const hooks = await createHooks({directory:'/tmp',client:{session:{messages:async()=>({data:[]})}}},tool,
    async()=>({classification:{tier:'T2',risk:'LOW'},decision:{executor:'premium',backend:'codex'}}));
  await hooks['chat.message']({sessionID:'s'}, {message:{id:'one'},parts:[{type:'text',text:'task'}]});
  const out={text:'Quale file devo modificare?'};
  await hooks['experimental.text.complete']({sessionID:'s'},out);
  assert.match(out.text,/Quale file devo modificare/);
  assert.match(out.text,/non completata/);
});

{
  const { createHooks } = await import(bridge);
  const tool = Object.assign(v => v, {schema: {string: () => ({describe(){return this;}})}});
  const route = {classification:{tier:'T2',risk:'MEDIUM'},decision:{executor:'open',model:'open/primary',pipeline:true,planner:'codex',reviewer:'claude'}};
  test('role pipeline blocks native implementation and asks before premium planning', async () => {
    const calls=[];
    const hooks=await createHooks({directory:'/tmp',client:{session:{messages:async()=>({data:[]})}}},tool,
      async(action,payload)=>{calls.push([action,payload]);if(action==='classify')return route;
        return {stage:payload.action==='start'?'plan':'execute',planner:'codex',reviewer:'claude',checks:[]};});
    await hooks['chat.message']({sessionID:'s'}, {message:{id:'1'},parts:[{type:'text',text:'task'}]});
    await assert.rejects(hooks['tool.execute.before']({sessionID:'s',tool:'bash'}),/pipeline/);
    assert.ok(hooks.tool.aos_pipeline);
    const ask=[];
    await hooks.tool.aos_pipeline.execute({action:'plan',data:'{}'}, {sessionID:'s',directory:'/tmp',ask:async p=>ask.push(p),metadata(){}});
    assert.equal(ask[0].permission,'aos_plan');
    assert.ok(calls.some(([a,p])=>a==='pipeline'&&p.action==='plan'));
  });
  test('denied pipeline permission makes no role call', async () => {
    const calls=[];
    const hooks=await createHooks({directory:'/tmp',client:{session:{messages:async()=>({data:[]})}}},tool,
      async(action,payload)=>{calls.push([action,payload]);return action==='classify'?route:{stage:'plan'};});
    await hooks['chat.message']({sessionID:'s'}, {message:{id:'1'},parts:[]});
    assert.ok(hooks.tool.aos_pipeline);
    await assert.rejects(hooks.tool.aos_pipeline.execute({action:'plan',data:'{}'}, {sessionID:'s',directory:'/tmp',ask:async()=>{throw Error('denied');}}),/denied/);
    assert.equal(calls.filter(([a,p])=>a==='pipeline'&&p.action==='plan').length,0);
  });
  test('check permission preserves command prefixes and a denial stops execution', async () => {
    const calls=[];
    const hooks=await createHooks({directory:'/tmp',client:{session:{messages:async()=>({data:[]})}}},tool,
      async(action,payload)=>{calls.push([action,payload]);return action==='classify'?route:{stage:'verify'};});
    await hooks['chat.message']({sessionID:'s'}, {message:{id:'1'},parts:[]});
    await assert.rejects(hooks.tool.aos_pipeline.execute({action:'check',data:JSON.stringify({
      id:'unit',argv:['python3','-c',"print('literal; value')"]
    })}, {sessionID:'s',directory:'/tmp',ask:async request=>{
      assert.equal(request.permission,'bash');
      assert.ok(request.patterns[0].startsWith('python3 -c '));
      assert.ok(request.patterns[0].includes("'print("));
      throw Error('policy denied python3 *');
    }}),/policy denied/);
    assert.equal(calls.filter(([a,p])=>a==='pipeline'&&p.action==='check').length,0);
  });
}
