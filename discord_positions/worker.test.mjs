import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,sign} from 'node:crypto';
import worker,{applyCommand,positionId,execute,pricingSummary} from './worker.mjs';
import {command} from './register.mjs';
const args={event:'Charlotte de Witte',date:'2099-08-08',venue:'Under the K Bridge',platform:'StubHub',listing_price:'137.00'};
const doc=()=>({positions:[]});
const fetchOriginal=globalThis.fetch;
afterEach(()=>globalThis.fetch=fetchOriginal);
test('add uses existing ID format and exact matching; stores gross price with explicit basis',()=>{
 const d=doc();applyCommand(d,'add',args,'1');
 assert.equal(positionId(d.positions[0]),'charlotte-de-witte|2099-08-08|stubhub|137');assert.equal(d.positions[0].listed_price,137);assert.equal(d.positions[0].match_mode,'exact');
 assert.equal(d.positions[0].price_basis,'listing');assert.equal(d.positions[0].seller_fee_pct,12.5);
});
test('same interaction delivery is idempotent',()=>{const d=doc();const a=applyCommand(d,'add',args,'1');assert.equal(applyCommand(d,'add',args,'1').message,a.message);assert.equal(d.positions.length,1);});
test('interaction reuse with different input rejected',()=>{const d=doc();applyCommand(d,'add',args,'1');assert.throws(()=>applyCommand(d,'add',{...args,listing_price:'5'},'1'));});
test('duplicate active position cannot overwrite price',()=>{const d=doc();applyCommand(d,'add',args,'1');assert.throws(()=>applyCommand(d,'add',{...args,listing_price:'5'},'2'));assert.equal(d.positions[0].listed_price,137);});
test('stop only one platform; repeat stop harmless',()=>{
 const d=doc();applyCommand(d,'add',args,'1');applyCommand(d,'add',{...args,platform:'TickPick'},'2');
 const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'3');applyCommand(d,'stop',{position:id},'4');assert.equal(d.positions[0].status,'closed');assert.equal(d.positions[1].status,'open');
});
test('update freezes legacy ID as well as new IDs',()=>{
 const d={positions:[{event:'Legacy',event_date:'2099-01-01',platform:'TickPick',listed_price:89,status:'open'}]};
 const id=positionId(d.positions[0]);applyCommand(d,'update',{position:id,listing_price:'88.75'},'1');assert.equal(positionId(d.positions[0]),id);assert.equal(d.positions[0].listed_price,88.75);assert.equal(d.positions[0].price_basis,'listing');assert.equal(d.positions[0].seller_fee_pct,15);
});
test('closed update and implicit reopen rejected',()=>{const d=doc();applyCommand(d,'add',args,'1');const id=positionId(d.positions[0]);applyCommand(d,'stop',{position:id},'2');assert.throws(()=>applyCommand(d,'update',{position:id,listing_price:90},'3'));assert.throws(()=>applyCommand(d,'add',args,'4'));});
test('bad dates, amounts, platforms rejected',()=>{
 for(const delta of [{date:'2099-02-30'},{date:'01/02/2099'},{date:'2000-01-01'},{listing_price:'NaN'},{listing_price:'Infinity'},{listing_price:'-5'},{listing_price:'0'},{listing_price:'1.001'},{listing_price:true},{platform:'VividSeats'},{platform:'Gametime'},{event:''},{venue:''}]) assert.throws(()=>applyCommand(doc(),'add',{...args,...delta},'1'),JSON.stringify(delta));
});
test('list is read only and warns not a price check',()=>{const d=doc();applyCommand(d,'add',args,'1');const snapshot=JSON.stringify(d);const r=applyCommand(d,'list',{},'2');assert.equal(JSON.stringify(d),snapshot);assert.match(r.message,/not a fresh price check/);});
test('default fee calculations and messages are explicitly estimated',()=>{
 for(const [platform,payout,fee] of [['TickPick','109.65',15],['StubHub','112.88',12.5]]){
  const d=doc();const r=applyCommand(d,'add',{...args,platform,listing_price:'129'},'1');
  assert.equal(d.positions[0].seller_fee_pct,fee);
  assert.ok(r.message.includes(`estimated payout $${payout}`));assert.match(r.message,/default fee estimate/);
  assert.ok(applyCommand(d,'list',{},'2').message.includes(`estimated payout $${payout}`));
 }
});
test('custom fee including zero persists through price updates with stable ID',()=>{
 for(const fee of ['10','0']){
  const d=doc();applyCommand(d,'add',{...args,seller_fee_pct:fee},'1');const id=positionId(d.positions[0]);
  const r=applyCommand(d,'update',{position:id,listing_price:'100'},'2');
  assert.equal(positionId(d.positions[0]),id);assert.equal(d.positions[0].seller_fee_pct,Number(fee));
  assert.ok(r.message.includes(`estimated payout $${fee==='10'?'90.00':'100.00'}`));assert.match(r.message,/your fee setting/);
 }
});
test('exact payout override is not double charged and clears when price changes',()=>{
 const d=doc();const r=applyCommand(d,'add',{...args,net_payout:'123.45'},'1');const id=positionId(d.positions[0]);
 assert.match(r.message,/payout \$123.45 \(your override\)/);assert.doesNotMatch(r.message,/estimated payout/);
 applyCommand(d,'update',{position:id,listing_price:'137'},'2');assert.equal(d.positions[0].net_payout_override,123.45);
 applyCommand(d,'update',{position:id,listing_price:'129'},'3');assert.ok(!('net_payout_override' in d.positions[0]));
 assert.match(pricingSummary(d.positions[0]),/estimated payout \$112.88/);assert.equal(positionId(d.positions[0]),id);
});
test('new fee clears an override and a new payout can be provided with a price change',()=>{
 const d=doc();applyCommand(d,'add',{...args,net_payout:'120'},'1');const id=positionId(d.positions[0]);
 applyCommand(d,'update',{position:id,listing_price:'137',seller_fee_pct:'10'},'2');assert.ok(!('net_payout_override' in d.positions[0]));
 applyCommand(d,'update',{position:id,listing_price:'129',net_payout:'119'},'3');assert.equal(d.positions[0].net_payout_override,119);
 assert.equal(d.positions[0].seller_fee_pct,10);
});
test('invalid fee or payout does not partly update the saved position',()=>{
 for(const delta of [{seller_fee_pct:'100'},{seller_fee_pct:'-1'},{seller_fee_pct:'Infinity'},{seller_fee_pct:'1.001'},{seller_fee_pct:true},{net_payout:'0'},{net_payout:'NaN'},{net_payout:'130'},{seller_fee_pct:'15',net_payout:'100'}]){
  const d=doc();applyCommand(d,'add',{...args,net_payout:'120'},'1');const snapshot=JSON.stringify(d);
  assert.throws(()=>applyCommand(d,'update',{position:positionId(d.positions[0]),listing_price:'129',...delta},'2'));
  assert.equal(JSON.stringify(d),snapshot);
 }
});
test('legacy net entries are displayed as net without modification',()=>{
 const d={positions:[{event:'Legacy',event_date:'2099-01-01',platform:'TickPick',listed_price:100,status:'open'}]};const snapshot=JSON.stringify(d);
 assert.match(applyCommand(d,'list',{},'1').message,/Net payout \$100.00 \(legacy entry/);assert.equal(JSON.stringify(d),snapshot);
});
test('outdated net-only command is rejected with a registration instruction',()=>{
 const {listing_price,...oldArgs}=args;
 assert.throws(()=>applyCommand(doc(),'add',{...oldArgs,net_payout:'100'},'1'),/listing_price is required.*Re-register/);
});
test('registration requires price and makes fee and payout overrides optional',()=>{
 for(const name of ['add','update']){
  const opts=command.options.find(o=>o.name===name).options;
  assert.equal(opts.find(o=>o.name==='listing_price').required,true);
  assert.equal(opts.find(o=>o.name==='seller_fee_pct').required,false);
  assert.equal(opts.find(o=>o.name==='net_payout').required,false);
  let optional=false;for(const o of opts){if(!o.required)optional=true;else assert.equal(optional,false);}
 }
});
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
