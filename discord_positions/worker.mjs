// Discord command endpoint. Positions remain in GitHub; no database or scanner runs here.
const enc = new TextEncoder();
export const positionId = p => p.position_id || `${p.event || ''}|${p.event_date || ''}|${p.platform || ''}|${p.listed_price ?? ''}`.toLowerCase().replace(/[^a-z0-9|.-]+/g, '-');
const open = p => (p.status || 'open').toLowerCase() === 'open';
const money = value => {
  const s = String(value);
  if (!/^\d+(\.\d{1,2})?$/.test(s) || !Number.isFinite(Number(s)) || Number(s) <= 0 || Number(s) > 1000000) throw Error('Enter a positive USD net payout with at most two decimal places.');
  return Number(s);
};
const textField = (v, label) => {
  if (typeof v !== 'string' || !v.trim() || v.length > 200 || /[\x00-\x1f]/.test(v)) throw Error(`Invalid ${label}.`);
  return v.trim();
};
const same = (a, b) => a.trim().toLowerCase() === b.trim().toLowerCase();
function dateField(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s || '') || !Number.isFinite(Date.parse(s)) || new Date(s).toISOString().slice(0,10) !== s) throw Error('Date must be YYYY-MM-DD.');
  const today = new Intl.DateTimeFormat('en-CA', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  if (s < today) throw Error('Cannot add a past event.');
  return s;
}
const revised = p => {
  const current = p.monitor_revision ?? 0;
  if (!Number.isSafeInteger(current) || current < 0 || current >= Number.MAX_SAFE_INTEGER) throw Error('Invalid monitoring revision. Nothing changed.');
  return current + 1;
};
const conflict = (doc, candidate, excluded) => doc.positions.some(q => q !== excluded && open(q)
  && same(q.event, candidate.event) && q.event_date === candidate.event_date && q.platform === candidate.platform);
const clip = value => String(value).slice(0, 200);

function positionPage(doc, args) {
  const status = args.status ?? 'open';
  if (!['open', 'closed', 'all'].includes(status)) throw Error('Choose open, closed or all.');
  const page = args.page ?? 1;
  if (!Number.isSafeInteger(page) || page < 1) throw Error('Page must be a positive whole number.');
  const rows = doc.positions.filter(p => status === 'all' || (status === 'open' ? open(p) : p.status === 'closed'));
  // Pack whole entries, never cut an ID. Reserve space for the heading/footer.
  const pages = [''];
  for (const p of rows) {
    const line = `\`${positionId(p)}\`\n${clip(p.event)} · ${clip(p.event_date)} · ${clip(p.venue || 'venue not set')} · ${clip(p.platform)} · ${clip(p.status || 'open')} · net $${Number(p.listed_price).toFixed(2)}`;
    if (line.length > 1500) throw Error('A saved position is too large to display; inspect positions.json.');
    if (pages.at(-1) && (pages.at(-1) + '\n\n' + line).length > 1500) pages.push('');
    pages[pages.length - 1] += (pages.at(-1) ? '\n\n' : '') + line;
  }
  if (page > pages.length) throw Error(`Page out of range. Choose 1–${pages.length}.`);
  const next = page < pages.length ? `\nNext: /position list status:${status} page:${page + 1}` : '';
  return `Positions (${status}) — page ${page}/${pages.length}\n\n${pages[page - 1] || 'No positions in this view.'}${next}\nSaved configuration, not a fresh price check.`;
}

