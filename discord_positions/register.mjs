// Explicit operator setup step; never executed by scanner workflows.
const field=(name,description,extra={})=>({type:3,name,description,required:true,...extra});
export const command={name:'position',description:'Manage one-ticket GA risk monitoring',default_member_permissions:'0',options:[
 {type:1,name:'add',description:'Save a position; exact CrowdVolt name, date and venue required',options:[field('event','Exact CrowdVolt event name'),field('date','Event date YYYY-MM-DD'),field('venue','Exact CrowdVolt venue'),field('platform','Where you listed',{choices:[{name:'StubHub',value:'StubHub'},{name:'TickPick',value:'TickPick'}]}),field('net_payout','USD after seller fees; one unrestricted GA ticket')]},
 {type:1,name:'stop',description:'Stop monitoring one position; does not delist the ticket',options:[field('position','Exact ID from /position list')]},
 {type:1,name:'update',description:'Correct a position without changing its ID or reopening it',options:[field('position','Exact ID from /position list'),field('net_payout','USD after seller fees',{required:false}),field('event','Correct CrowdVolt event name',{required:false}),field('date','Correct event date YYYY-MM-DD',{required:false}),field('venue','Correct CrowdVolt venue',{required:false})]},
 {type:1,name:'resume',description:'Explicitly restart a stopped position with a fresh risk assessment',options:[field('position','Exact ID from /position list status:closed')]},
 {type:1,name:'list',description:'List saved positions and IDs (not a fresh price check)',options:[field('status','Which positions to show',{required:false,choices:['open','closed','all'].map(value=>({name:value,value}))}),{type:4,name:'page',description:'Page number (default 1)',required:false,min_value:1}]}]};
if(process.argv[1] && import.meta.url===new URL(process.argv[1],'file:').href){
 const {DISCORD_APPLICATION_ID,DISCORD_GUILD_ID,DISCORD_BOT_TOKEN}=process.env;
 if(!DISCORD_APPLICATION_ID || !DISCORD_GUILD_ID || !DISCORD_BOT_TOKEN) throw Error('Set app ID, guild ID and bot token in environment.');
 const r=await fetch(`https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/guilds/${DISCORD_GUILD_ID}/commands`,{method:'POST',headers:{Authorization:`Bot ${DISCORD_BOT_TOKEN}`,'Content-Type':'application/json'},body:JSON.stringify(command)});
 if(!r.ok)throw Error(`Registration failed: ${r.status}`);
 console.log('Registered /position. Grant your user command access in server Integrations settings.');
}
