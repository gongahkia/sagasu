(function(root){
  function parseSearch(text, days, today){
    const q=text.toLowerCase().replace(/[–—]/g,'-');const warnings=[];
    let date=today, start='10:00', end='12:00', location='', invalidWeekday=false;
    const aliases=[[/\b(lkcsb|business)\b/,'LKCSB'],[/\b(soa|accountancy)\b/,'SOA'],[/\b(soe|economics|scis\s*2)\b/,'SOE/SCIS2'],[/\b(scis\s*1|scis|computing|sis)\b/,'SCIS1'],[/\b(lks|library|li ka shing)\b/,'LKS']];
    for(const [pattern,value] of aliases)if(pattern.test(q)){location=value;break;}
    if(/\b(tomorrow|tmr|tmrw|tom)\b/.test(q)){const d=new Date(today+'T12:00:00+08:00');d.setUTCDate(d.getUTCDate()+1);date=d.toISOString().slice(0,10);}
    const explicit=q.match(/\b\d{4}-\d{2}-\d{2}\b/);if(explicit)date=explicit[0];
    const weekday=q.match(/\b(sun(?:day)?|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?)\b/);
    if(weekday&&!explicit){const names=['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];const found=days.find(d=>(/\bnext\b/.test(q)?d>today:d>=today)&&new Date(d+'T12:00:00+08:00').getUTCDay()===names.findIndex(name=>name.startsWith(weekday[1])));if(found)date=found;else invalidWeekday=true;}
    const timeText=q.replace(/\bgsr\s+\d+-\d+\b/g,'').replace(/\b\d{4}-\d{2}-\d{2}\b/g,'');
    const range=timeText.match(/\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b/);
    function clock(h,m,suffix){h=Number(h);m=Number(m||0);if(m>59||h>23||(suffix&&(h<1||h>12)))return null;if(suffix)h=h%12+(suffix==='pm'?12:0);return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0');}
    let error=invalidWeekday?'That weekday is outside the published dates.':null;
    if(range){const inferredStart=range[3]||((range[6]==='pm'&&Number(range[1])>Number(range[4]))?'am':range[6]);start=clock(range[1],range[2],inferredStart);end=clock(range[4],range[5],range[6]||range[3]);}
    else {
      const at=q.match(/\b(?:at|from|after)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b/);
      const duration=q.match(/\b(?:for\s+)?(\d+(?:\.\d+)?)\s*(hours?|hrs?)\b/);
      if(at){start=clock(at[1],at[2],at[3]);if(start){const n=Number(start.slice(0,2))*60+Number(start.slice(3))+Number(duration?.[1]||2)*60;end=String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');}}
      else if(duration)error='Add a start time, for example “from 2pm for 2 hours”.';
      else if(/\d/.test(q.replace(/\d{4}-\d{2}-\d{2}/g,'').replace(/(?:scis\s*|gsr\s*)\d+(?:-\d+)?/g,'')))error='Use a time range such as “2-4pm” or “14:00-16:00”.';
    }
    if(!start||!end||start>=end||start<'08:00'||end>'22:00')error='Choose a time between 08:00 and 22:00, with the end after the start.';
    if(!days.includes(date))error='That date is outside the published snapshot. Choose one of the available dates.';
    const equipment=q.match(/\b(projector|whiteboard|screen|tv|hdmi|power outlets?)\b/g)||[];
    if(equipment.length)warnings.push(`Equipment is not in the public snapshot. Confirm ${[...new Set(equipment)].join(', ')} in FBS; these results do not verify it.`);
    if(/\b\d+\s*(people|persons?|seats?)\b/.test(q))warnings.push('Room capacity is not in the public snapshot; confirm it in FBS.');
    const room=q.match(/\bgsr\s+(\d+-\d+)\b/);const query=[location,room?'GSR '+room[1]:''].filter(Boolean).join(' ');
    if(location==='LKS')warnings.push('Library project rooms are not included in this snapshot.');
    if(q.trim()&&!location&&!range&&!weekday&&!explicit&&!/\b(today|tomorrow|tmr|tmrw|tom|from|at|hours?|room|study)\b/.test(q))warnings.push('Location not recognized. Try LKCSB, SOA, SCIS1 or SOE/SCIS2.');
    return {date,start,end,query,warnings,error,defaultTime:!range&&!/\b(at|from|after)\s+\d/.test(q)};
  }
  root.SagasuSearch={parseSearch};if(typeof module!=='undefined')module.exports={parseSearch};
})(typeof globalThis==='undefined'?this:globalThis);
