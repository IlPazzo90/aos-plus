import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

export function runBridge(action, payload, directory, backend, signal) {
  return new Promise((resolve, reject) => {
    const args = [root + 'bin/aos-entry.py', action, '--directory', directory];
    if (backend) args.push('--backend', backend);
    const child = spawn('python3', args, { stdio: ['pipe', 'pipe', 'pipe'], signal });
    let out = '';
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.stderr.resume(); // Provider diagnostics never enter a shared prompt or log.
    child.on('error', reject);
    child.stdin.on('error', () => {});
    child.on('close', (code) => {
      try {
        const result = JSON.parse(out);
        if (code || result.error) throw new Error(result.error || `AOS bridge exit ${code}`);
        resolve(result);
      } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

function textOf(message) {
  return (message.parts || []).flatMap((part) => {
    if (part.type === 'text') return [part.text];
    // A premium result is a tool output; it is essential context for follow-ups.
    if (part.type === 'tool' && ['aos_execute', 'aos_handoff', 'aos_pipeline'].includes(part.tool) && part.state?.status === 'completed')
      return [part.state.output];
    if (part.type === 'tool') return [`Tool ${part.tool}: ${part.state?.status}. ${JSON.stringify(part.state?.input || {})}\n${String(part.state?.output || part.state?.error || '').slice(-4000)}`];
    if (part.type === 'file') return [`Attached file: ${part.filename || part.url} (${part.mime || 'unknown type'}). Read it if your runtime can access it; never invent its contents.`];
    return [];
  }).join('\n');
}

// Preserve every user constraint and compaction summary; spend the remaining
// budget on recent assistant/tool evidence. Omitted evidence is never approval.
export function conversationText(messages) {
  const summary = messages.findLastIndex(m => m.info.role === 'assistant' && m.info.summary === true && textOf(m).trim());
  if (summary >= 0) messages = messages.slice(summary);
  const limit = 140000;
  const notice = 'Older assistant/tool evidence omitted for size. Inspect current files before repeating work. Missing evidence never grants authorization.\n\n';
  const entries = messages.map(m => ({ text: `${m.info.role}:\n${textOf(m)}`,
    required: m.info.role === 'user' || m.info.summary === true }));
  const complete = entries.map(e => e.text).join('\n\n');
  if (complete.length <= limit) return complete;
  let remaining = limit - notice.length - entries.filter(e => e.required).reduce((n, e) => n + e.text.length + 2, 0);
  if (remaining < 0) throw new Error('AOS: user context exceeds safe routing size. Compact the session while preserving constraints before continuing.');
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.required) continue;
    const budget = Math.min(remaining, entry.text.length + 2);
    entry.text = budget > 100 ? entry.text.slice(-(budget - 2)) : '';
    remaining -= budget;
  }
  return notice + entries.filter(e => e.text).map(e => e.text).join('\n\n');
}

export async function createHooks({ client, directory }, tool, bridge = runBridge) {
  const states = new Map();
  const locks = new Map();
  const classifying = new Set();

  async function history(sessionID) {
    const result = await client.session.messages({ path: { id: sessionID } });
    if (result.error) throw new Error('AOS: session context unavailable');
    return result.data || [];
  }

  async function classify(sessionID, message, parts, agent, previous) {
    // Never keep a previous turn's authorization while a new turn is classifying.
    states.delete(sessionID);
    const past = previous ?? await history(sessionID);
    const context = conversationText(previous ?? [...past.filter((m) => m.info.id !== message.id),
      { info: { role: 'user', id: message.id }, parts }]);
    const result = await bridge('classify', { text: context }, directory);
    const state = { ...result, context, turn: message.id, agent: agent || message.agent,
      completed: false, result: null, attempted: false };
    states.set(sessionID, state);
    const label = `${result.classification.tier}/${result.classification.risk} · ${result.decision.model || result.decision.backend}`;
    await client.tui?.showToast({ body: { title: 'AOS', message: label, variant: 'info' } }).catch(() => {});
    return state;
  }

  async function stateFor(sessionID) {
    if (states.has(sessionID)) return states.get(sessionID);
    // A restarted server must reconstruct context and reclassify, not trust stale state.
    const messages = await history(sessionID);
    const latest = messages.findLast((m) => m.info.role === 'user');
    if (!latest) throw new Error('AOS: no user request to classify');
    const state = await classify(sessionID, latest.info, latest.parts, latest.info.agent, messages);
    const after = messages.slice(messages.indexOf(latest) + 1);
    const prior = after.flatMap(m => m.parts || []).findLast(p => p.type === 'tool' && ['aos_execute', 'aos_handoff'].includes(p.tool));
    if (prior) {
      state.attempted = true;
      if (prior.state?.status === 'completed') {
        state.completed = true;
        state.result = prior.state.output;
      }
    }
    const pipelineCalls = after.flatMap(m => m.parts || []).filter(p => p.type === 'tool' && p.tool === 'aos_pipeline');
    const last = pipelineCalls.at(-1);
    if (last) {
      if (last.state?.status !== 'completed') state.pipelineInterrupted = true;
      else {
        try { state.pipeline = JSON.parse(last.state.output); }
        catch { state.pipelineInterrupted = true; }
      }
    }
    return state;
  }

  const hooks = {
    'chat.message': async (input, output) => {
      if (locks.has(input.sessionID) || classifying.has(input.sessionID))
        throw new Error('AOS: executor or classifier still running; stop it before a new turn');
      classifying.add(input.sessionID);
      try {
        const state = await classify(input.sessionID, output.message, output.parts, input.agent);
        const model = state.decision.executor === 'open' ? state.decision.model : state.decision.coordinator_model;
        if (model) {
          const [providerID, ...rest] = model.split('/');
          output.message.model = { providerID, modelID: rest.join('/') };
        }
      } finally { classifying.delete(input.sessionID); }
    },
    'experimental.chat.system.transform': async (input, output) => {
      if (!input.sessionID) return;
      const state = await stateFor(input.sessionID);
      const aos = await readFile(root + 'SKILL.md', 'utf8');
      output.system.push(`AOS GLOBAL ENTRY — applies to this request, including non-software work.
${aos}
ENTRY OVERRIDE: Every domain is classified. Apply the software process only to
software work; use domain skills for writing, research and business. Do not invoke
another classifier or delegate the same open task: the runtime already selected
this turn's executor. You may delegate bounded independent subtasks when needed.
Use shared skills natively and skill-library for on-demand sources. A skill's
instructions do not make an absent tool available. Respect actual tool availability.
If a loaded skill requires a missing Claude/Codex runtime capability, call
aos_handoff with the missing capability and work already performed. AOS will
reclassify before handing off. Do not simulate unavailable plugin tools. Do not
repeat work already performed by a prior executor. The new runtime must preserve
the same working directory and project instructions.
The following runtime route is authoritative:
${JSON.stringify(state.classification)}
${JSON.stringify(state.decision)}
Begin your visible response with: AOS · ${state.classification.tier}/${state.classification.risk} · ${state.decision.executor === 'open' ? state.decision.model : [state.decision.backend, state.decision.premium_model].filter(Boolean).join('/')}
If decision.pipeline is true, use aos_pipeline exclusively for task changes:
plan (premium read-only), execute (guarded open worker), check (host-authorized argv
for each plan.tests identifier, plus diff and security), verify, review (opposite
premium read-only), check and arbitrate every finding, execute for open fixes,
then check/verify/review again. Tool responses contain authoritative stage and plan.
For arbitration supply verdicts [{id,confirmed,evidence,check_ids}]; reproduce every
claim mechanically through check and explain confirmation or counterevidence.
Never use native write/bash/task to implement, retry or fix pipeline work. Never
invent checks or skip blocked stages. If stage is escalate, explain the exhausted
open attempts and use escalate; premium execution is never the default. A blocked
or interrupted pipeline is incomplete. T3 executes each bounded subtask in order.
Send only relevant check output and delta context, never the repository by default.
If executor is premium or verify is needs_approval, call aos_execute. That tool
supplies the complete user conversation and handles any required approval. Do not
execute the task through native tools. If unavailable, explain the missing host
capability and stop. If executor is open, do the work in this session on the selected
model. Announce the selected route briefly when it is useful. After aos_execute,
report its result and limitations faithfully; do not invent verification or repeat
side effects. A backend error is not permission to execute the work yourself.
An AOS route is not authorization for unrelated actions or external messages.`);
    },
    'tool.execute.before': async (input) => {
      const state = await stateFor(input.sessionID);
      if (state.decision.pipeline && !['aos_pipeline', 'question', 'read', 'glob', 'grep', 'skill'].includes(input.tool))
        throw new Error('AOS: role pipeline requires aos_pipeline; native implementation is disabled');
      if (state.decision.executor === 'unavailable') throw new Error('AOS: required host capability unavailable');
      if ((state.decision.executor === 'premium' || state.decision.verify === 'needs_approval')
          && !['aos_execute', 'question'].includes(input.tool))
        throw new Error('AOS: this request must use aos_execute, not native tools');
    },
    'experimental.text.complete': async (input, output) => {
      const state = await stateFor(input.sessionID);
      if (state.decision.executor === 'unavailable') {
        output.text = 'AOS: capacità richiesta non disponibile in OpenCode. Apri il runtime richiesto; nessuna esecuzione dichiarata.';
        return;
      }
      if (state.decision.pipeline && state.pipeline?.stage !== 'pass')
        output.text = `AOS: pipeline incompleta (${state.pipeline?.stage || 'plan'}). Nessun PASS verificato.\n\n${output.text}`;
      if ((state.decision.executor === 'premium' || state.decision.verify === 'needs_approval') && !state.completed)
        output.text = `AOS: esecuzione premium non completata. Il testo del coordinatore seguente non è un risultato verificato.\n\n${output.text}`;
      const executor = state.decision.executor === 'open' ? state.decision.model :
        [state.decision.backend, state.decision.premium_model].filter(Boolean).join('/');
      const prefix = `AOS · ${state.classification.tier}/${state.classification.risk} · ${executor}`;
      if (!output.text.startsWith(prefix)) output.text = `${prefix}\n\n${output.text}`;
    },
    'experimental.session.compacting': async (_input, output) => {
      output.context.push('Preserve the latest user task, constraints, approvals, executor, checks and unresolved work. AOS reclassifies after restart; do not treat a summary as a new authorization.');
    },
    tool: {
      aos_pipeline: tool({
        description: 'Advance the role pipeline one stage. plan/execute/check/verify/review/arbitrate/escalate. data is JSON; check uses {id,argv}; arbitrate uses {verdicts:[{id,confirmed,evidence,check_ids}]}. State and model are owned by AOS. Never bypass a denial.',
        args: { action: tool.schema.string().describe('Pipeline action'), data: tool.schema.string().describe('JSON object; {} when no arguments') },
        async execute({ action, data }, context) {
          const state = await stateFor(context.sessionID);
          if (!state.decision.pipeline) throw new Error('AOS: no role pipeline on this route');
          if (locks.has(context.sessionID) || classifying.has(context.sessionID)) throw new Error('AOS: pipeline busy');
          if (state.pipelineInterrupted) throw new Error('AOS: interrupted stage; inspect work before an explicit new request');
          if (state.agent === 'plan' && !['plan'].includes(action)) throw new Error('AOS: Plan mode cannot execute pipeline work');
          const payload = JSON.parse(data);
          if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw new Error('AOS: object required');
          locks.set(context.sessionID, true);
          try {
            if (!state.pipeline) state.pipeline = await bridge('pipeline', { action: 'start',
              data: { classification: state.classification, text: state.context } }, context.directory);
            let permission = 'aos_' + action;
            let patterns = [action];
            if (action === 'check') {
              permission = 'bash';
              if (payload.id === 'diff') payload.argv = ['git', 'diff', '--check'];
              if (payload.id === 'security') payload.argv = ['bash', root + 'bin/aos-security.sh'];
              if (!Array.isArray(payload.argv) || !payload.argv.length || payload.argv.some(a => typeof a !== 'string' || !a))
                throw new Error('AOS: check requires argv');
              // Preserve ordinary command prefixes for host deny patterns; quote shell metacharacters.
              patterns = [payload.argv.map(a => /^[A-Za-z0-9_./:=+-]+$/.test(a) ? a : "'" + a.replaceAll("'", "'\\''") + "'").join(' ')];
            }
            if (['plan','execute','review','check','escalate'].includes(action)) {
              await context.ask({ permission, patterns, always: [], metadata: {
                directory: context.directory, role: state.pipeline.role, stage: state.pipeline.stage,
                model: action === 'plan' ? state.pipeline.planner : action === 'review' ? state.pipeline.reviewer : state.pipeline.model,
                argv: payload.argv, reason: 'Execute only this pipeline stage under existing host and worker permissions.' } });
            }
            state.pipelineInterrupted = true;
            const result = await bridge('pipeline', { state: state.pipeline, action, data: payload },
              context.directory, undefined, context.abort);
            state.pipeline = result;
            state.pipelineInterrupted = false;
            return JSON.stringify(result);
          } finally { locks.delete(context.sessionID); }
        },
      }),
      aos_handoff: tool({
        description: 'Report a missing host capability discovered during an open task. AOS reclassifies the original task and may hand off to a premium CLI. Describe completed work to prevent repetition; this is not a new user authorization.',
        args: { reason: tool.schema.string().describe('Missing runtime capability and work already performed (max 4000 characters).') },
        async execute({ reason }, context) {
          const state = await stateFor(context.sessionID);
          if (state.decision.executor !== 'open' || locks.has(context.sessionID) || classifying.has(context.sessionID))
            throw new Error('AOS: handoff is only available during an idle open route');
          if (!reason?.trim() || reason.length > 4000) throw new Error('AOS: invalid handoff reason');
          classifying.add(context.sessionID);
          try {
            const past = await history(context.sessionID);
            const conversation = conversationText(past);
            const text = `${conversation}\n\nAssistant capability finding (not new authorization): ${reason}`;
            const result = await bridge('classify', { text }, directory);
            if (result.decision.executor !== 'premium' && result.decision.verify !== 'needs_approval')
              throw new Error('AOS: no premium route available for the reported capability');
            Object.assign(state, result, { context: text });
          } finally { classifying.delete(context.sessionID); }
          return hooks.tool.aos_execute.execute({}, context);
        },
      }),
      aos_execute: tool({
        description: 'Execute the current AOS-routed premium task using its original conversation. No task/model arguments: routing is owned by AOS. Repeated calls return the same result.',
        args: {},
        async execute(_args, context) {
          const state = await stateFor(context.sessionID);
          if (state.completed) return state.result;
          if (locks.has(context.sessionID)) throw new Error('AOS: executor already running');
          if (state.attempted) throw new Error('AOS: execution failed; inspect state before explicitly requesting a retry');
          if (state.decision.executor !== 'premium' && state.decision.verify !== 'needs_approval')
            throw new Error('AOS: this request was not routed to a premium executor');
          if (state.agent === 'plan') throw new Error('AOS: Plan mode does not launch an executor. Switch to AOS/build to run the routed task.');
          locks.set(context.sessionID, true);
          try {
            if (state.decision.verify === 'needs_approval')
              await context.ask({ permission: 'aos_critical', patterns: [state.turn], always: [],
                metadata: { reason: state.classification.reason, backend: state.decision.backend } });
            await context.ask({ permission: 'aos_execute', patterns: [state.decision.backend], always: [],
              metadata: { backend: state.decision.backend, directory: context.directory,
                reason: 'Run the routed task in the named CLI with that runtime permissions.' } });
            state.attempted = true;
            context.metadata({ title: `AOS · ${state.decision.backend} · ${state.classification.tier}/${state.classification.risk}` });
            const result = await bridge('execute', { text: `Classification: ${JSON.stringify(state.classification)}\n\n${state.context}` },
              context.directory, state.decision.backend, context.abort);
            state.result = JSON.stringify(result);
            state.completed = true;
            return state.result;
          } finally { locks.delete(context.sessionID); }
        },
      }),
    },
  };
  return hooks;
}
