/* Shared setup, groceries, and meal follow-up. No framework or extra browser work. */
(function () {
  const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const css=document.createElement('style');
  css.textContent=`.flow-panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin:16px 0}.flow-panel h2{margin:0 0 10px}.flow-panel p{margin:8px 0}.flow-panel .frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}.flow-panel .frow>span{flex:1;min-width:140px}.flow-panel input{font:inherit;min-width:0;flex:1;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}.flow-panel button,.flow-panel a.btn{font:inherit;border:1px solid var(--line);border-radius:12px;padding:10px;background:var(--chip);color:var(--ink);cursor:pointer}.flow-panel .primary{background:var(--accent);color:var(--accent-ink)}.flow-panel .small{font-size:13px;color:var(--muted)}.flow-panel .notice{border-left:4px solid var(--warn);padding-left:10px}.flow-dialog{border:1px solid var(--line);border-radius:18px;background:var(--card);color:var(--ink);width:min(560px,94vw);max-height:90vh;overflow:auto;padding:18px}.flow-dialog::backdrop{background:#0008}.flow-dialog input[type=number]{width:80px}.flow-dialog .frow{justify-content:space-between}.flow-dialog h2{font-size:20px;text-transform:none;letter-spacing:normal}.flow-panel input[type=checkbox]{flex:none;width:20px;height:20px}.flow-panel button:disabled{opacity:.5} [hidden]{display:none!important}`;
  document.head.appendChild(css);
  async function request(path,body){
    const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const v=await r.json();if(!r.ok)throw Error(v.detail||'Please try again');return v;
  }
  function error(e){if(window.toast)window.toast(e.message);else console.error(e);}
  let main=document.querySelector('main');
  if(main.id==='app'){const host=document.createElement('main');host.id='trip-tools';host.style.paddingTop='0';main.style.paddingBottom='24px';main.after(host);main=host;}
  const path=location.pathname;
  if(['/dinners','/review','/pantry'].includes(path)){
    const panel=document.createElement('section');panel.className='flow-panel';
    panel.innerHTML=`<h2>Add groceries</h2><p class="small">For this trip, or the next one if a cart is already active. No dinner needed. Add one item at a time.</p><form class="frow"><input aria-label="Grocery item" maxlength="120" required placeholder="e.g. milk"><button type="submit">Add to list</button></form><p role="status" class="small"></p>`;
    main.appendChild(panel);
    panel.querySelector('form').onsubmit=async e=>{e.preventDefault();const input=panel.querySelector('input'),btn=panel.querySelector('button');btn.disabled=true;
      try{const r=await request('/api/groceries',{text:input.value});input.value='';panel.querySelector('[role=status]').textContent=r.key+' saved. Build or update the shopping list when ready.';if(window.onDataChanged)await window.onDataChanged();}
      catch(e){error(e);}finally{btn.disabled=false;}};
  }
  async function meals(){
    if(!['/dinners','/review','/pantry'].includes(path))return;
    const r=await request('/api/meals/pending');
    let panel=document.getElementById('meal-followup');
    if(!panel){panel=document.createElement('section');panel.id='meal-followup';panel.className='flow-panel';main.appendChild(panel);}
    panel.hidden=!r.meals.length;
    panel.innerHTML='<h2>Dinners still planned</h2><p class="small">Ingredients stay set aside until you cook the dinner or release them.</p>';
    for(const m of r.meals){const row=document.createElement('div');row.className='frow';
      row.innerHTML=`<span><b>${esc(m.name)}</b><br><small>${esc(new Date(m.date+'T12:00:00').toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'}))}</small></span><button>Cooked</button><button>Didn’t cook</button>`;
      const buttons=row.querySelectorAll('button');
      buttons[0].onclick=()=>resolve(m.key,'cooked');buttons[1].onclick=()=>resolve(m.key,'skipped');panel.appendChild(row);}
  }
  async function resolve(key,action){try{await request('/api/meals/resolve',{key,action});await meals();if(window.onDataChanged)await window.onDataChanged();}catch(e){error(e);}}
  window.refreshMealFollowup=meals;
  meals().catch(error);
  if(path==='/settings'){
    const panel=document.createElement('section');panel.id='setup';panel.className='flow-panel';main.insertBefore(panel,main.querySelector('h2'));
    async function setup(checked=false){
      const r=await request(checked?'/api/setup/check':'/api/setup',checked?{}:undefined);
      panel.innerHTML=`<h2>Get ready</h2><p class="small">Grocer ${esc(r.version)} · ${esc(r.revision)}. Docker health only checks that this app responds.</p>
        <p>Settings and tokens: <b>/data/grocer.env</b>. Restart Grocer after editing that file.</p>`;
      for(const [name,title] of [['mealie','Recipes from Mealie'],['ollama','Local model'],['notifications','Phone notifications']]){
        const c=r.checks[name];const row=document.createElement('p');row.textContent=title+': '+(c?c.message:(name==='notifications'&&!r.optional_notifications?'Optional, not configured':'Not checked yet'));panel.appendChild(row);}
      const p=document.createElement('p');p.textContent='Walmart store: '+(r.store.store_id?'#'+r.store.store_id+' · '+r.store.fulfillment.toLowerCase():'Choose a store below');panel.appendChild(p);
      const w=document.createElement('p');w.textContent='Walmart sign-in: '+(r.walmart.signed_in===true?'Last confirmed '+r.walmart.checked:r.walmart.signed_in===false?'Sign in again':'Not checked yet. Sign in using the link below.');panel.appendChild(w);
      const btn=document.createElement('button');btn.textContent='Check connections';btn.onclick=async()=>{btn.disabled=true;try{await setup(true);}catch(e){error(e);}finally{btn.disabled=false;}};panel.appendChild(btn);
    }
    setup().catch(error);
    const cleanup=document.createElement('section');cleanup.className='flow-panel';cleanup.id='ingredient-cleanup';main.appendChild(cleanup);
    async function proposal(){
      const {proposal:p}=await request('/api/cleanup');
      cleanup.innerHTML='<h2>Review duplicate ingredients</h2><p class="small">Scanning only suggests changes. The kept product and estimate survive. If it has no estimate, the other ingredient’s measured amount is preserved when possible. Estimates are not added together because they may describe the same food.</p><button id="scan-local">Find suggestions</button>';
      cleanup.querySelector('button').onclick=()=>window.startJob('/api/dedupe').then(proposal).catch(error);
      if(!p)return;
      if(p.state==='applied'){
        const b=document.createElement('button');b.textContent='Undo this cleanup';b.onclick=async()=>{try{await request('/api/cleanup/'+p.id+'/undo',{});await proposal();}catch(e){error(e);}};cleanup.appendChild(b);return;}
      if(p.state!=='proposed')return;
      p.groups.forEach((g,i)=>{
        const box=document.createElement('div');box.className='notice';
        box.innerHTML=`<label><input type="checkbox" data-group="${i}"> Keep <b>${esc(g.keep)}</b>; merge ${g.same.map(esc).join(', ')}</label>`;
        for(const name of [g.keep,...g.same]){const line=document.createElement('p');line.className='small';line.textContent=name+': '+(g.products[name]?.title||'No chosen product')+'; estimated stock '+(g.stock[name]?.packages??'unknown')+' packages';box.appendChild(line);}cleanup.appendChild(box);
      });
      if(p.groups.length){const b=document.createElement('button');b.textContent='Apply selected changes';b.onclick=async()=>{try{const groups=[...cleanup.querySelectorAll('input:checked')].map(x=>Number(x.dataset.group));await request('/api/cleanup/apply',{id:p.id,groups});await proposal();}catch(e){error(e);}};cleanup.appendChild(b);}
      else cleanup.insertAdjacentHTML('beforeend','<p>No suggested duplicates.</p>');
    }
    proposal().catch(error);
    window.refreshCleanup=proposal;
  }
})();
