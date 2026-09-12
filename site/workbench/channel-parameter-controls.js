(function(root){
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function label(key,value){
    const labels={quality:{low:'经济',medium:'标准',high:'高品质',auto:'自动',standard:'标准',hd:'高清'},background:{opaque:'不透明',transparent:'透明',auto:'自动'},output_format:{png:'PNG',jpeg:'JPEG',webp:'WebP'},resolution:{'480p':'480p · 流畅','720p':'720p · 高清','1080p':'1080p · 超清'}};
    const ratios={'1024x1024':'1:1','1536x1024':'3:2','1024x1536':'2:3','1792x1024':'7:4','1024x1792':'4:7','2048x2048':'1:1','2048x1152':'16:9','1152x2048':'9:16','3840x2160':'16:9','2160x3840':'9:16','2048x1536':'4:3','1536x2048':'3:4','1024x1280':'4:5','1280x1024':'5:4','1024x768':'4:3','768x1024':'3:4','1280x720':'16:9','720x1280':'9:16'};
    return key==='size'&&ratios[value]?value+' · '+ratios[value]:key==='duration'?value+' 秒':labels[key]?.[value]||value;
  }
  function fields(spec){
    return (spec.fields||[]).filter(f=>f.visible!==false).map(f=>f.key);
  }
  // 字段有优先级（按 spec.fields 顺序，越靠前越高）：某个值只有在「更高优先级字段的当前选择」下存在已发布组合时才可选。
  // 后台没发布的搭配在这里就置灰，用户点不动，不会被提交后再收敛成别的参数。
  function options(spec,current,key){
    const combos=spec.combinations||[],keys=fields(spec),idx=keys.indexOf(key),higher=keys.slice(0,idx),cur=(current&&current.values)||{};
    return [...new Set(combos.map(c=>String(c.values[key])))].map(value=>({
      value,
      enabled:combos.some(c=>String(c.values[key])===value&&higher.every(k=>!cur[k]||String(c.values[k])===String(cur[k]))),
    }));
  }
  function choose(spec,current,key,value){
    const wanted={...(current?.values||{}),[key]:value};
    const candidates=spec.combinations.filter(c=>String(c.values[key])===String(value));
    return candidates.sort((a,b)=>Object.keys(wanted).filter(k=>String(b.values[k])===String(wanted[k])).length-Object.keys(wanted).filter(k=>String(a.values[k])===String(wanted[k])).length)[0];
  }
  function mount(host,spec,onChange,initial,options_={}){
    let current=spec.combinations.find(c=>c.id===(initial||spec.default))||spec.combinations[0];
    let billingEnabled=options_.billingEnabled!==false;
    const priceText=()=>billingEnabled?'本次 '+Number(current?.points||0)+' 点 · 1 个产物':'内测期间免费 · 1 个产物';
    function render(note=''){
      if(!current){host.textContent='没有可选组合';return}
      host.innerHTML='<div class="cp-fields">'+spec.fields.filter(f=>f.visible!==false).map(f=>{
        return '<label>'+esc(f.label)+'<select data-cp-field="'+esc(f.key)+'">'+options(spec,current,f.key).map(o=>{
          const selected=String(current.values[f.key])===o.value;
          return '<option value="'+esc(o.value)+'"'+(o.enabled?'':' disabled title="当前组合下没有对应搭配"')+(selected?' selected':'')+'>'+esc(label(f.key,o.value))+(o.enabled?'':' · 当前不适用')+'</option>';
        }).join('')+'</select></label>';
      }).join('')+'</div><p class="cp-price">'+priceText()+'</p><p class="cp-note" role="status">'+esc(note||'灰色选项在当前组合下没有对应搭配，不可选')+'</p>';
      if(onChange)onChange(current);
    }
    host.onchange=e=>{
      if(!e.target.dataset.cpField)return;
      const key=e.target.dataset.cpField,before={...(current?.values||{})};
      current=choose(spec,current,key,e.target.value);
      const after=(current&&current.values)||{};
      const moved=fields(spec).filter(k=>k!==key&&String(before[k]??'')!==String(after[k]??'')).map(k=>label(k,after[k]));
      const base=billingEnabled?'已按合法组合更新相关参数，请确认点数。':'已按合法组合更新相关参数。';
      render((moved.length?('已自动调整：'+moved.join('、')+'。'):'')+base);
    };
    render();
    return {value:()=>current,setBillingEnabled:enabled=>{billingEnabled=!!enabled;const price=host.querySelector('.cp-price');if(price)price.textContent=priceText();if(!billingEnabled){const note=host.querySelector('.cp-note');if(note)note.textContent='灰色选项在当前组合下没有对应搭配，不可选'}}};
  }
  root.ChannelParameterControls={esc,choose,mount,label,options,fields};
})(typeof window==='undefined'?globalThis:window);
