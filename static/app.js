  let allArtists      = [];
  let selectedArtist  = null;
  let currentAlbums   = [];
  let currentDiscFilter = 'all';
  let logsCollapsed   = false;
  let searchTimer     = null;
  let homeLoaded      = false;

  // ── Sidebar drawer (mobile) ──────────────────────────────────────────

  function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const open = sidebar.classList.toggle('open');
    document.getElementById('sidebar-backdrop').classList.toggle('visible', open);
    document.getElementById('btn-menu').setAttribute('aria-expanded', open);
  }

  function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-backdrop').classList.remove('visible');
    document.getElementById('btn-menu').setAttribute('aria-expanded', 'false');
  }

  // ── Utility ─────────────────────────────────────────────────────────

  function esc(s) {
    return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function fmtDur(secs) {
    const m = Math.floor(secs / 60), s = String(secs % 60).padStart(2, '0');
    return `${m}:${s}`;
  }

  function bigImg(url) {
    return url ? url.replace('/250x250-', '/500x500-') : '';
  }

  async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json', 'X-API-Key': window._apiKey || '' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch('/api' + path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.status === 204 ? null : r.json();
  }

  function openModal(id) {
    const el = document.getElementById(id);
    el.classList.remove('hidden');
    el.querySelectorAll('input').forEach(i => i.value = '');
    const first = el.querySelector('input');
    if (first) setTimeout(() => first.focus(), 30);
  }
  function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

  document.querySelectorAll('.modal-backdrop').forEach(el => {
    el.addEventListener('click', e => { if (e.target === el) el.classList.add('hidden'); });
  });

  // Close search drop on outside click
  document.addEventListener('click', e => {
    if (!document.getElementById('search-wrap').contains(e.target)) {
      document.getElementById('search-drop').classList.add('hidden');
    }
  });

  // ── Stats ────────────────────────────────────────────────────────────

  async function retryAlbumCard(albumId) {
    await api('POST', `/albums/${albumId}/retry`);
    showToast('Retry queued');
    if (selectedArtist) loadAlbums(selectedArtist.id);
  }

let logExpanded = false;
function expandLogs() {
  logExpanded = !logExpanded;
  document.getElementById('log-body').style.height = logExpanded ? '350px' : '150px';
  document.getElementById('log-expand').textContent = logExpanded ? '⤡' : '⤢';
}

