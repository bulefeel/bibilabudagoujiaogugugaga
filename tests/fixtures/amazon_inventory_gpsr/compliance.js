({traditional=false, count=1, selected=false, invalid=false, responsibleFirst=true,
   katRadio=false, disabledFirst=false, invalidFirst=false, blockedSelection=false,
   personReceiptStyle='legacy', personReceiptText=null, personInitiallyPending=false, manufacturerOnly=false, includeManufacturer=false, manufacturerSelected=false}={}) => {
 const panel=document.querySelector('kat-panel');
 window.complianceClicks=[];
 const labels=traditional?{entry:'修正刊登資訊問題',violation:'處理違規商品刊登資訊',person:'負責人資訊'}:
   {entry:'解决商品信息问题',violation:'解决商品信息违规问题',person:'负责人信息'};
 const show=html=>{panel.hidden=false;panel.shadowRoot.querySelector('button').hidden=false;panel.innerHTML='<div id="ActionPanelView">'+html+'</div>';};
 const ident=(sku,asin)=>'<div>SKU：'+sku+'</div><div>ASIN：'+asin+'</div>';
 for(const row of document.querySelectorAll('[data-sku]')){
   const sku=row.dataset.sku,asin=row.querySelector('a').href.split('/dp/')[1];
   const entry=row.querySelector('kat-link');entry.setAttribute('label',labels.entry);entry.textContent=labels.entry;
   const first=()=>{
     show(ident(sku,asin)+'<div data-testid="action-panel-body">'+
       '<div class="violation">'+labels.violation+'</div><div class="violation">'+labels.violation+'</div>'+
        (includeManufacturer?'<div class="violation">'+labels.violation+'</div>':'')+'</div>');
     panel.querySelectorAll('.violation').forEach((e,index)=>e.onclick=()=>{
       window.complianceClicks.push('violation:'+index);
       if(index===2){person('manufacturer');return;}
        (index===(responsibleFirst?0:1)?person:security)();
     });
   };
    const personReceipt=(kind='person')=>{
       const title=kind==='manufacturer'?(traditional?'製造商資訊':'制造商信息'):labels.person;
      const message=personReceiptText ?? (traditional?'您的提交正在審核中。':'您的提交正在审核中。');
      show('<h3>'+title+'</h3><div>SKU：'+sku+'</div>'+
        '<kat-card><div class="submitted"><p>'+message+'</p><b>Fixture Office</b></div></kat-card>'+
        '<kat-button label="'+(traditional?'關閉':'关闭')+'">'+(traditional?'關閉':'关闭')+'</kat-button>');
      panel.shadowRoot.querySelector('button').hidden=true;
      panel.querySelector('kat-button').onclick=()=>{
        window.complianceClicks.push('close-'+kind+'-receipt');panel.hidden=true;
      };
    };
    const person=(kind='person')=>{
       const title=kind==='manufacturer'?(traditional?'製造商資訊':'制造商信息'):labels.person;
       const initiallySelected=kind==='manufacturer'?manufacturerSelected:selected;
      if(personInitiallyPending){personReceipt(kind);return;}
     show('<button aria-label="back">返回</button><h3>'+title+'</h3><div>SKU：'+sku+'</div>'+
       Array.from({length:count},(_,i)=>{
         const badge=invalid||(invalidFirst&&i===0)?'失效':'有效';
         const input='<input type="radio" name="person" '+(initiallySelected&&i===0?'checked':'')+
           (disabledFirst&&i===0?' disabled':'')+'>';
         return '<kat-card><b>Fixture Office '+i+'</b><kat-badge label="'+badge+'">'+badge+'</kat-badge>'+
           (katRadio?'<kat-radiobutton style="display:inline-block;position:relative;width:20px;height:20px">'+input+
             '<span part="radiobutton-icon" style="position:absolute;inset:0;background:lightgray;border-radius:50%"></span></kat-radiobutton>':input)+'</kat-card>';
       }).join('')+
       '<kat-button id="send" '+(!initiallySelected?'disabled="true"':'')+'>送出</kat-button>');
     panel.querySelector('button').onclick=first;
     panel.querySelectorAll('input').forEach(e=>e.onchange=()=>{
       window.complianceClicks.push('select-'+kind);panel.querySelector('#send').removeAttribute('disabled');
     });
     panel.querySelectorAll('kat-radiobutton').forEach(host=>host.onclick=e=>{
       const input=host.querySelector('input');
       if(input.disabled || blockedSelection || e.target===input)return;
       input.click();
     });
     panel.querySelector('#send').onclick=()=>{
       window.complianceClicks.push('send-'+kind);
       if(window.noPersonReceipt)return;
        if(personReceiptStyle==='responsible'){personReceipt(kind);return;}
       show(ident(sku,asin)+'<div>您提交的内容正在审核中。将在七天内做出决定</div>');
     };
   };
   const security=()=>{
      if(manufacturerOnly){person("manufacturer");return;}
     show(ident(sku,asin)+'<h2>警告和安全信息</h2><p>商品文件</p><p>警告和安全图片</p>'+
       '<div role="button" aria-label="安全证明" id="card">安全证明 未开始</div>'+
       '<kat-button data-testid="primary-button" label="返回">返回</kat-button>');
     panel.querySelector('kat-button').onclick=first;
     panel.querySelector('#card').onclick=()=>{
       const declaration='如果您的产品没有任何警告和安全信息，请勾选“是”，因为即使没有此类信息，也可以安全地按预期使用。';
       show(ident(sku,asin)+'<h2>GPSR 安全认证</h2><form data-testid="form"><kat-checkbox name="value" label="'+declaration+'"><input type="checkbox"></kat-checkbox></form>'+
        '<kat-button data-testid="back-button" label="返回">返回</kat-button><kat-button data-testid="primary-button" label="保存" disabled="true">保存</kat-button>');
       panel.querySelector('[data-testid="back-button"]').onclick=security;
       panel.querySelector('kat-checkbox').onclick=e=>{
        if(e.target===e.currentTarget)e.currentTarget.querySelector('input').click();
      };
      panel.querySelector('input').onchange=e=>{
         window.complianceClicks.push('select-safety');panel.querySelector('kat-checkbox').setAttribute('value',String(e.target.checked));
         panel.querySelector('[data-testid="primary-button"]').removeAttribute('disabled');
       };
       panel.querySelector('[data-testid="primary-button"]').onclick=()=>{
         window.complianceClicks.push('save-safety');show(ident(sku,asin)+'<div>您提交的内容正在审核中。将在七天内做出决定</div>');
       };
     };
   };
   entry.onclick=()=>{window.complianceClicks.push('open:'+sku);first();};
 }
}
