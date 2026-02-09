// Client-side hints & address suggestions (local); optional hook for Google Places
(function () {
  // Progress Steps Tracking
  const steps = document.querySelectorAll('.step');
  const sections = {
    1: ['business_legal_name', 'industry', 'legal_entity', 'ein', 'company_address1'],
    2: ['owner_0_first', 'owner_0_last', 'owner_0_ssn', 'owner_0_email'],
    3: ['own_real_estate'],
    4: ['bank_files'],
    5: ['esign_consent']
  };

  function updateProgressSteps() {
    let currentStep = 1;

    for (let step = 1; step <= 5; step++) {
      const fields = sections[step] || [];
      const hasValue = fields.some(name => {
        const el = document.querySelector(`[name="${name}"]`);
        if (!el) return false;
        if (el.type === 'file') return el.files && el.files.length > 0;
        if (el.type === 'checkbox') return el.checked;
        return el.value && el.value.trim() !== '';
      });

      if (hasValue && step >= currentStep) {
        currentStep = step;
      }
    }

    steps.forEach((stepEl, idx) => {
      const stepNum = idx + 1;
      stepEl.classList.remove('active', 'completed');

      if (stepNum < currentStep) {
        stepEl.classList.add('completed');
      } else if (stepNum === currentStep) {
        stepEl.classList.add('active');
      }
    });
  }

  // Debounce helper
  function debounce(fn, delay) {
    let timeout;
    return function(...args) {
      clearTimeout(timeout);
      timeout = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  const debouncedUpdate = debounce(updateProgressSteps, 150);

  // Listen for input changes
  document.querySelectorAll('input, select, textarea').forEach(el => {
    el.addEventListener('input', debouncedUpdate);
    el.addEventListener('change', debouncedUpdate);
  });

  // Initial update
  setTimeout(updateProgressSteps, 100);

  // Loading state for form submission
  const form = document.getElementById('appForm');
  const submitBtn = document.getElementById('submitBtn');

  if (form && submitBtn) {
    form.addEventListener('submit', function() {
      // Basic HTML5 validation check
      if (!form.checkValidity()) {
        return;
      }

      // Add loading state
      submitBtn.classList.add('loading');
      submitBtn.disabled = true;
    });
  }

  // Drag and drop for file upload
  const uploader = document.querySelector('.uploader');
  const fileInput = document.getElementById('bank_files');

  if (uploader && fileInput) {
    ['dragenter', 'dragover'].forEach(evt => {
      uploader.addEventListener(evt, (e) => {
        e.preventDefault();
        uploader.classList.add('drag-over');
      });
    });

    ['dragleave', 'drop'].forEach(evt => {
      uploader.addEventListener(evt, (e) => {
        e.preventDefault();
        uploader.classList.remove('drag-over');
      });
    });

    uploader.addEventListener('drop', (e) => {
      const files = e.dataTransfer.files;
      if (files.length > 0) {
        fileInput.files = files;
        debouncedUpdate();
      }
    });
  }
  const EIN_RE = /^(?!00)\d{2}-\d{7}$/;
  const SSN_RE = /^(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})$/;
  const PHONE_RE = /^\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$/;

  // Optional FICO: blank allowed, otherwise must be 300-850
  function isValidFico(v) {
    const s = (v || "").trim();
    if (!s) return true;
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
    const allowBlank = (opts && opts.allowBlank) ? true : false;

    function validate() {
      if (!isEnabled()) {
        hintInvalid(el, true);
        return;
      }
      const val = (el.value || "").trim();
      if (allowBlank && !val) {
        hintInvalid(el, true);
        return;
      }
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
      if (!isEnabled()) {
        hintInvalid(el, true);
        return;
      }
      hintInvalid(el, isValidFico(el.value));
    }

    el.addEventListener("input", validate);
    el.addEventListener("blur", validate);
    validate();
  }

  // Second owner toggle helper
  function isOwner1Enabled() {
    const sel = document.getElementById("has_owner_1");
    if (!sel) return false;
    return (sel.value || "").trim() === "Yes";
  }

  // Auto-format SSN: user types digits, dashes inserted automatically
  function autoFormatSSN(el) {
    if (!el) return;
    el.addEventListener('input', function() {
      var cursorPos = this.selectionStart;
      var oldLen = this.value.length;
      var digits = this.value.replace(/\D/g, '').substring(0, 9);
      var formatted;
      if (digits.length > 5) {
        formatted = digits.substring(0, 3) + '-' + digits.substring(3, 5) + '-' + digits.substring(5);
      } else if (digits.length > 3) {
        formatted = digits.substring(0, 3) + '-' + digits.substring(3);
      } else {
        formatted = digits;
      }
      if (this.value !== formatted) {
        this.value = formatted;
        var newPos = cursorPos + (formatted.length - oldLen);
        if (newPos < 0) newPos = 0;
        this.setSelectionRange(newPos, newPos);
      }
    });
  }

  // Auto-format EIN: user types digits, dash inserted after 2nd digit
  function autoFormatEIN(el) {
    if (!el) return;
    el.addEventListener('input', function() {
      var cursorPos = this.selectionStart;
      var oldLen = this.value.length;
      var digits = this.value.replace(/\D/g, '').substring(0, 9);
      var formatted;
      if (digits.length > 2) {
        formatted = digits.substring(0, 2) + '-' + digits.substring(2);
      } else {
        formatted = digits;
      }
      if (this.value !== formatted) {
        this.value = formatted;
        var newPos = cursorPos + (formatted.length - oldLen);
        if (newPos < 0) newPos = 0;
        this.setSelectionRange(newPos, newPos);
      }
    });
  }

  // Apply auto-formatting (runs before regex validation on the same input event)
  autoFormatSSN(document.querySelector('input[name="owner_0_ssn"]'));
  autoFormatSSN(document.querySelector('input[name="owner_1_ssn"]'));
  autoFormatEIN(document.querySelector('input[name="ein"]'));

  // Base validations
  bindRegexValidation('input[name="ein"]', EIN_RE);
  bindRegexValidation('input[name="owner_0_ssn"]', SSN_RE);
  bindRegexValidation('input[name="owner_0_mobile"]', PHONE_RE);

  // Added validations for new fields (owner 0 optional)
  bindFicoValidation('input[name="owner_0_fico"]');

  // Added validations for second owner, only when enabled
  bindRegexValidation('input[name="owner_1_ssn"]', SSN_RE, { isEnabled: isOwner1Enabled, allowBlank: false });
  bindRegexValidation('input[name="owner_1_mobile"]', PHONE_RE, { isEnabled: isOwner1Enabled, allowBlank: false });
  bindFicoValidation('input[name="owner_1_fico"]', { isEnabled: isOwner1Enabled });

  // Local address suggestions
  const KEY = "pc_addr_suggestions_v1";
  const dl = document.getElementById("addr_suggestions");

  function load() {
    try {
      return JSON.parse(localStorage.getItem(KEY) || "[]");
    } catch {
      return [];
    }
  }

  function save(list) {
    try {
      localStorage.setItem(KEY, JSON.stringify(list.slice(0, 10)));
    } catch {}
  }

  function render(list) {
    if (!dl) return;
    dl.innerHTML = "";
    list.forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a;
      dl.appendChild(opt);
    });
  }

  // Added owner_1_addr1 to suggestions (when enabled)
  const fields = ["company_address1", "owner_0_addr1", "owner_1_addr1"];
  const list = load();
  render(list);

  fields.forEach((name) => {
    const el = document.querySelector(`input[name="${name}"]`);
    if (!el) return;

    function shouldTrack() {
      if (name !== "owner_1_addr1") return true;
      return isOwner1Enabled();
    }

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

  // Optional Google Places hook
  function attachPlaces(input) {
    try {
      if (window.google && google.maps && google.maps.places) {
        new google.maps.places.Autocomplete(input, { types: ["address"] });
      }
    } catch {}
  }

  fields.forEach((name) => {
    const el = document.querySelector(`input[name="${name}"]`);
    if (el) attachPlaces(el);
  });

  // When Add Owner changes, re-run validations and re-attach places for owner_1 address if needed
  (function watchOwnerToggle() {
    const sel = document.getElementById("has_owner_1");
    if (!sel) return;

    sel.addEventListener("change", () => {
      const owner1Addr = document.querySelector('input[name="owner_1_addr1"]');
      if (owner1Addr) attachPlaces(owner1Addr);

      // Trigger blur to refresh border hints
      const owner1Fields = [
        'input[name="owner_1_ssn"]',
        'input[name="owner_1_mobile"]',
        'input[name="owner_1_fico"]',
      ];
      owner1Fields.forEach((q) => {
        const el = document.querySelector(q);
        if (el) el.dispatchEvent(new Event("blur"));
      });
    });
  })();
})();