async function loadStats() {
    try {
      const s = await api('GET', '/stats');
      document.getElementById('s-complete').textContent    = s.complete;
      document.getElementById('s-partial').textContent     = s.partial;
      document.getElementById('s-missing').textContent     = s.pending;
      document.getElementById('s-downloading').textContent = s.downloading;
      document.getElementById('s-error').textContent       = s.error;
      const pill = document.getElementById('cycle-pill');
      pill.textContent = s.cycle_running ? 'Running…' : 'Idle';
      pill.className   = s.cycle_running ? 'running' : '';
    } catch (_) {}
  }

  // ── Sidebar artists ──────────────────────────────────────────────────

  async function loadArtists() {
    try { allArtists = await api('GET', '/artists'); } catch (_) { return; }
    renderArtistList();
  }

  function renderArtistList() {
    const q = document.getElementById('artist-search').value.toLowerCase();
    const list = q ? allArtists.filter(a => a.name.toLowerCase().includes(q)) : allArtists;
    const el = document.getElementById('artist-list');
    if (!list.length) { el.innerHTML = '<div class="empty">No artists.</div>'; return; }
    el.innerHTML = list.map(a => {
      const initials = a.name.split(' ').map(w => w[0]).slice(0,2).join('').toUpperCase();
      const avatar = a.image_url ? `<img src="${esc(a.image_url)}" alt="" loading="lazy">` : initials;
      const sel = selectedArtist && selectedArtist.id === a.id ? 'selected' : '';
      const count = a.album_total > 0 ? `${a.album_done}/${a.album_total} albums` : 'No albums';
      return `
        <div class="artist-row ${sel}" data-act="selectArtist" data-id="${a.id}" role="listitem">
          <div class="artist-avatar" aria-hidden="true">${avatar}</div>
          <div class="artist-meta">
            <div class="name">${esc(a.name)}</div>
            <div class="count">${count}</div>
          </div>
          <button class="artist-row-btn"
                  data-act="toggleMonitor" data-id="${a.id}" data-monitored="${a.monitored ? 'true' : 'false'}"
                  aria-label="${a.monitored ? 'Pause' : 'Resume'} ${esc(a.name)}">${a.monitored ? '⏸' : '▶'}</button>
        </div>`;
    }).join('');
  }

  // ── View switching ───────────────────────────────────────────────────

  function showHome() {
    closeSidebar();
    selectedArtist = null;
    document.getElementById('home-view').classList.remove('hidden');
    document.getElementById('artist-view').classList.add('hidden');
    document.getElementById('btn-back').style.display = 'none';
    renderArtistList();
    if (!homeLoaded) { homeLoaded = true; loadHome(); }
  }

  async function selectArtist(id) {
    selectedArtist = allArtists.find(a => a.id === id);
    if (!selectedArtist) return;
    closeSidebar();
    document.getElementById('home-view').classList.add('hidden');
    document.getElementById('artist-view').classList.remove('hidden');
    document.getElementById('btn-back').style.display = '';
    renderArtistList();
    renderHero(selectedArtist);
    currentDiscFilter = 'all';
    setDiscTab('all');
    const ds = document.getElementById('discSearch');
    if (ds) ds.value = '';
    document.getElementById('track-list').innerHTML = '<div class="loading">Loading…</div>';
    document.getElementById('row-related').innerHTML = '<div class="loading">Loading…</div>';
    await Promise.all([
      loadAlbums(id),
      loadTopTracks(id),
      loadRelated(id),
    ]);
  }

  function renderHero(artist) {
    const hero = document.getElementById('artist-hero');
    const large = bigImg(artist.image_url);
    // Only allow plain http(s) URLs into the CSS url() to prevent CSS injection.
    let safeUrl = '';
    try { const u = new URL(large); if (u.protocol === 'https:' || u.protocol === 'http:') safeUrl = u.href; } catch (e) {}
    if (safeUrl) {
      hero.style.backgroundImage = `url("${encodeURI(safeUrl)}")`;
    } else {
      hero.style.backgroundImage = '';
      hero.style.background = 'linear-gradient(135deg, #1e1b4b, var(--surface2))';
    }
    document.getElementById('hero-name').textContent = artist.name;
    document.getElementById('btn-monitor').textContent = artist.monitored ? 'Pause' : 'Resume';
  }

  // ── Top tracks ───────────────────────────────────────────────────────

  async function loadTopTracks(artistId) {
    const el = document.getElementById('track-list');
    try {
      const tracks = await api('GET', `/artists/${artistId}/top-tracks`);
      if (!tracks.length) { el.innerHTML = '<div class="empty">No track data available.</div>'; return; }
      el.innerHTML = tracks.map((t, i) => {
        const thumb = t.album_cover
          ? `<img class="track-thumb" src="${esc(t.album_cover)}" alt="" loading="lazy">`
          : `<div class="track-thumb" style="background:var(--border)"></div>`;
        return `
          <div class="track-row">
            <span class="track-num">${i + 1}</span>
            ${thumb}
            <div class="track-info">
              <div class="track-title">${esc(t.title)}</div>
              <div class="track-album-label">${esc(t.album_title)}</div>
            </div>
            <span class="track-dur">${fmtDur(t.duration)}</span>
            <button class="track-dl-btn" title="Download track"
                    data-id="${esc(t.id)}" data-title="${esc(t.title)}"
                    data-album="${esc(t.album_title)}" data-year="${esc(t.year)}"
                    data-act="downloadTrack">↓</button>
          </div>`;
      }).join('');
    } catch (_) {
      el.innerHTML = '<div class="empty">Could not load tracks.</div>';
    }
  }

  async function downloadTrack(btn) {
    const { id, title, album, year } = btn.dataset;
    const artist = selectedArtist?.name ?? '';
    btn.textContent = '…'; btn.disabled = true;
    try {
      await api('POST', '/tracks/download', { track_id: id, title, artist, album, year: year || '' });
      btn.textContent = '✓'; btn.style.color = 'var(--green)';
    } catch (_) {
      btn.textContent = '↓'; btn.disabled = false;
    }
  }

  // ── Album track modal ────────────────────────────────────────────────

  async function openAlbum(albumId) {
    const album = currentAlbums.find(a => a.id === albumId);
    if (!album) return;

    const coverEl = document.getElementById('album-modal-cover');
    coverEl.innerHTML = album.cover_url
      ? `<img src="${esc(album.cover_url)}" alt="">`
      : '♪';
    document.getElementById('lbl-album-tracks').textContent = album.title;
    document.getElementById('album-modal-sub').textContent =
      [album.year, album.track_count ? `${album.track_count} tracks` : null]
        .filter(Boolean).join(' · ');
    document.getElementById('album-track-list').innerHTML = '<div class="loading">Loading…</div>';
    openModal('modal-album-tracks');
    loadAlbumDisk(albumId);

    try {
      const tracks = await api('GET', `/albums/${albumId}/tracks`);
      if (!tracks.length) {
        document.getElementById('album-track-list').innerHTML =
          '<div class="empty">No tracks found on Deezer for this album.</div>';
        return;
      }
      const artistName = selectedArtist?.name ?? '';
      const thumbHtml = album.cover_url
        ? `<img src="${esc(album.cover_url)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:4px;">`
        : '';
      document.getElementById('album-track-list').innerHTML = tracks.map((t, i) => `
        <div class="track-row">
          <span class="track-num">${t.track_number || i + 1}</span>
          <div class="track-thumb" style="background:var(--border);border-radius:4px;">${thumbHtml}</div>
          <div class="track-info">
            <div class="track-title">${esc(t.title)}</div>
            <div class="track-album-label">${esc(t.artist || artistName)}</div>
          </div>
          <span class="track-dur">${fmtDur(t.duration)}</span>
          <button class="track-dl-btn" title="Download track"
                  data-id="${esc(t.id)}" data-title="${esc(t.title)}"
                  data-album="${esc(album.title)}" data-year="${esc(album.year || '')}"
                  data-act="downloadTrack">↓</button>
        </div>`).join('');
    } catch (_) {
      document.getElementById('album-track-list').innerHTML =
        '<div class="empty">Could not load tracks.</div>';
    }
  }

  // ── On-disk files (library management) ───────────────────────────────

  let _diskAlbumId = null;

  async function loadAlbumDisk(albumId) {
    _diskAlbumId = albumId;
    document.getElementById('album-tagfix-section').innerHTML = '';
    document.getElementById('album-sonic-section').innerHTML = '';
    const sec = document.getElementById('album-disk-section');
    const delBtn = document.getElementById('btn-delete-album-disk');
    sec.innerHTML = '<div class="loading">Checking disk…</div>';
    delBtn.style.display = 'none';
    try {
      const info = await api('GET', `/albums/${albumId}/files`);
      if (!info.exists || !info.files.length) {
        sec.innerHTML = '<div class="disk-empty">Not on disk yet.</div>';
        return;
      }
      delBtn.style.display = '';
      const mb = b => (b / 1048576).toFixed(1) + ' MB';
      sec.innerHTML =
        `<div class="disk-head">On disk · ${info.files.length} file${info.files.length > 1 ? 's' : ''} · ${mb(info.total_bytes)}</div>` +
        info.files.map(f => `
          <div class="disk-row">
            <span class="disk-name" title="${esc(f.name)}">${esc(f.name)}</span>
            <span class="disk-meta">${esc((f.ext || '').toUpperCase())}${f.bitrate ? ' · ' + f.bitrate + 'k' : ''}${f.lossless ? ' · <span class="disk-lossless">LOSSLESS</span>' : ''} · ${mb(f.size)}</span>
            <button class="disk-del" title="Delete this file from disk" data-act="deleteDiskFile" data-name="${esc(f.name)}">🗑</button>
          </div>`).join('');
    } catch (_) {
      sec.innerHTML = '<div class="disk-empty">Could not read disk.</div>';
    }
  }

  async function deleteDiskFile(el) {
    if (!_diskAlbumId) return;
    const name = el.dataset.name;
    if (!confirm(`Delete this file from disk?\n\n${name}`)) return;
    try {
      await api('POST', `/albums/${_diskAlbumId}/files/delete`, { filename: name });
      await loadAlbumDisk(_diskAlbumId);
      if (selectedArtist) loadAlbums(selectedArtist.id);
    } catch (e) {
      alert('Could not delete file: ' + (e.message || e));
    }
  }

  async function deleteAlbumDisk() {
    if (!_diskAlbumId) return;
    const album = currentAlbums.find(a => a.id === _diskAlbumId);
    const label = album ? album.title : 'this album';
    if (!confirm(`Delete all downloaded files for "${label}" from disk?\n\nThe album stays in your library as "missing" so you can re-download it later.`)) return;
    try {
      await api('DELETE', `/albums/${_diskAlbumId}/files`);
      closeModal('modal-album-tracks');
      if (selectedArtist) await loadAlbums(selectedArtist.id);
      await loadStats();
    } catch (e) {
      alert('Could not delete album files: ' + (e.message || e));
    }
  }

  // ── Sonic similarity (AudioMuse) ─────────────────────────────────────

  let _sonicAvailable = false;

  async function loadSonicStatus() {
    try {
      const s = await api('GET', '/sonic/status');
      _sonicAvailable = !!s.available;
    } catch (_) { _sonicAvailable = false; }
    // Only offer sonic features when the backend can actually serve them.
    const btn = document.getElementById('btn-more-like-this');
    if (btn) btn.style.display = _sonicAvailable ? '' : 'none';
    const row = document.getElementById('sonic-opt-row');
    if (row) row.style.display = _sonicAvailable ? '' : 'none';
  }

  async function moreLikeThis() {
    if (!_diskAlbumId) return;
    const sec = document.getElementById('album-sonic-section');
    sec.innerHTML = '<div class="loading">Finding tracks that sound like this…</div>';
    try {
      const r = await api('GET', `/albums/${_diskAlbumId}/similar?n=12`);
      if (!r.tracks || !r.tracks.length) {
        sec.innerHTML = '<div class="disk-empty">No sonic matches found — this album may not be analyzed yet.</div>';
        return;
      }
      sec.innerHTML =
        `<div class="sonic-head">Sounds like ${esc(r.album)} · ${r.tracks.length} tracks in your library</div>` +
        r.tracks.map(t => {
          // distance -> a friendlier 0-100 "match" score; nearer is better.
          const pct = t.distance != null ? Math.max(0, Math.round((1 - t.distance) * 100)) : null;
          return `<div class="sonic-row">
            <div class="sonic-main">
              <div class="sonic-title">${esc(t.title || '')}</div>
              <div class="sonic-sub">${esc(t.artist || '')}${t.album ? ' · ' + esc(t.album) : ''}${t.genre ? ' · ' + esc(t.genre) : ''}</div>
            </div>
            ${pct != null ? `<span class="sonic-dist">${pct}%</span>` : ''}
          </div>`;
        }).join('');
    } catch (e) {
      sec.innerHTML = `<div class="disk-empty">Sonic search failed: ${esc(e.message || String(e))}</div>`;
    }
  }

  // ── Auto-tagger (fingerprint-based tag fix) ──────────────────────────

  let _tagfixAlbumId = null;

  async function fixTags() {
    if (!_diskAlbumId) return;
    _tagfixAlbumId = _diskAlbumId;
    const sec = document.getElementById('album-tagfix-section');
    sec.innerHTML = '<div class="loading">Fingerprinting tracks — this can take a bit…</div>';
    let pv;
    try {
      pv = await api('GET', `/albums/${_tagfixAlbumId}/tagfix/preview`);
    } catch (e) {
      sec.innerHTML = `<div class="disk-empty">Tag preview failed: ${esc(e.message || String(e))}</div>`;
      return;
    }
    if (!pv.available || !pv.files.length) {
      sec.innerHTML = '<div class="disk-empty">No single-album folder on disk to tag.</div>';
      return;
    }
    const matched = pv.files.filter(f => f.proposed).length;
    const rows = pv.files.map(f => {
      const badge = f.proposed
        ? `<span class="tagfix-badge tagfix-${f.tier}">${Math.round(f.proposed.score * 100)}%</span>`
        : '<span class="tagfix-badge tagfix-none">no match</span>';
      const cur = `${esc(f.current.artist || '—')} · ${esc(f.current.title || f.file)}`;
      const proposedLine = f.proposed
        ? `<div class="tagfix-new">${esc(f.proposed.artist || '')} · ${esc(f.proposed.title || '')}</div>`
        : '';
      const checkbox = (f.proposed && f.changed)
        ? `<input type="checkbox" data-tf-file="${esc(f.file)}" data-tf-artist="${esc(f.proposed.artist || '')}" data-tf-title="${esc(f.proposed.title || '')}"${f.tier === 'high' ? ' checked' : ''}>`
        : '<input type="checkbox" disabled>';
      return `<div class="tagfix-row">${checkbox}
          <div class="tagfix-main">
            <div class="tagfix-cur">${cur}</div>
            ${proposedLine}
          </div>${badge}</div>`;
    }).join('');
    sec.innerHTML =
      `<div class="tagfix-head">Proposed tags · ${matched}/${pv.files.length} matched — review before applying</div>` +
      rows +
      `<div class="tagfix-actions">
        <button class="sm" data-act="applyTagFix">Apply checked</button>
        <button class="ghost sm" data-act="undoTagFix">Undo tag fix</button>
      </div>`;
  }

  async function applyTagFix() {
    if (!_tagfixAlbumId) return;
    const sec = document.getElementById('album-tagfix-section');
    const items = [...sec.querySelectorAll('input[type=checkbox][data-tf-file]:checked')].map(c => ({
      file: c.dataset.tfFile, artist: c.dataset.tfArtist, title: c.dataset.tfTitle,
    }));
    if (!items.length) { alert('Nothing checked to apply.'); return; }
    if (!confirm(`Write corrected tags to ${items.length} file(s)?\n\nOriginals are backed up first — you can Undo.`)) return;
    try {
      const r = await api('POST', `/albums/${_tagfixAlbumId}/tagfix/apply`, { items });
      if (selectedArtist) loadAlbums(selectedArtist.id);
      await fixTags();
      alert(`Applied corrected tags to ${r.applied} file(s).`);
    } catch (e) { alert('Apply failed: ' + (e.message || e)); }
  }

  async function undoTagFix() {
    if (!_tagfixAlbumId) return;
    if (!confirm('Restore the original tags for this album?')) return;
    try {
      const r = await api('POST', `/albums/${_tagfixAlbumId}/tagfix/undo`);
      if (selectedArtist) loadAlbums(selectedArtist.id);
      await fixTags();
      alert(`Restored original tags on ${r.restored} file(s).`);
    } catch (e) { alert('Undo failed: ' + (e.message || e)); }
  }

  // ── Albums / discography ─────────────────────────────────────────────

  async function loadAlbums(artistId) {
    try {
      const albums = await api('GET', `/artists/${artistId}/albums`);
      currentAlbums = albums;
      const done   = albums.filter(a => a.status === 'complete').length;
      const wanted = albums.filter(a => a.wanted).length;
      document.getElementById('albums-count').textContent =
        `DISCOGRAPHY · ${done}/${albums.length} complete · ${wanted} wanted`;
      const entry = allArtists.find(a => a.id === artistId);
      if (entry) { entry.album_total = albums.length; entry.album_done = done; renderArtistList(); }
      renderAlbumGrid();
    } catch (_) {
      document.getElementById('album-grid').innerHTML = '<div class="empty">Failed to load albums.</div>';
    }
  }

  function renderAlbumGrid() {
    const grid = document.getElementById('album-grid');
    const filtered = currentDiscFilter === 'all'
      ? currentAlbums
      : currentAlbums.filter(a => (a.record_type || 'album') === currentDiscFilter);

    if (!filtered.length) { grid.innerHTML = '<div class="empty">No albums in this category.</div>'; return; }

    const q = (document.getElementById('discSearch')?.value || '').toLowerCase();
    const visible = q ? filtered.filter(a => a.title.toLowerCase().includes(q)) : filtered;
    grid.innerHTML = visible.map(al => {
      const cover = al.cover_url
        ? `<img src="${esc(al.cover_url)}" alt="${esc(al.title)} cover" loading="lazy">`
        : '♪';
      const retryBtn = '';
      const trackLine = '';
      const dlBtn = al.status !== 'complete'
        ? `<button class="overlay-btn" data-act="downloadAlbumNow" data-id="${al.id}"
                   aria-label="Download ${esc(al.title)}">Download</button>`
        : '';
      const badge = `<span class="badge badge-${esc(al.status)}">${esc(al.status)}</span>`;
      return `
        <div class="album-card" role="button" tabindex="0"
             data-act="openAlbum" data-id="${al.id}"
             aria-label="View tracks: ${esc(al.title)}">
          <div class="album-cover">
            ${cover}
            <div class="album-overlay">
              ${dlBtn}${retryBtn}
            </div>
          </div>
          <div class="album-body">
            <div class="title">${esc(al.title)}</div>
            <div class="year">${esc(al.year || '—')}</div>
            ${badge}${trackLine}
          </div>
        </div>`;
    }).join('');
  }

  function filterDisc(type) {
    currentDiscFilter = type;
    setDiscTab(type);
    renderAlbumGrid();
  }

  function setDiscTab(type) {
    document.querySelectorAll('.disc-tab').forEach(btn => {
      const active = btn.textContent.toLowerCase().replace(/s$/, '') === type || (type === 'all' && btn.textContent === 'All');
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
  }

  // ── Wanted ───────────────────────────────────────────────────────────

  async function ignoreAll() {
    if (!selectedArtist) return;
    await api('PATCH', `/artists/${selectedArtist.id}/albums/wanted?wanted=false`);
    await loadAlbums(selectedArtist.id);
  }

  async function confirmDiscography() {
    if (!selectedArtist) return;
    const total = currentAlbums.length;
    if (!confirm(`Download all ${total} albums from ${selectedArtist.name}?\n\nThis will queue the full discography.`)) return;
    await api('PATCH', `/artists/${selectedArtist.id}/albums/wanted?wanted=true`);
    await api('POST', '/cycle/run');
    await loadAlbums(selectedArtist.id);
  }

  async function downloadAlbumNow(albumId) {
    const album = currentAlbums.find(a => a.id === albumId);
    if (album) { album.status = 'downloading'; renderAlbumGrid(); }
    try {
      await api('POST', `/albums/${albumId}/retry`);
    } catch (_) {
      if (selectedArtist) await loadAlbums(selectedArtist.id);
    }
  }

  // ── Related artists on artist page ───────────────────────────────────

  async function loadRelated(artistId) {
    const el = document.getElementById('row-related');
    try {
      const results = await api('GET', `/artists/${artistId}/related`);
      if (!results.length) { el.innerHTML = '<div class="empty">No related artists found.</div>'; return; }
      const known = new Set(allArtists.map(a => a.name.toLowerCase()));
      el.innerHTML = results.map(a => makeArtistCard(a, known.has(a.name.toLowerCase()))).join('');
    } catch (_) {
      el.innerHTML = '<div class="empty">Could not load related artists.</div>';
    }
  }

  // ── Home page ────────────────────────────────────────────────────────

  async function loadHome() {
    await Promise.all([
      loadRecent(), loadCharts(), loadSimilar(),
      loadDigest(false), loadAISuggestions(), loadAIReleases(), loadAIPlaylists(),
    ]);
  }

  // ── AI surfaces (GLM-4 powered) ──────────────────────────────────────

  async function loadDigest(forceFresh) {
    const sec = document.getElementById('sec-ai-digest');
    const body = document.getElementById('ai-digest-body');
    try {
      // Aggressive cache (digest costs an LLM call) — only refresh when asked.
      const cached = !forceFresh && sessionStorage.getItem('aria.digest');
      if (cached) {
        sec.style.display = '';
        body.textContent = cached;
        return;
      }
      body.innerHTML = '<div class="loading">Asking AI…</div>';
      sec.style.display = '';
      const { digest, error } = await api('GET', '/ai-digest');
      if (digest) {
        body.textContent = digest;
        sessionStorage.setItem('aria.digest', digest);
      } else {
        body.innerHTML = `<div class="empty">${error || 'Digest unavailable.'}</div>`;
      }
    } catch (_) {
      body.innerHTML = '<div class="empty">Could not load digest.</div>';
    }
  }

  async function loadAISuggestions() {
    const row = document.getElementById('row-ai-suggestions');
    try {
      const sugg = await api('GET', '/ai-suggestions');
      if (!sugg.length) {
        row.innerHTML = '<div class="empty">No AI suggestions yet — they refresh weekly.</div>';
        return;
      }
      const known = new Set(allArtists.map(a => a.name.toLowerCase()));
      row.innerHTML = sugg.map(s => `
        <div class="h-artist-card ai-sugg-card">
          <div class="ai-sugg-name">${escapeHtml(s.artist_name)}</div>
          <div class="ai-sugg-reason">${escapeHtml(s.reason)}</div>
          <div class="ai-sugg-actions">
            ${known.has(s.artist_name.toLowerCase())
              ? '<span class="muted-tag">In library</span>'
              : `<button class="sm" data-act="addSuggestedArtist" data-name="${esc(s.artist_name)}" data-sugg-id="${s.id}">+ Add</button>`}
            <button class="ghost sm" data-act="dismissSuggestion" data-id="${s.id}">Dismiss</button>
          </div>
        </div>
      `).join('');
    } catch (_) {
      row.innerHTML = '<div class="empty">Could not load AI suggestions.</div>';
    }
  }

  async function addSuggestedArtist(name, suggId) {
    try {
      await api('POST', '/artists', { name });
      await api('DELETE', `/ai-suggestions/${suggId}`);
      await loadArtists();
      loadAISuggestions();
    } catch (e) {
      alert('Could not add artist: ' + (e.message || e));
    }
  }

  async function dismissSuggestion(suggId) {
    try {
      await api('DELETE', `/ai-suggestions/${suggId}`);
      loadAISuggestions();
    } catch (_) {}
  }

  async function loadAIReleases() {
    const row = document.getElementById('row-ai-releases');
    try {
      const items = await api('GET', '/ai-releases');
      if (!items.length) {
        row.innerHTML = '<div class="empty">No new-release highlights yet — check back after the weekly run, or click refresh.</div>';
        return;
      }
      row.innerHTML = items.map(r => `
        <div class="h-artist-card ai-release-card">
          <div class="ai-release-title">${escapeHtml(r.album_title)}</div>
          <div class="ai-release-artist">${escapeHtml(r.artist_name)} · ${escapeHtml(r.year || '')}</div>
          <div class="ai-release-reason">${escapeHtml(r.reason)}</div>
          <div class="ai-sugg-actions">
            <button class="ghost sm" data-act="dismissRelease" data-id="${r.id}">Dismiss</button>
          </div>
        </div>
      `).join('');
    } catch (_) {
      row.innerHTML = '<div class="empty">Could not load new-release highlights.</div>';
    }
  }

  async function dismissRelease(id) {
    try {
      await api('DELETE', `/ai-releases/${id}`);
      loadAIReleases();
    } catch (_) {}
  }

  async function refreshReleases() {
    const row = document.getElementById('row-ai-releases');
    row.innerHTML = '<div class="loading">Asking AI — this can take 30-60s…</div>';
    try {
      await api('POST', '/ai-releases/refresh');
      // Poll for completion — releases_feed grows when AI finishes.
      let attempts = 0;
      const initial = (await api('GET', '/ai-releases')).length;
      const tick = setInterval(async () => {
        attempts++;
        const items = await api('GET', '/ai-releases');
        if (items.length !== initial || attempts >= 30) {
          clearInterval(tick);
          loadAIReleases();
        }
      }, 4000);
    } catch (_) {
      loadAIReleases();
    }
  }

  let _currentPlaylistId = null;

  async function loadAIPlaylists() {
    const list = document.getElementById('ai-playlists-list');
    try {
      const playlists = await api('GET', '/ai-playlists');
      if (!playlists.length) {
        list.innerHTML = '<div class="empty">No AI playlists yet — they refresh weekly, or use the buttons above to make one now.</div>';
        return;
      }
      list.innerHTML = playlists.map(p => `
        <div class="ai-playlist-row" data-act="openPlaylistModal" data-id="${p.id}">
          <div class="ai-playlist-name">${escapeHtml(p.name)}</div>
          <div class="ai-playlist-desc">${escapeHtml(p.description || '')}</div>
          <div class="ai-playlist-meta">${p.tracks.length} tracks · ${p.created_at}</div>
        </div>
      `).join('');
    } catch (_) {
      list.innerHTML = '<div class="empty">Could not load playlists.</div>';
    }
  }

  async function openPlaylistModal(id) {
    const playlists = await api('GET', '/ai-playlists');
    const p = playlists.find(x => x.id === id);
    if (!p) return;
    _currentPlaylistId = id;
    document.getElementById('lbl-ai-playlist').textContent = p.name;
    document.getElementById('ai-playlist-desc').textContent = p.description || '';
    const tracks = p.tracks || [];
    document.getElementById('ai-playlist-tracks').innerHTML = tracks.length
      ? tracks.map((t, i) => `
          <div class="ai-track">
            <span class="ai-track-n">${i + 1}.</span>
            <span class="ai-track-artist">${escapeHtml(t.artist || '?')}</span>
            <span class="ai-track-sep">—</span>
            <span class="ai-track-title">${escapeHtml(t.title || '?')}</span>
          </div>
        `).join('')
      : '<div class="empty">No tracks.</div>';
    openModal('modal-ai-playlist');
  }

  async function deleteCurrentPlaylist() {
    if (!_currentPlaylistId) return;
    if (!confirm('Delete this playlist?')) return;
    await api('DELETE', `/ai-playlists/${_currentPlaylistId}`);
    closeModal('modal-ai-playlist');
    loadAIPlaylists();
  }

  async function submitMoodPlaylist() {
    const input = document.getElementById('input-mood');
    const mood = input.value.trim();
    if (!mood) return;
    input.disabled = true;
    try {
      const sonicEl = document.getElementById('input-sonic');
      const body = { mood };
      if (_sonicAvailable && sonicEl) body.sonic = sonicEl.checked;
      await api('POST', '/ai-playlists/mood', body);
      closeModal('modal-mood-playlist');
      input.value = '';
      input.disabled = false;
      // The mood playlist queues — poll until it appears in the list.
      const initial = (await api('GET', '/ai-playlists')).length;
      let attempts = 0;
      const tick = setInterval(async () => {
        attempts++;
        const cur = await api('GET', '/ai-playlists');
        if (cur.length > initial || attempts >= 30) {
          clearInterval(tick);
          loadAIPlaylists();
        }
      }, 3000);
    } catch (e) {
      input.disabled = false;
      alert('Could not generate playlist: ' + (e.message || e));
    }
  }

  async function submitLyricSearch() {
    const input = document.getElementById('input-lyric-query');
    const results = document.getElementById('lyric-results');
    const query = input.value.trim();
    if (!query) return;
    results.innerHTML = '<div class="loading">Asking AI…</div>';
    try {
      const { results: tracks } = await api('POST', '/ai-lyric-search', { query });
      if (!tracks.length) {
        results.innerHTML = '<div class="empty">No matches.</div>';
        return;
      }
      results.innerHTML = tracks.map(t => `
        <div class="ai-track">
          <div>
            <span class="ai-track-artist">${escapeHtml(t.artist)}</span>
            <span class="ai-track-sep">—</span>
            <span class="ai-track-title">${escapeHtml(t.title)}</span>
          </div>
          <div class="ai-track-reason">${escapeHtml(t.reason || '')}</div>
        </div>
      `).join('');
    } catch (e) {
      results.innerHTML = `<div class="empty">Search failed: ${escapeHtml(e.message || String(e))}</div>`;
    }
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function escapeAttr(s) {
    return String(s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
  }

  async function loadRecent() {
    try {
      const items = await api('GET', '/recent');
      if (!items.length) return;
      document.getElementById('sec-recent').style.display = '';
      const row = document.getElementById('row-recent');
      row.innerHTML = items.map(a => makeAlbumCard(a, a.artist_id)).join('');
    } catch (_) {}
  }

  async function loadCharts() {
    try {
      const { artists, releases } = await api('GET', '/charts');
      const known = new Set(allArtists.map(a => a.name.toLowerCase()));
      if (artists.length) {
        document.getElementById('row-trending').innerHTML =
          artists.map(a => makeArtistCard(a, known.has(a.name.toLowerCase()))).join('');
      }
      if (releases.length) {
        document.getElementById('row-releases').innerHTML =
          releases.map(a => makeAlbumCard(a, null)).join('');
      }
    } catch (_) {
      document.getElementById('row-trending').innerHTML = '<div class="empty">Could not load charts.</div>';
      document.getElementById('row-releases').innerHTML = '<div class="empty">Could not load releases.</div>';
    }
  }

  async function loadSimilar() {
    try {
      const results = await api('GET', '/discover');
      const known = new Set(allArtists.map(a => a.name.toLowerCase()));
      if (!results.length) {
        document.getElementById('row-similar').innerHTML = '<div class="empty">Add artists to get recommendations.</div>';
        return;
      }
      document.getElementById('row-similar').innerHTML =
        results.map(a => makeArtistCard(a, known.has(a.name.toLowerCase()))).join('');
    } catch (_) {}
  }

  // ── Card builders ────────────────────────────────────────────────────

  function makeArtistCard(artist, inLibrary) {
    const initials = artist.name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
    const avatar = artist.image_url
      ? `<img src="${esc(artist.image_url)}" alt="" loading="lazy">`
      : initials;
    const tag = inLibrary
      ? `<span class="h-artist-tag tag-library">IN LIBRARY</span>`
      : `<span class="h-artist-tag tag-add">+ Add</span>`;
    return `
      <div class="h-artist-card" data-name="${esc(artist.name)}" data-in-lib="${inLibrary}"
           role="button" tabindex="0" data-act="onArtistCardClick">
        <div class="h-artist-avatar">${avatar}</div>
        <div class="h-artist-name">${esc(artist.name)}</div>
        ${tag}
      </div>`;
  }

  async function onArtistCardClick(card) {
    const name = card.dataset.name;
    if (card.dataset.inLib === 'true') {
      selectArtistByName(name);
    } else {
      card.style.opacity = '0.5';
      card.style.pointerEvents = 'none';
      await addArtistByName(name);
      card.dataset.inLib = 'true';
      card.querySelector('.h-artist-tag').textContent = 'IN LIBRARY';
      card.querySelector('.h-artist-tag').className = 'h-artist-tag tag-library';
      card.style.opacity = '';
      card.style.pointerEvents = '';
      selectArtistByName(name);
    }
  }

  function makeAlbumCard(item, artistId = null) {
    const cover = item.cover_url
      ? `<img src="${esc(item.cover_url)}" alt="" loading="lazy">`
      : '♪';
    const actAttr = artistId !== null ? `data-act="selectArtist" data-id="${Number(artistId)}"` : '';
    return `
      <div class="h-album-card" ${actAttr}>
        <div class="h-album-cover">${cover}</div>
        <div class="h-album-info">
          <div class="h-album-title">${esc(item.title)}</div>
          <div class="h-album-artist">${esc(item.artist || item.year || '')}</div>
        </div>
      </div>`;
  }

  // ── Global search ────────────────────────────────────────────────────

  function onGlobalSearch() {
    clearTimeout(searchTimer);
    const q = document.getElementById('global-search').value.trim();
    const drop = document.getElementById('search-drop');
    if (!q) { drop.classList.add('hidden'); return; }
    drop.classList.remove('hidden');
    drop.innerHTML = '<div class="drop-section">Searching…</div>';
    searchTimer = setTimeout(() => runSearch(q), 280);
  }

  async function runSearch(q) {
    const drop = document.getElementById('search-drop');
    try {
      const results = await api('GET', `/search/artists?q=${encodeURIComponent(q)}`);
      if (!results.length) { drop.innerHTML = '<div class="drop-section">No results.</div>'; return; }
      const known = new Set(allArtists.map(a => a.name.toLowerCase()));
      drop.innerHTML = '<div class="drop-section">Artists</div>' +
        results.map(a => {
          const initials = a.name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
          const thumb = a.image_url
            ? `<img src="${esc(a.image_url)}" alt="" loading="lazy">`
            : initials;
          const inLib = known.has(a.name.toLowerCase());
          const badge = inLib ? `<span class="drop-badge">IN LIBRARY</span>` : '';
          return `
            <div class="drop-row" role="option" data-name="${esc(a.name)}" data-in-lib="${inLib}"
                 data-act="onDropRowClick">
              <div class="drop-thumb round">${thumb}</div>
              <div class="drop-meta">
                <div class="drop-name">${esc(a.name)}</div>
                <div class="drop-sub">Artist</div>
              </div>
              ${badge}
            </div>`;
        }).join('');
    } catch (_) {
      drop.innerHTML = '<div class="drop-section">Search failed.</div>';
    }
  }

  async function onDropRowClick(row) {
    const name = row.dataset.name;
    const inLib = row.dataset.inLib === 'true';
    document.getElementById('search-drop').classList.add('hidden');
    document.getElementById('global-search').value = '';
    if (inLib) {
      selectArtistByName(name);
    } else {
      await addArtistByName(name);
      selectArtistByName(name);
    }
  }

  // ── Artist actions ───────────────────────────────────────────────────

  async function addArtistByName(name) {
    try {
      await api('POST', '/artists', { name });
      await loadArtists();
      await loadStats();
      homeLoaded = false; // refresh recommendations next time
    } catch (e) { alert('Could not add artist: ' + e.message); }
  }

  function selectArtistByName(name) {
    const artist = allArtists.find(a => a.name.toLowerCase() === name.toLowerCase());
    if (artist) selectArtist(artist.id);
  }

  async function submitAddArtist() {
    const name = document.getElementById('input-artist-name').value.trim();
    if (!name) return;
    closeModal('modal-add-artist');
    await addArtistByName(name);
    selectArtistByName(name);
  }

  async function submitAddAlbum() {
    const title = document.getElementById('input-album-title').value.trim();
    const year  = document.getElementById('input-album-year').value.trim();
    if (!title || !selectedArtist) return;
    closeModal('modal-add-album');
    try {
      await api('POST', `/artists/${selectedArtist.id}/albums`, { title, year });
      await loadAlbums(selectedArtist.id);
      await loadStats();
    } catch (e) { alert('Could not add album: ' + e.message); }
  }

  async function removeArtistSelected() {
    if (!selectedArtist) return;
    if (!confirm(`Remove "${selectedArtist.name}" and all their albums from your library?`)) return;
    const purge = confirm('Also DELETE their downloaded files from disk?\n\nOK = delete the audio files too (permanent).\nCancel = keep files on disk, only remove from the library.');
    await api('DELETE', `/artists/${selectedArtist.id}${purge ? '?purge=true' : ''}`);
    showHome();
    await loadArtists();
    await loadStats();
  }

  async function toggleMonitorSelected() {
    if (!selectedArtist) return;
    await toggleMonitor(selectedArtist.id, selectedArtist.monitored);
  }

  async function toggleMonitor(id, current) {
    await api('PATCH', `/artists/${id}/monitor?monitored=${!current}`);
    const artist = allArtists.find(a => a.id === id);
    if (artist) artist.monitored = !current;
    if (selectedArtist && selectedArtist.id === id) {
      selectedArtist.monitored = !current;
      document.getElementById('btn-monitor').textContent = selectedArtist.monitored ? 'Pause' : 'Resume';
    }
    renderArtistList();
  }

  async function resyncArtist() {
    if (!selectedArtist) return;
    const btn = document.getElementById('btn-resync');
    btn.textContent = 'Queued'; btn.disabled = true;
    try {
      await api('POST', `/artists/${selectedArtist.id}/sync`);
    } catch (_) {}
    setTimeout(() => { btn.textContent = 'Resync'; btn.disabled = false; }, 2000);
  }


  // ── Cycle / Logs ─────────────────────────────────────────────────────

  async function triggerCycle() {
    await api('POST', '/cycle/run');
    const pill = document.getElementById('cycle-pill');
    pill.textContent = 'Running…'; pill.className = 'running';
  }

  async function scanExistingLibrary() {
    // Walks MUSIC_DIR on the server + marks albums whose folders exist as
    // complete. Idempotent. Surfaces the result counts inline so Chuck can
    // see what changed without digging through logs.
    const pill = document.getElementById('cycle-pill');
    const orig = pill.textContent;
    pill.textContent = 'Scanning…'; pill.className = 'running';
    try {
      const r = await api('POST', '/scan-existing');
      pill.textContent = `Matched ${r.matched_albums}/${r.matched_albums + r.unmatched_dirs}`;
      pill.className = '';
      // Reload artists so the album-count badges update without a page refresh.
      await loadArtists();
      // Restore the idle pill after a few seconds.
      setTimeout(() => { pill.textContent = orig || 'Idle'; }, 5000);
    } catch (e) {
      pill.textContent = 'Scan failed'; pill.className = '';
      alert('Scan failed: ' + (e.message || e));
    }
  }

  async function importLibrary() {
    if (!confirm('Import all music already on disk into Aria?\n\n'
      + 'Aria will scan your library (this can take a minute), then add the artists and '
      + 'albums it finds — marking what you already own as complete. It ONLY adds library '
      + 'entries; it never moves or deletes your audio files, and it can be undone.')) return;
    const pill = document.getElementById('cycle-pill');
    pill.textContent = 'Importing…'; pill.className = 'running';
    try {
      const r = await api('POST', '/library/ingest');
      pill.textContent = `Imported +${r.albums_created}`; pill.className = '';
      homeLoaded = false;
      await loadArtists();
      await loadStats();
      setTimeout(() => { pill.textContent = 'Idle'; }, 6000);
      alert(`Import complete:\n`
        + `  +${r.artists_created} artists\n`
        + `  +${r.albums_created} albums\n`
        + `  ${r.albums_reconciled} existing albums reconciled\n\n`
        + `It only added library entries — your files are untouched, and this can be undone.`);
    } catch (e) {
      pill.textContent = 'Import failed'; pill.className = '';
      alert('Import failed: ' + (e.message || e));
    }
  }

  async function enrichImported() {
    if (!confirm('Fill in your imported artists with their photos and full discographies?\n\n'
      + 'Runs in the background (a few minutes) — pulls each artist\'s catalog from Spotify/Deezer. '
      + 'Nothing auto-downloads (extra albums show as available to want). Ambiguous / soundtrack '
      + 'names with no confident match are skipped.')) return;
    try {
      await api('POST', '/library/enrich-imported');
      alert('Enrichment started — watch the Activity Log. Artists will gain photos and full '
        + 'discographies over the next few minutes; refresh to see them fill in.');
    } catch (e) {
      alert('Could not start enrichment: ' + (e.message || e));
    }
  }

  function toggleLogs() {
    logsCollapsed = !logsCollapsed;
    document.getElementById('log-body').classList.toggle('collapsed', logsCollapsed);
    document.getElementById('log-toggle').textContent = logsCollapsed ? '▼' : '▲';
    document.getElementById('log-drawer-header').setAttribute('aria-expanded', String(!logsCollapsed));
  }

  async function loadLogs() {
    try {
      const logs = await api('GET', '/logs?limit=100');
      const el = document.getElementById('log-body');
      if (!logs.length) { el.innerHTML = '<div class="empty">No logs yet.</div>'; return; }
      el.innerHTML = logs.map(l => `
        <div class="log-entry">
          <span class="log-time">${esc(l.at.slice(11,19))}</span>
          <span class="log-${esc(l.level)}">${esc(l.message)}</span>
        </div>`).join('');
    } catch (_) {}
  }

  // ── Refresh loop ─────────────────────────────────────────────────────

  async function refresh() {
    await Promise.all([loadStats(), loadLogs()]);
    if (selectedArtist) await loadAlbums(selectedArtist.id);
  }

  // ── Event delegation (CSP-safe: no inline handlers) ──────────────────
  // A single delegated listener replaces every former inline onclick. Because
  // it resolves the *innermost* [data-act] ancestor, a click on a nested
  // button (e.g. the monitor toggle inside an artist row) fires only that
  // button's action — which is exactly what the old event.stopPropagation()
  // calls did.
  const CLICK_ACTIONS = {
    closeSidebar: () => closeSidebar(),
    toggleSidebar: () => toggleSidebar(),
    showHome: () => showHome(),
    triggerCycle: () => triggerCycle(),
    scanExisting: () => scanExistingLibrary(),
    importLibrary: () => importLibrary(),
    enrichImported: () => enrichImported(),
    openModal: (el) => openModal(el.dataset.modal),
    closeModal: (el) => closeModal(el.dataset.modal),
    loadDigestRefresh: () => loadDigest(true),
    refreshReleases: () => refreshReleases(),
    confirmDiscography: () => confirmDiscography(),
    resyncArtist: () => resyncArtist(),
    toggleMonitorSelected: () => toggleMonitorSelected(),
    removeArtistSelected: () => removeArtistSelected(),
    ignoreAll: () => ignoreAll(),
    filterDisc: (el) => filterDisc(el.dataset.filter),
    toggleLogs: () => toggleLogs(),
    submitAddArtist: () => submitAddArtist(),
    submitAddAlbum: () => submitAddAlbum(),
    submitMoodPlaylist: () => submitMoodPlaylist(),
    submitLyricSearch: () => submitLyricSearch(),
    deleteCurrentPlaylist: () => deleteCurrentPlaylist(),
    selectArtist: (el) => selectArtist(Number(el.dataset.id)),
    toggleMonitor: (el) => toggleMonitor(Number(el.dataset.id), el.dataset.monitored === 'true'),
    downloadTrack: (el) => downloadTrack(el),
    downloadAlbumNow: (el) => downloadAlbumNow(Number(el.dataset.id)),
    openAlbum: (el) => openAlbum(Number(el.dataset.id)),
    addSuggestedArtist: (el) => addSuggestedArtist(el.dataset.name, Number(el.dataset.suggId)),
    dismissSuggestion: (el) => dismissSuggestion(Number(el.dataset.id)),
    dismissRelease: (el) => dismissRelease(Number(el.dataset.id)),
    openPlaylistModal: (el) => openPlaylistModal(Number(el.dataset.id)),
    onArtistCardClick: (el) => onArtistCardClick(el),
    onDropRowClick: (el) => onDropRowClick(el),
    deleteDiskFile: (el) => deleteDiskFile(el),
    deleteAlbumDisk: () => deleteAlbumDisk(),
    moreLikeThis: () => moreLikeThis(),
    fixTags: () => fixTags(),
    applyTagFix: () => applyTagFix(),
    undoTagFix: () => undoTagFix(),
  };

  const ENTER_ACTIONS = {
    submitAddArtist: () => submitAddArtist(),
    submitAddAlbum: () => submitAddAlbum(),
    submitMoodPlaylist: () => submitMoodPlaylist(),
    submitLyricSearch: () => submitLyricSearch(),
    focusAlbumYear: () => document.getElementById('input-album-year').focus(),
  };

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const fn = CLICK_ACTIONS[el.dataset.act];
    if (!fn) return;
    if (el.tagName === 'A') e.preventDefault();
    fn(el, e);
  });

  // Keyboard activation for role="button" elements that are NOT native
  // buttons/links (they carry an explicit tabindex). Native <button>/<a> fire
  // a click on Enter/Space themselves, so scoping to [tabindex] avoids a
  // double-trigger.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const el = e.target;
    if (!el.matches || !el.matches('[data-act][tabindex]')) return;
    const fn = CLICK_ACTIONS[el.dataset.act];
    if (!fn) return;
    e.preventDefault();
    fn(el, e);
  });

  // Enter-to-submit / advance inside modal inputs.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const act = e.target && e.target.dataset ? e.target.dataset.enter : null;
    if (!act) return;
    const fn = ENTER_ACTIONS[act];
    if (fn) { e.preventDefault(); fn(); }
  });

  function wireEvents() {
    const gs = document.getElementById('global-search');
    gs.addEventListener('input', onGlobalSearch);
    gs.addEventListener('focus', onGlobalSearch);
    document.getElementById('artist-search').addEventListener('input', renderArtistList);
    document.getElementById('discSearch').addEventListener('input', renderAlbumGrid);
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (document.getElementById('sidebar').classList.contains('open')) { closeSidebar(); return; }
      document.querySelectorAll('.modal-backdrop:not(.hidden)').forEach(m => m.classList.add('hidden'));
    }
  });

  (async () => {
    wireEvents();
    loadSonicStatus();   // gates the sonic UI; non-blocking
    await Promise.all([loadStats(), loadArtists(), loadLogs()]);
    showHome();
    setInterval(refresh, 10000);
  })();