export function applyCommand(doc, command, args, interactionId) {
  if (!Array.isArray(doc.positions)) throw Error('Position file is malformed; nothing changed.');
  const receipts = doc._discord_receipts || [];
  const prior = receipts.find(r => r.id === interactionId);
  const fingerprint = JSON.stringify([command, Object.entries(args).sort()]);
  if (prior) {
    if (prior.fingerprint !== fingerprint) throw Error('Interaction ID already used for different input.');
    return {changed:false, message:prior.message};
  }
  if (command === 'list') return {changed:false, message:positionPage(doc,args)};
  let message;
  if (command === 'add') {
    const p = {event:textField(args.event,'event'),event_date:dateField(args.date),venue:textField(args.venue,'venue'),platform:args.platform,listed_price:money(args.net_payout),status:'open',match_mode:'exact',monitor_revision:1};
    if (!['TickPick','StubHub'].includes(p.platform)) throw Error('Choose TickPick or StubHub.');
    if (conflict(doc,p)) throw Error('An open position already exists for this event/date/platform. Use update or stop.');
    p.position_id=positionId(p);
    if (doc.positions.some(q => positionId(q)===p.position_id)) throw Error('This position ID already exists in history. Use /position list status:closed, then update or resume it explicitly.');
    doc.positions.push(p);
    message=`Saved \`${p.position_id}\`.\n${p.event} · ${p.event_date} · ${p.venue} · ${p.platform}\nOne GA ticket; net payout $${p.listed_price.toFixed(2)}. Awaiting a scanner check; event and supply are not yet verified.`;
  } else if (['stop','update','resume'].includes(command)) {
    const matches=doc.positions.filter(p=>positionId(p)===args.position);
    if(matches.length!==1) throw Error('Position ID is missing or ambiguous. Use /position list status:all.');
    const p=matches[0];
    if(command==='stop') {
      if (open(p)) Object.assign(p, {status:'closed', monitor_revision:revised(p)});
      message=`Monitoring stopped for \`${positionId(p)}\` (${p.platform}). This does not delist the marketplace ticket. An alert already being delivered may still arrive.`;
    } else if (command==='resume') {
      if (open(p)) {
        message=`\`${positionId(p)}\` is already open. Monitoring history was not reset.`;
      } else {
        if (p.status !== 'closed') throw Error('Only stopped (closed) positions can be resumed. Sold history is not reopened.');
        if (!['TickPick','StubHub'].includes(p.platform)) throw Error('Only TickPick and StubHub positions can be resumed.');
        const next={...p,position_id:positionId(p),event:textField(p.event,'event'),event_date:dateField(p.event_date),venue:textField(p.venue,'venue; correct it with update first'),listed_price:money(p.listed_price),status:'open',match_mode:'exact',monitor_revision:revised(p)};
        if (conflict(doc,next,p)) throw Error('Another position is already open for this event/date/platform.');
        Object.assign(p,next);
        message=`Resumed \`${positionId(p)}\` (${p.platform}); net payout $${p.listed_price.toFixed(2)}. Awaiting a fresh scanner check.`;
      }
    } else {
      if (!open(p) && p.status !== 'closed') throw Error('Sold history cannot be edited.');
      const next={...p,position_id:positionId(p)};
      const fields=['event','date','venue','net_payout'];
      if (!fields.some(k=>args[k] !== undefined)) throw Error('Provide a correction: event, date, venue or net_payout.');
      if(args.event !== undefined) next.event=textField(args.event,'event');
      if(args.date !== undefined) next.event_date=dateField(args.date);
      if(args.venue !== undefined) next.venue=textField(args.venue,'venue');
      if(args.net_payout !== undefined) next.listed_price=money(args.net_payout);
      if(['event','date','venue'].some(k=>args[k] !== undefined)) {
        textField(next.venue,'venue; supply venue when correcting event identity');
        next.match_mode='exact';
      }
      if(open(next) && conflict(doc,next,p)) throw Error('Another position is already open for this event/date/platform.');
      const changed=['event','event_date','venue','listed_price','match_mode'].some(k=>next[k]!==p[k]);
      if(changed) next.monitor_revision=revised(p);
      Object.assign(p,next);
      message=`Updated \`${positionId(p)}\`: ${p.event} · ${p.event_date} · ${p.venue || 'venue not set'} · ${p.platform} · net $${p.listed_price.toFixed(2)}. ${open(p) ? 'Awaiting the next scanner check.' : 'Still closed; use /position resume to restart monitoring.'}`;
    }
  } else throw Error('Unknown command.');
  doc._discord_receipts=[...receipts,{id:interactionId,fingerprint,message}].slice(-200);
  return {changed:true,message};
}
const decode = s => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\s/g,'')),c=>c.charCodeAt(0)));
const encode = s => btoa(Array.from(enc.encode(s),c=>String.fromCharCode(c)).join(''));
async function github(env, init={}) {
  if(!/^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPO || '') || !env.GITHUB_TOKEN) throw Error('GitHub storage is not configured.');
  const url=`https://api.github.com/repos/${env.GITHUB_REPO}/contents/positions.json`;
  const resp=await fetch(init.method==='PUT' ? url : `${url}?ref=${encodeURIComponent(env.GITHUB_BRANCH || 'main')}`, {
    ...init, headers:{Authorization:`Bearer ${env.GITHUB_TOKEN}`,Accept:'application/vnd.github+json','User-Agent':'ticket-position-commands','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'},signal:AbortSignal.timeout(3000)
  });
  return resp;
}
export async function execute(env,command,args,id) {
  for(let attempt=0;attempt<3;attempt++) {
    const read=await github(env);
    if(!read.ok) throw Error(`GitHub read failed (${read.status}); no successful save confirmed.`);
    const file=await read.json();
    const doc=JSON.parse(decode(file.content));
    const result=applyCommand(doc,command,args,id);
    if(!result.changed) return result.message;
    const write=await github(env,{method:'PUT',body:JSON.stringify({message:`Discord position ${command} [skip ci]`,branch:env.GITHUB_BRANCH || 'main',sha:file.sha,content:encode(JSON.stringify(doc,null,2)+'\n')})});
    if(write.ok) return result.message;
    if(![409,422].includes(write.status)) throw Error(`GitHub write failed (${write.status}); save not confirmed. Check /position list before retrying.`);
  }
  throw Error('Concurrent file edits prevented saving. Check /position list, then retry.');
}
const response = (content,type=4) => Response.json({type,data:{content,flags:64,allowed_mentions:{parse:[]}}});
export default {
  async fetch(request,env,ctx) {
    if(request.method!=='POST') return new Response('Method not allowed',{status:405});
    const raw=await request.text(),stamp=request.headers.get('X-Signature-Timestamp'),sig=request.headers.get('X-Signature-Ed25519');
    try {
      if(!/^\d+$/.test(stamp || '') || Math.abs(Date.now()/1000-Number(stamp))>300 || !/^[a-f0-9]{128}$/i.test(sig || '') || !/^[a-f0-9]{64}$/i.test(env.DISCORD_PUBLIC_KEY || '')) throw Error();
      const bytes=s=>Uint8Array.from(s.match(/../g),x=>parseInt(x,16));
      const key=await crypto.subtle.importKey('raw',bytes(env.DISCORD_PUBLIC_KEY),{name:'Ed25519'},false,['verify']);
      if(!await crypto.subtle.verify('Ed25519',key,bytes(sig),enc.encode(stamp+raw))) throw Error();
    } catch { return new Response('Invalid signature',{status:401}); }
    let payload;try{payload=JSON.parse(raw);}catch{return new Response('Invalid JSON',{status:400});}
    if(payload.type===1) return Response.json({type:1});
    if(!env.DISCORD_USER_ID || !env.DISCORD_GUILD_ID || !env.DISCORD_CHANNEL_ID || payload.member?.user?.id!==env.DISCORD_USER_ID || payload.guild_id!==env.DISCORD_GUILD_ID || payload.channel_id!==env.DISCORD_CHANNEL_ID) return response('You cannot manage positions here.');
    if(payload.type!==2 || payload.data?.name!=='position') return response('Unsupported command.');
    const sub=payload.data.options?.[0];
    if(!sub || !['add','stop','update','resume','list'].includes(sub.name)) return response('Unsupported position command.');
    const args=Object.fromEntries((sub.options || []).map(o=>[o.name,o.value]));
    // Acknowledge immediately. GitHub reads/writes cannot block Discord's 3s response.
    ctx.waitUntil((async()=>{
      let content;
      try {content=await execute(env,sub.name,args,payload.id);}
      catch(e){content=`Position command not confirmed: ${e.message}. If a timeout occurred, use /position list to check the saved state.`;}
      // Keep one edit small; never emit secrets or allow mentions.
      if(content.length>1950) content='Response too large. Use /position list status:all page:1 to inspect the saved state.';
      const res=await fetch(`https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${payload.token}/messages/@original`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,allowed_mentions:{parse:[]}}),signal:AbortSignal.timeout(5000)});
      if(!res.ok) console.error('Discord command confirmation failed',res.status);
    })().catch(()=>console.error('Command completion failed; inspect saved positions.')));
    return response('Processing position command…',5);
  }
};
