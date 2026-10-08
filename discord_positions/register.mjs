// Explicit operator setup step; never executed by scanner workflows.
const field=(name,description,extra={})=>({type:3,name,description,required:true,...extra});
const pricing=()=>[
 field('listing_price','Your asking price per ticket before seller fees, USD'),
 field('seller_fee_pct','Optional seller fee percentage, e.g. 15; saved for this position',{required:false}),
 field('net_payout','Optional exact payout shown by marketplace for this price; overrides estimate',{required:false}),
];
export const command={name:'position',description:'Manage one-ticket GA risk monitoring',default_member_permissions:'0',options:[
 {type:1,name:'add',description:'Save a position; exact CrowdVolt name, date and venue required',options:[field('event','Exact CrowdVolt event name'),field('date','Event date YYYY-MM-DD'),field('venue','Exact CrowdVolt venue'),field('platform','Where you listed',{choices:[{name:'StubHub',value:'StubHub'},{name:'TickPick',value:'TickPick'}]}),...pricing()]},
 {type:1,name:'stop',description:'Stop monitoring one position; does not delist the ticket',options:[field('position','Exact ID from /position list')]},
 {type:1,name:'update',description:'Change listing price without changing position ID',options:[field('position','Exact ID from /position list'),...pricing()]},
 {type:1,name:'list',description:'List saved open positions and IDs (not a fresh price check)'}]};
if(process.argv[1] && import.meta.url===new URL(process.argv[1],'file:').href){
 const {DISCORD_APPLICATION_ID,DISCORD_GUILD_ID,DISCORD_BOT_TOKEN}=process.env;
 if(!DISCORD_APPLICATION_ID || !DISCORD_GUILD_ID || !DISCORD_BOT_TOKEN) throw Error('Set app ID, guild ID and bot token in environment.');
 const r=await fetch(`https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/guilds/${DISCORD_GUILD_ID}/commands`,{method:'POST',headers:{Authorization:`Bot ${DISCORD_BOT_TOKEN}`,'Content-Type':'application/json'},body:JSON.stringify(command)});
 if(!r.ok)throw Error(`Registration failed: ${r.status}`);
 console.log('Registered /position. Grant your user command access in server Integrations settings.');
}
