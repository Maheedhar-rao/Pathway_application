// Plaid Link widget: "Connect your bank" as an alternative to uploading
// bank-statement PDFs. Mounted on every [data-plaid-connect] element, which
// carries data-context ("form" on wizard step 5, "resume" on credit setup) and
// data-csrf. The server resolves the application from the session for that
// context; nothing here names an application id.
//
// On /plaid/oauth-return the element also carries data-link-token: the
// applicant is back from an OAuth bank's own login page, so Link reopens on
// its own with that token to finish, and data-continue-url (if set) is where
// to go once it has.
(function () {
  function post(root, path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.csrf },
      body: JSON.stringify(Object.assign({ context: root.dataset.context }, body || {})),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || 'Something went wrong. Please upload your statements instead.');
        return data;
      });
    });
  }

  function mount(root) {
    const btn = root.querySelector('.plaid-btn');
    const status = root.querySelector('.plaid-status');
    function say(msg, kind) {
      status.textContent = msg;
      status.className = 'plaid-status' + (kind ? ' ' + kind : '');
    }
    function finished(connected) {
      root.dispatchEvent(new CustomEvent('plaid:done', { bubbles: true, detail: { connected: connected } }));
      if (connected && root.dataset.continueUrl) window.location.href = root.dataset.continueUrl;
    }

    function openLink(token, receivedRedirectUri) {
      const handler = window.Plaid.create({
        token: token,
        receivedRedirectUri: receivedRedirectUri,
        onSuccess: function (publicToken, meta) {
          say('Connecting to ' + ((meta.institution && meta.institution.name) || 'your bank') + '…');
          post(root, '/plaid/exchange', {
            public_token: publicToken,
            institution: meta.institution ? { name: meta.institution.name } : null,
          }).then(function (res) {
            root.classList.add('connected');
            btn.hidden = true;
            say('✓ Connected' + (res.institution ? ' to ' + res.institution : '') +
                '. We\'ll fetch your statements automatically — no need to upload them.', 'ok');
            finished(true);
          }).catch(function (e) {
            btn.disabled = false;
            say(e.message, 'err');
            finished(false);
          });
        },
        onExit: function (err, meta) {
          if (root.classList.contains('connected')) return;
          btn.disabled = false;
          say(err ? 'The bank connection didn\'t complete. You can try again or upload PDFs.' : '', err ? 'err' : '');
          post(root, '/plaid/exit', {
            status: meta && meta.status,
            error_code: err && err.error_code,
          }).catch(function () {});
          finished(false);
        },
      });
      handler.open();
    }

    btn.addEventListener('click', function () {
      if (!window.Plaid) { say('Bank connection failed to load. Please upload your statements instead.', 'err'); return; }
      btn.disabled = true;
      say('Opening a secure connection…');
      post(root, '/plaid/link-token').then(function (data) {
        openLink(data.link_token);
      }).catch(function (e) {
        btn.disabled = false;
        say(e.message, 'err');
      });
    });

    if (root.dataset.linkToken && window.Plaid) {
      btn.disabled = true;
      say('Finishing your bank connection…');
      openLink(root.dataset.linkToken, window.location.href);
    }
  }

  document.querySelectorAll('[data-plaid-connect]').forEach(mount);
})();
