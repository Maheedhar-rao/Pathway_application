// Client-side hints & address suggestions (local); optional hook for Google Places
(function(){
  const EIN_RE = /^(?!00)\d{2}-\d{7}$/;
  const SSN_RE = /^(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})$/;
  const PHONE_RE = /^\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$/;

  function hintInvalid(el, ok){ el.style.borderColor = ok ? '' : '#f87171'; }
  function bindValidation(sel, re){
    const el = document.querySelector(sel); if(!el) return;
    el.addEventListener('input', ()=> hintInvalid(el, re.test(el.value.trim())));
    el.addEventListener('blur', ()=> hintInvalid(el, re.test(el.value.trim())));
  }
  bindValidation('input[name="ein"]', EIN_RE);
  bindValidation('input[name="owner_0_ssn"]', SSN_RE);
  bindValidation('input[name="owner_0_mobile"]', PHONE_RE);

  // Local address suggestions
  const KEY = 'pc_addr_suggestions_v1';
  const dl = document.getElementById('addr_suggestions');
  function load(){ try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; } }
  function save(list){ try { localStorage.setItem(KEY, JSON.stringify(list.slice(0,10))); } catch {} }
  function render(list){ if(!dl) return; dl.innerHTML = ''; list.forEach(a => { const opt = document.createElement('option'); opt.value = a; dl.appendChild(opt); }); }
  const fields = ['company_address1','owner_0_addr1'];
  const list = load(); render(list);
  fields.forEach(name => {
    const el = document.querySelector(`input[name="${name}"]`);
    if(!el) return;
    el.addEventListener('change', ()=>{
      const v = el.value.trim(); if(!v) return;
      const idx = list.indexOf(v); if(idx >= 0) list.splice(idx,1);
      list.unshift(v); save(list); render(list);
    });
  });

  // Optional Google Places hook
  function attachPlaces(input){
    try { if(window.google && google.maps && google.maps.places){ new google.maps.places.Autocomplete(input, { types: ['address'] }); } } catch {}
  }
  fields.forEach(name => { const el = document.querySelector(`input[name="${name}"]`); if(el) attachPlaces(el); });
})();
