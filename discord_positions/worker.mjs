// Discord command endpoint. Positions remain in GitHub; no database or scanner runs here.
import defaultFees from './seller_fees.json' with {type:'json'};
const enc = new TextEncoder();
export const positionId = p => p.position_id || `${p.event || ''}|${p.event_date || ''}|${p.platform || ''}|${p.listed_price ?? ''}`.toLowerCase().replace(/[^a-z0-9|.-]+/g, '-');
const open = p => (p.status || 'open').toLowerCase() === 'open';
const money = (value, label='amount') => {
  const s = String(value);
  if (!/^\d+(\.\d{1,2})?$/.test(s) || !Number.isFinite(Number(s)) || Number(s) <= 0 || Number(s) > 1000000) throw Error(`Enter a positive USD ${label} with at most two decimal places.`);
  return Number(s);
};
const feePercent = value => {
  const s=String(value);
  if(!/^\d+(\.\d{1,2})?$/.test(s) || !Number.isFinite(Number(s)) || Number(s)<0 || Number(s)>=100) throw Error('Seller fee must be a percentage from 0 to under 100, with at most two decimal places.');
  return Number(s);
};
function setPricing(p,args) {
  if(args.listing_price===undefined) throw Error('listing_price is required. Re-register /position if Discord still requires only net_payout.');
  if(args.seller_fee_pct!==undefined && args.net_payout!==undefined) throw Error('Provide seller_fee_pct or net_payout, not both.');
  const price=money(args.listing_price,'listing price');
  const wasListing=p.price_basis==='listing';
  const priceChanged=!wasListing || price!==p.listed_price;
  const fee=args.seller_fee_pct!==undefined ? feePercent(args.seller_fee_pct)
    : feePercent(wasListing ? p.seller_fee_pct : defaultFees[p.platform]);
  if(priceChanged || args.seller_fee_pct!==undefined) delete p.net_payout_override;
  if(args.net_payout!==undefined) {
    const payout=money(args.net_payout,'net payout');
    if(payout>price) throw Error('Net payout cannot exceed the listing price.');
    p.net_payout_override=payout;
  }
  p.seller_fee_source=args.seller_fee_pct!==undefined ? 'operator' : (wasListing ? p.seller_fee_source : 'default');
  Object.assign(p,{listed_price:price,price_basis:'listing',seller_fee_pct:fee});
}
export function pricingSummary(p) {
  if(!p.price_basis || p.price_basis==='net') return `Net payout $${Number(p.listed_price).toFixed(2)} (legacy entry; no further fee deduction)`;
  const price=money(p.listed_price,'listing price');
  if(p.net_payout_override!==undefined) return `Listing $${price.toFixed(2)}; payout $${Number(p.net_payout_override).toFixed(2)} (your override)`;
  const fee=feePercent(p.seller_fee_pct);
  // Integer cents and hundredths of a percent; round half up, matching Python.
  const cents=Math.floor((Math.round(price*100)*(10000-Math.round(fee*100))+5000)/10000);
  return `Listing $${price.toFixed(2)}; estimated payout $${(cents/100).toFixed(2)} (${fee}% ${p.seller_fee_source==='operator'?'your fee setting':'default fee estimate'})`;
}
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
export function applyCommand(doc, command, args, interactionId) {
  if (!Array.isArray(doc.positions)) throw Error('Position file is malformed; nothing changed.');
  const receipts = doc._discord_receipts || [];
  const prior = receipts.find(r => r.id === interactionId);
  const fingerprint = JSON.stringify([command, Object.entries(args).sort()]);
  if (prior) {
    if (prior.fingerprint !== fingerprint) throw Error('Interaction ID already used for different input.');
    return {changed:false, message:prior.message};
  }
  let message;
  if (command === 'list') {
    const rows = doc.positions.filter(open);
    message = rows.length ? rows.map(p => `\`${positionId(p)}\`\n${p.event} · ${p.event_date} · ${p.venue || 'venue not set'} · ${p.platform}\n${pricingSummary(p)}`).join('\n\n') : 'No open positions.';
    return {changed:false,message:message + '\nThis is saved configuration, not a fresh price check.'};
  }
  if (command === 'add') {
    const p = {event:textField(args.event,'event'),event_date:dateField(args.date),venue:textField(args.venue,'venue'),platform:args.platform,status:'open',match_mode:'exact'};
    if (!['TickPick','StubHub'].includes(p.platform)) throw Error('Choose TickPick or StubHub.');
    setPricing(p,args);
    if (doc.positions.some(q => open(q) && same(q.event,p.event) && q.event_date===p.event_date && q.platform===p.platform)) throw Error('An open position already exists for this event/date/platform. Use update or stop.');
    p.position_id=positionId(p);
    // Keep historical IDs unique. Reopening identical closed positions is explicit/manual.
    if (doc.positions.some(q => positionId(q)===p.position_id)) throw Error('This position ID already exists in history; it was not reopened.');
    doc.positions.push(p);
    message=`Saved \`${p.position_id}\`.\n${p.event} · ${p.event_date} · ${p.venue} · ${p.platform}\nOne GA ticket. ${pricingSummary(p)}. Awaiting a scanner check; event and supply are not yet verified.`;
  } else if (command==='stop' || command==='update') {
    const matches=doc.positions.filter(p=>positionId(p)===args.position);
    if(matches.length!==1) throw Error('Position ID is missing or ambiguous. Use /position list.');
    const p=matches[0];
    if(command==='stop') {
      p.status='closed';
      message=`Monitoring stopped for \`${positionId(p)}\` (${p.platform}). This does not delist the marketplace ticket. An alert already being delivered may still arrive.`;
    } else {
      if(!open(p)) throw Error('Cannot update a closed position.');
      if (!['TickPick','StubHub'].includes(p.platform)) throw Error('Only TickPick or StubHub positions support listing-price updates.');
      const updated={...p,position_id:positionId(p)}; // Preserve ID and validate before mutating.
      setPricing(updated,args);
      Object.assign(p,updated);
      if(updated.net_payout_override===undefined) delete p.net_payout_override;
      message=`Updated \`${positionId(p)}\`: ${pricingSummary(p)}. Awaiting the next scanner check.`;
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
    if(!sub || !['add','stop','update','list'].includes(sub.name)) return response('Unsupported position command.');
    const args=Object.fromEntries((sub.options || []).map(o=>[o.name,o.value]));
    // Acknowledge immediately. GitHub reads/writes cannot block Discord's 3s response.
    ctx.waitUntil((async()=>{
      let content;
      try {content=await execute(env,sub.name,args,payload.id);}
      catch(e){content=`Position command not confirmed: ${e.message}. If a timeout occurred, use /position list to check the saved state.`;}
      // Keep one edit small; never emit secrets or allow mentions.
      if(content.length>1950) content=content.slice(0,1850)+'\n…List truncated. Use a smaller active position set or inspect positions.json.';
      const res=await fetch(`https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${payload.token}/messages/@original`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,allowed_mentions:{parse:[]}}),signal:AbortSignal.timeout(5000)});
      if(!res.ok) console.error('Discord command confirmation failed',res.status);
    })().catch(()=>console.error('Command completion failed; inspect saved positions.')));
    return response('Processing position command…',5);
  }
};
