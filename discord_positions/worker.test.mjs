import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,sign} from 'node:crypto';
import worker,{applyCommand,positionId,execute} from './worker.mjs';
import {command} from './register.mjs';
const args={event:'Charlotte de Witte',date:'2099-08-08',venue:'Under the K Bridge',platform:'StubHub',net_payout:'137.00'};
const doc=()=>({positions:[]});
const fetchOriginal=globalThis.fetch;
afterEach(()=>globalThis.fetch=fetchOriginal);
test('add uses existing ID format and exact matching; payout is net',()=>{
 const d=doc();applyCommand(d,'add',args,'1');
 assert.equal(positionId(d.positions[0]),'charlotte-de-witte|2099-08-08|stubhub|137');assert.equal(d.positions[0].listed_price,137);assert.equal(d.positions[0].match_mode,'exact');
});
test('same interaction delivery is idempotent',()=>{const d=doc();const a=applyCommand(d,'add',args,'1');assert.equal(applyCommand(d,'add',args,'1').message,a.message);assert.equal(d.positions.length,1);});
test('interaction reuse with different input rejected',()=>{const d=doc();applyCommand(d,'add',args,'1');assert.throws(()=>applyCommand(d,'add',{...args,net_payout:'5'},'1'));});
test('duplicate active position cannot overwrite payout',()=>{const d=doc();applyCommand(d,'add',args,'1');assert.throws(()=>applyCommand(d,'add',{...args,net_payout:'5'},'2'));assert.equal(d.positions[0].listed_price,137);});
test('stop only one platform; repeat stop harmless',()=>{
 const d=doc();applyCommand(d,'add',args,'1');applyCommand(d,'add',{...args,platform:'TickPick'},'2');
 const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'3');applyCommand(d,'stop',{position:id},'4');assert.equal(d.positions[0].status,'closed');assert.equal(d.positions[1].status,'open');
});
test('update freezes legacy ID as well as new IDs',()=>{
 const d={positions:[{event:'Legacy',event_date:'2099-01-01',platform:'TickPick',listed_price:89,status:'open'}]};
 const id=positionId(d.positions[0]);applyCommand(d,'update',{position:id,net_payout:'88.75'},'1');assert.equal(positionId(d.positions[0]),id);assert.equal(d.positions[0].listed_price,88.75);
});
test('closed correction stays closed and implicit reopen is rejected',()=>{const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');applyCommand(d,'update',{position:id,net_payout:90},'3');assert.equal(d.positions[0].status,'closed');assert.throws(()=>applyCommand(d,'add',args,'4'));});
test('bad dates, amounts, platforms rejected',()=>{
 for(const delta of [{date:'2099-02-30'},{date:'01/02/2099'},{date:'2000-01-01'},{net_payout:'NaN'},{net_payout:'Infinity'},{net_payout:'-5'},{net_payout:'0'},{net_payout:'1.001'},{net_payout:true},{platform:'VividSeats'},{platform:'Gametime'},{event:''},{venue:''}]) assert.throws(()=>applyCommand(doc(),'add',{...args,...delta},'1'),JSON.stringify(delta));
});
test('list is read only and warns not a price check',()=>{const d=doc();applyCommand(d,'add',args,'1');const snapshot=JSON.stringify(d);const r=applyCommand(d,'list',{},'2');assert.equal(JSON.stringify(d),snapshot);assert.match(r.message,/not a fresh price check/);});
test('missing or ambiguous stop cannot close anything',()=>{assert.throws(()=>applyCommand(doc(),'stop',{position:'x'},'1'));const p={position_id:'x',status:'open'};assert.throws(()=>applyCommand({positions:[{...p},{...p}]},'stop',{position:'x'},'1'));});
test('malformed store rejected',()=>assert.throws(()=>applyCommand({},'list',{},'1')));
test('GitHub SHA conflict retries fresh content without losing other position',async()=>{
 let d=doc(),sha='one',puts=0;
 globalThis.fetch=async(_url,init)=>{
  if(init.method!=='PUT')return Response.json({sha,content:Buffer.from(JSON.stringify(d)).toString('base64')});
  const body=JSON.parse(init.body);puts++;
  if(puts===1){applyCommand(d,'add',{...args,platform:'TickPick'},'other');sha='two';return new Response('',{status:409});}
  assert.equal(body.sha,'two');d=JSON.parse(Buffer.from(body.content,'base64').toString());return Response.json({});
 };
 await execute({GITHUB_REPO:'owner/repo',GITHUB_TOKEN:'fake'},'add',args,'1');assert.equal(d.positions.length,2);assert.equal(puts,2);
});
test('read failure does not attempt write',async()=>{let calls=0;globalThis.fetch=async()=>{calls++;return new Response('',{status:503});};await assert.rejects(execute({GITHUB_REPO:'o/r',GITHUB_TOKEN:'fake'},'add',args,'1'));assert.equal(calls,1);});
test('persistent conflict is bounded',async()=>{let calls=0;globalThis.fetch=async(_u,i)=>{calls++;return i.method==='PUT'?new Response('',{status:409}):Response.json({sha:'x',content:Buffer.from(JSON.stringify(doc())).toString('base64')});};await assert.rejects(execute({GITHUB_REPO:'o/r',GITHUB_TOKEN:'fake'},'add',args,'1'));assert.equal(calls,6);});
const keys=generateKeyPairSync('ed25519');
const env={DISCORD_PUBLIC_KEY:keys.publicKey.export({format:'der',type:'spki'}).subarray(-32).toString('hex'),DISCORD_USER_ID:'owner',DISCORD_GUILD_ID:'guild',DISCORD_CHANNEL_ID:'channel',DISCORD_APPLICATION_ID:'app'};
function request(payload,change=false,stamp=String(Math.floor(Date.now()/1000))){const raw=JSON.stringify(payload);const sig=sign(null,Buffer.from(stamp+raw),keys.privateKey).toString('hex');return new Request('https://example.test',{method:'POST',headers:{'X-Signature-Timestamp':stamp,'X-Signature-Ed25519':sig},body:raw+(change?' ':'')});}
test('signed ping accepted',async()=>{const r=await worker.fetch(request({type:1}),env,{});assert.deepEqual(await r.json(),{type:1});});
test('tampered and stale signed requests rejected',async()=>{assert.equal((await worker.fetch(request({type:1},true),env,{})).status,401);assert.equal((await worker.fetch(request({type:1},false,'1'),env,{})).status,401);});
test('unauthorized actor rejected without GitHub access',async()=>{globalThis.fetch=()=>{throw Error('unexpected network');};const r=await worker.fetch(request({type:2,member:{user:{id:'other'}},guild_id:'guild',channel_id:'channel'}),env,{});assert.match((await r.json()).data.content,/cannot manage/);});
test('defer is immediate; completion catches storage failure',async()=>{
 let task;globalThis.fetch=async()=>Response.json({});
 const p={type:2,id:'123',token:'fake',guild_id:'guild',channel_id:'channel',member:{user:{id:'owner'}},data:{name:'position',options:[{name:'list'}]}};
 const r=await worker.fetch(request(p),env,{waitUntil:t=>task=t});assert.equal((await r.json()).type,5);await task;
});
test('registered commands limited to requested operations/platforms',()=>{assert.deepEqual(command.options.map(x=>x.name),['add','stop','update','resume','list']);assert.deepEqual(command.options[0].options.find(x=>x.name==='platform').choices.map(x=>x.value),['StubHub','TickPick']);});
test('wrong server and channel are rejected even for owner',async()=>{
 globalThis.fetch=()=>{throw Error('unexpected network');};
 for(const change of [{guild_id:'wrong'},{channel_id:'wrong'}]){
  const p={type:2,member:{user:{id:'owner'}},guild_id:'guild',channel_id:'channel',...change};
  assert.match((await (await worker.fetch(request(p),env,{})).json()).data.content,/cannot manage/);
 }
});
test('slow storage does not delay Discord acknowledgment',async()=>{
 let release,task;
 const waiting=new Promise(r=>release=r);
 globalThis.fetch=async(_u,init)=>init.method==='PATCH'?Response.json({}):waiting;
 const p={type:2,id:'456',token:'fake',guild_id:'guild',channel_id:'channel',member:{user:{id:'owner'}},data:{name:'position',options:[{name:'list'}]}};
 const r=await worker.fetch(request(p),{...env,GITHUB_REPO:'o/r',GITHUB_TOKEN:'fake'},{waitUntil:t=>task=t});
 assert.equal((await r.json()).type,5);
 release(Response.json({sha:'x',content:Buffer.from(JSON.stringify(doc())).toString('base64')}));
 await task;
});
test('stopped venue correction resumes with original ID and new assessment revision',()=>{
 const d=doc();applyCommand(d,'add',{...args,venue:'Wrong Venue'},'1');const id=positionId(d.positions[0]);
 applyCommand(d,'stop',{position:id},'2');
 const stoppedRevision=d.positions[0].monitor_revision;
 applyCommand(d,'update',{position:id,venue:args.venue},'3');
 assert.equal(d.positions[0].status,'closed');assert.equal(positionId(d.positions[0]),id);
 applyCommand(d,'resume',{position:id},'4');
 assert.equal(d.positions[0].status,'open');assert.equal(positionId(d.positions[0]),id);
 assert.equal(d.positions[0].venue,args.venue);assert.ok(d.positions[0].monitor_revision>stoppedRevision);
});
test('resume replays and repeated commands do not repeatedly reset cooldown',()=>{
 const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');
 applyCommand(d,'resume',{position:id},'3');const revision=d.positions[0].monitor_revision;
 applyCommand(d,'resume',{position:id},'3');applyCommand(d,'resume',{position:id},'4');
 assert.equal(d.positions[0].monitor_revision,revision);
});
test('resume cannot create duplicate active position or reopen a sold/past one',()=>{
 const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');
 applyCommand(d,'add',{...args,net_payout:'130'},'3');
 assert.throws(()=>applyCommand(d,'resume',{position:id},'4'),/already open/);
 assert.equal(d.positions[0].status,'closed');
 const old={...d.positions[0],status:'sold'};
 assert.throws(()=>applyCommand({positions:[old]},'resume',{position:id},'5'),/Sold/);
 const past={...old,status:'closed',event_date:'2000-01-01'};
 assert.throws(()=>applyCommand({positions:[past]},'resume',{position:id},'6'),/past/);
});
test('correction validates before mutation and rejects duplicate or empty edits',()=>{
 const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);
 const original=JSON.stringify(d);
 assert.throws(()=>applyCommand(d,'update',{position:id,event:'New title',net_payout:'NaN'},'2'));
 assert.equal(JSON.stringify(d),original);
 assert.throws(()=>applyCommand(d,'update',{position:id},'3'));
 applyCommand(d,'add',{...args,event:'Another Artist'},'4');
 assert.throws(()=>applyCommand(d,'update',{position:id,event:'Another Artist'},'5'),/already open/);
 assert.equal(d.positions[0].event,args.event);
});
test('event and date corrections preserve ID and identical update preserves revision',()=>{
 const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);
 applyCommand(d,'update',{position:id,event:'Correct Artist',date:'2099-08-09',net_payout:'123.45'},'2');
 assert.equal(positionId(d.positions[0]),id);assert.equal(d.positions[0].event_date,'2099-08-09');
 const revision=d.positions[0].monitor_revision;
 applyCommand(d,'update',{position:id,event:'Correct Artist',date:'2099-08-09',net_payout:'123.45'},'3');
 assert.equal(d.positions[0].monitor_revision,revision);
});
test('long lists expose every full ID within Discord message limits',()=>{
 const d=doc();
 for(let i=0;i<20;i++)applyCommand(d,'add',{...args,event:`Artist ${i} `+'Long Festival Name '.repeat(9),venue:'Venue '.repeat(30)},String(i));
 const snapshot=JSON.stringify(d);const ids=[];
 for(let page=1;;page++){
  const message=applyCommand(d,'list',{page},'read').message;
  assert.ok(message.length<=1950,message.length);
  ids.push(...Array.from(message.matchAll(/`([^`]+)`/g),m=>m[1]));
  const total=Number(message.match(/page \d+\/(\d+)/)[1]);
  if(page===total)break;
  assert.match(message,new RegExp(`page:${page+1}`));
 }
 assert.deepEqual(ids,d.positions.map(positionId));assert.equal(JSON.stringify(d),snapshot);
});
test('closed/all lists allow finding stopped IDs; empty and invalid pages are explicit',()=>{
 const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');
 assert.ok(applyCommand(d,'list',{status:'closed'},'3').message.includes(id));
 assert.ok(applyCommand(d,'list',{status:'all'},'4').message.includes(id));
 assert.ok(!applyCommand(d,'list',{},'5').message.includes(id));
 for(const page of [0,-1,1.5,'2',NaN,2])assert.throws(()=>applyCommand(d,'list',{page},'6'));
 assert.throws(()=>applyCommand(d,'list',{status:'unknown'},'7'));
});
test('full signed list request returns paginated content without truncation',async()=>{
 const d=doc();for(let i=0;i<10;i++)applyCommand(d,'add',{...args,event:`Artist ${i} `+'Long Name '.repeat(16)},String(i));
 let task,reply;
 globalThis.fetch=async(_url,init)=>{
  if(init.method==='PATCH'){reply=JSON.parse(init.body);return Response.json({});}
  assert.notEqual(init.method,'PUT');
  return Response.json({sha:'x',content:Buffer.from(JSON.stringify(d)).toString('base64')});
 };
 const payload={type:2,id:'list-page',token:'fake',guild_id:'guild',channel_id:'channel',member:{user:{id:'owner'}},data:{name:'position',options:[{name:'list',options:[{name:'page',value:2},{name:'status',value:'all'}]}]}};
 const r=await worker.fetch(request(payload),{...env,GITHUB_REPO:'o/r',GITHUB_TOKEN:'fake'},{waitUntil:t=>task=t});
 assert.equal((await r.json()).type,5);await task;
 assert.match(reply.content,/page 2\//);assert.ok(reply.content.length<=1950);assert.doesNotMatch(reply.content,/truncated/);
});
