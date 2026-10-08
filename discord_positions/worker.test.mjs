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
test('closed update and implicit reopen rejected',()=>{const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');assert.throws(()=>applyCommand(d,'update',{position:id,net_payout:90},'3'));assert.throws(()=>applyCommand(d,'add',args,'4'));});
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
test('registered commands limited to requested operations/platforms',()=>{assert.deepEqual(command.options.map(x=>x.name),['add','stop','update','list']);assert.deepEqual(command.options[0].options.find(x=>x.name==='platform').choices.map(x=>x.value),['StubHub','TickPick']);});
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
