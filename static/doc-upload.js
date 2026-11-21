/*
 * Shared document upload for the application form (step 5) and the Plaid
 * return page. Both used to POST everything in one request and go to the next
 * page whatever happened -- so a request over the server's 100 MB cap, a
 * network drop, or a file the server could not store all looked like success,
 * and the documents were lost without anyone knowing.
 *
 * window.uploadDocuments(sid, entries, csrf) where entries = [{field, file}]
 *   -> Promise<{ saved: [entry], failed: [entry], tooBig: [entry] }>
 *
 * Files go up in batches that stay under the server cap, so the total can be
 * any size. A single file bigger than one batch is refused before upload
 * (tooBig) rather than sent to fail. `failed` lists what did not get stored,
 * from the server's own per-file report or because its batch never arrived.
 */
(function () {
  var BATCH_LIMIT = 90 * 1024 * 1024;   // under MAX_CONTENT_LENGTH (100 MB) with room for the multipart envelope

  function batches(entries) {
    var out = [], cur = [], size = 0;
    entries.forEach(function (e) {
      if (cur.length && size + e.file.size > BATCH_LIMIT) { out.push(cur); cur = []; size = 0; }
      cur.push(e); size += e.file.size;
    });
    if (cur.length) out.push(cur);
    return out;
  }

  function send(sid, batch, csrf) {
    var fd = new FormData();
    batch.forEach(function (e) { fd.append(e.field, e.file); });
    if (csrf) fd.append('csrf_token', csrf);
    return fetch('/upload-documents/' + sid, { method: 'POST', body: fd })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok || !data || data.success !== true) return { all: true };
          return { names: data.failed || [] };
        });
      })
      .catch(function () { return { all: true }; });
  }

  window.uploadDocuments = function (sid, entries, csrf) {
    var tooBig = entries.filter(function (e) { return e.file.size > BATCH_LIMIT; });
    var ok = entries.filter(function (e) { return e.file.size <= BATCH_LIMIT; });
    var saved = [], failed = [];
    return batches(ok).reduce(function (chain, batch) {
      return chain.then(function () {
        return send(sid, batch, csrf).then(function (res) {
          if (res.all) { failed = failed.concat(batch); return; }
          var bad = res.names.slice();
          batch.forEach(function (e) {
            var i = bad.indexOf(e.file.name);
            if (i !== -1) { bad.splice(i, 1); failed.push(e); } else { saved.push(e); }
          });
        });
      });
    }, Promise.resolve()).then(function () {
      return { saved: saved, failed: failed, tooBig: tooBig };
    });
  };

  // One sentence for the applicant: what did not go through, and why.
  window.describeUploadProblem = function (res) {
    var parts = [];
    function names(list) {
      var n = list.map(function (e) { return '"' + e.file.name + '"'; });
      return n.length > 3 ? n.slice(0, 3).join(', ') + ' and ' + (n.length - 3) + ' more' : n.join(', ');
    }
    if (res.tooBig.length) {
      parts.push(names(res.tooBig) + (res.tooBig.length === 1 ? ' is' : ' are')
        + ' too large to upload (90 MB max per file). Try a smaller copy, or photos of the pages.');
    }
    if (res.failed.length) {
      parts.push(names(res.failed) + (res.failed.length === 1 ? " wasn't" : " weren't")
        + ' saved. Check your connection and tap Try again.');
    }
    return parts.join(' ');
  };
})();
