// Field-level validation, auto-formatting, address suggestions, and the
// bank-statements multi-pick uploader. Wizard navigation lives inline in
// form.html so it can interact with the Jinja-rendered error state.
(function () {
  const EIN_RE   = /^(?!00)\d{2}-\d{7}$/;
  const SSN_RE   = /^(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})$/;
  const PHONE_RE = /^\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$/;

  function isValidFico(v) {
    const s = (v || "").trim();
    if (!s) return true;          // blank is allowed
    if (!/^\d{3}$/.test(s)) return false;
    const n = Number(s);
    return n >= 300 && n <= 850;
  }

  function hintInvalid(el, ok) {
    if (!el) return;
    el.style.borderColor = ok ? "" : "#f87171";
  }

  function bindRegexValidation(sel, re, opts) {
    const el = document.querySelector(sel);
    if (!el) return;
    const isEnabled = (opts && typeof opts.isEnabled === "function") ? opts.isEnabled : () => true;
    const allowBlank = !!(opts && opts.allowBlank);

    function validate() {
      if (!isEnabled()) { hintInvalid(el, true); return; }
      const val = (el.value || "").trim();
      if (allowBlank && !val) { hintInvalid(el, true); return; }
      hintInvalid(el, re.test(val));
    }
    el.addEventListener("input", validate);
    el.addEventListener("blur", validate);
    validate();
  }

  function bindFicoValidation(sel, opts) {
    const el = document.querySelector(sel);
    if (!el) return;
    const isEnabled = (opts && typeof opts.isEnabled === "function") ? opts.isEnabled : () => true;
    function validate() {
      if (!isEnabled()) { hintInvalid(el, true); return; }
      hintInvalid(el, isValidFico(el.value));
    }
    el.addEventListener("input", validate);
    el.addEventListener("blur", validate);
    validate();
  }

  function isOwner1Enabled() {
    const sel = document.getElementById("has_owner_1");
    return !!sel && (sel.value || "").trim() === "Yes";
  }

  function autoFormatSSN(el) {
    if (!el) return;
    el.addEventListener('input', function () {
      const cursorPos = this.selectionStart;
      const oldLen = this.value.length;
      const digits = this.value.replace(/\D/g, '').substring(0, 9);
      let formatted;
      if (digits.length > 5) formatted = digits.substring(0, 3) + '-' + digits.substring(3, 5) + '-' + digits.substring(5);
      else if (digits.length > 3) formatted = digits.substring(0, 3) + '-' + digits.substring(3);
      else formatted = digits;
      if (this.value !== formatted) {
        this.value = formatted;
        let newPos = cursorPos + (formatted.length - oldLen);
        if (newPos < 0) newPos = 0;
        this.setSelectionRange(newPos, newPos);
      }
    });
  }

  function autoFormatEIN(el) {
    if (!el) return;
    el.addEventListener('input', function () {
      const cursorPos = this.selectionStart;
      const oldLen = this.value.length;
      const digits = this.value.replace(/\D/g, '').substring(0, 9);
      const formatted = digits.length > 2 ? digits.substring(0, 2) + '-' + digits.substring(2) : digits;
      if (this.value !== formatted) {
        this.value = formatted;
        let newPos = cursorPos + (formatted.length - oldLen);
        if (newPos < 0) newPos = 0;
        this.setSelectionRange(newPos, newPos);
      }
    });
  }

  autoFormatSSN(document.querySelector('input[name="owner_0_ssn"]'));
  autoFormatSSN(document.querySelector('input[name="owner_1_ssn"]'));
  autoFormatEIN(document.querySelector('input[name="ein"]'));

  bindRegexValidation('input[name="ein"]', EIN_RE);
  bindRegexValidation('input[name="owner_0_ssn"]', SSN_RE);
  bindRegexValidation('input[name="owner_0_mobile"]', PHONE_RE);
  bindFicoValidation('input[name="owner_0_fico"]');

  bindRegexValidation('input[name="owner_1_ssn"]', SSN_RE, { isEnabled: isOwner1Enabled, allowBlank: false });
  bindRegexValidation('input[name="owner_1_mobile"]', PHONE_RE, { isEnabled: isOwner1Enabled, allowBlank: false });
  bindFicoValidation('input[name="owner_1_fico"]', { isEnabled: isOwner1Enabled });

  // Loading state on final submit
  const form = document.getElementById('appForm');
  const submitBtn = document.getElementById('submitBtn');
  if (form && submitBtn) {
    form.addEventListener('submit', function () {
      if (!form.checkValidity()) return;
      submitBtn.classList.add('loading');
      submitBtn.disabled = true;
    });
  }

  // Local address suggestions (datalist)
  const KEY = "pc_addr_suggestions_v1";
  const dl = document.getElementById("addr_suggestions");

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; }
  }
  function save(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, 10))); } catch {}
  }
  function render(list) {
    if (!dl) return;
    dl.innerHTML = "";
    list.forEach((a) => { const opt = document.createElement("option"); opt.value = a; dl.appendChild(opt); });
  }

  const addrFields = ["company_address1", "owner_0_addr1", "owner_1_addr1"];
  const list = load();
  render(list);

  addrFields.forEach((name) => {
    const el = document.querySelector(`input[name="${name}"]`);
    if (!el) return;
    const shouldTrack = () => name !== "owner_1_addr1" || isOwner1Enabled();
    el.addEventListener("change", () => {
      if (!shouldTrack()) return;
      const v = (el.value || "").trim();
      if (!v) return;
      const idx = list.indexOf(v);
      if (idx >= 0) list.splice(idx, 1);
      list.unshift(v);
      save(list);
      render(list);
    });
  });

  function attachPlaces(input) {
    try {
      if (window.google && google.maps && google.maps.places) {
        new google.maps.places.Autocomplete(input, { types: ["address"] });
      }
    } catch {}
  }
  addrFields.forEach((name) => {
    const el = document.querySelector(`input[name="${name}"]`);
    if (el) attachPlaces(el);
  });

  // Re-validate owner-1 fields when toggle flips so stale red borders clear
  (function watchOwnerToggle() {
    const sel = document.getElementById("has_owner_1");
    if (!sel) return;
    sel.addEventListener("change", () => {
      const owner1Addr = document.querySelector('input[name="owner_1_addr1"]');
      if (owner1Addr) attachPlaces(owner1Addr);
      ['input[name="owner_1_ssn"]','input[name="owner_1_mobile"]','input[name="owner_1_fico"]']
        .forEach((q) => { const el = document.querySelector(q); if (el) el.dispatchEvent(new Event("blur")); });
    });
  })();

  // Bank-statement multi-pick uploader (page 5) — accumulates across picks and
  // accepts drag-and-drop, mirroring the post-submit uploader on thank_you.html.
  (function bankUploader() {
    const input = document.getElementById('bank-files-input');
    const listEl = document.getElementById('bank-files-list');
    const drop = document.getElementById('bank-uploader');
    if (!input || !listEl || !drop || typeof DataTransfer === 'undefined') return;

    const files = [];
    const keyOf = (f) => f.name + '::' + f.size;
    function isPdf(f) {
      if (f.type === 'application/pdf') return true;
      if (!f.type && /\.pdf$/i.test(f.name)) return true;
      return false;
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
    }
    function renderList() {
      listEl.innerHTML = '';
      files.forEach((f, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = '<span style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escapeHtml(f.name) + '</span>';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute('aria-label', 'Remove ' + f.name);
        btn.textContent = '×';
        btn.style.cssText = 'background:none;border:0;color:inherit;cursor:pointer;font-size:16px;line-height:1;padding:0 2px;';
        btn.addEventListener('click', () => { files.splice(i, 1); sync(); });
        chip.appendChild(btn);
        listEl.appendChild(chip);
      });
    }
    function sync() {
      const dt = new DataTransfer();
      files.forEach((f) => dt.items.add(f));
      input.files = dt.files;
      renderList();
    }
    function addFiles(incoming) {
      const have = {};
      files.forEach((f) => { have[keyOf(f)] = true; });
      for (let i = 0; i < incoming.length; i++) {
        const f = incoming[i];
        if (!isPdf(f)) continue;
        const k = keyOf(f);
        if (have[k]) continue;
        files.push(f);
        have[k] = true;
      }
      sync();
    }

    input.addEventListener('change', () => addFiles(Array.prototype.slice.call(input.files || [])));
    ['dragenter','dragover'].forEach((ev) => {
      drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add('drag-over'); });
    });
    ['dragleave','drop'].forEach((ev) => {
      drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove('drag-over'); });
    });
    drop.addEventListener('drop', (e) => {
      const dropped = (e.dataTransfer && e.dataTransfer.files) ? Array.prototype.slice.call(e.dataTransfer.files) : [];
      addFiles(dropped);
    });
  })();
})();
