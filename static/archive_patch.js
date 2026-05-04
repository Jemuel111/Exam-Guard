/**
 * ExamGuard — Archive & Forgot Password Patch
 * Drop this file as static/archive_patch.js
 * Include at the BOTTOM of teacher_dashboard.html before </body>
 *
 * What this adds:
 * 1. Archive sidebar link + badge counter
 * 2. Overwrites deleteExam() and deleteStudent() with soft-delete + toast
 * 3. Adds archiveSession() function
 * 4. Adds loadArchiveBadge() + showToastMsg() helpers
 * 5. Injects "Archive" button into reports table rows via MutationObserver
 */

(function() {
    'use strict';
  
    // ── Wait for DOM ────────────────────────────────────────────────────────────
    function ready(fn) {
      if (document.readyState !== 'loading') fn();
      else document.addEventListener('DOMContentLoaded', fn);
    }
  
    // ── Toast helper ────────────────────────────────────────────────────────────
    window.showToastMsg = function(msg, severity = 'warn') {
      const container = document.getElementById('rtToasts');
      if (!container) return;
      const div = document.createElement('div');
      div.className = 'rt-toast medium';
      div.style.cssText = `border-left-color:var(--${severity === 'ok' ? 'ok' : 'warn'})`;
      div.innerHTML = `
        <div class="rt-toast-name" style="color:var(--${severity === 'ok' ? 'ok' : 'warn'})">
          ${severity === 'ok' ? 'Restored' : 'Archived'}
        </div>
        <div class="rt-toast-msg">${msg}</div>`;
      container.appendChild(div);
      setTimeout(() => div.remove(), 6000);
    };
  
    // ── Archive badge ────────────────────────────────────────────────────────────
    window.loadArchiveBadge = async function() {
      try {
        const d = await fetch('/api/archive/count', { headers: window.authHeader }).then(r => r.json());
        const badge = document.getElementById('archiveBadge');
        if (!badge) return;
        if (d.total > 0) {
          badge.textContent = d.total;
          badge.style.display = 'inline-block';
        } else {
          badge.style.display = 'none';
        }
      } catch(e) {}
    };
  
    // ── Override deleteExam → soft archive ────────────────────────────────────
    window.deleteExam = async function(id) {
      if (!confirm('Archive this exam?\n\nIt will be hidden from the exam list but can be restored or permanently deleted from the Archive page.')) return;
      try {
        const res = await fetch('/api/exams/' + id, { method:'DELETE', headers:window.authHeader }).then(r => r.json());
        if (res.success) {
          window.showToastMsg('Exam archived — visit Archive to restore or permanently delete.');
          if (typeof loadExams === 'function') loadExams();
          if (typeof loadStats === 'function') loadStats();
          window.loadArchiveBadge();
        }
      } catch(e) { alert('Error archiving exam.'); }
    };
  
    // ── Override deleteStudent → soft archive ────────────────────────────────
    window.deleteStudent = async function(id) {
      if (!confirm('Archive this student?\n\nThey will be hidden from the student list but can be restored or permanently deleted from the Archive page.')) return;
      try {
        const res = await fetch('/api/students/' + id, { method:'DELETE', headers:window.authHeader }).then(r => r.json());
        if (res.success) {
          window.showToastMsg('Student archived — visit Archive to restore or permanently delete.');
          if (typeof loadStudents === 'function') loadStudents();
          if (typeof loadStats === 'function') loadStats();
          window.loadArchiveBadge();
        }
      } catch(e) { alert('Error archiving student.'); }
    };
  
    // ── Archive session ──────────────────────────────────────────────────────
    window.archiveSession = async function(sessionId) {
      if (!confirm('Archive this session report?\n\nIt will be hidden from the reports list but can be restored from the Archive page.')) return;
      try {
        const res = await fetch('/api/archive_session/' + sessionId, { method:'POST', headers:window.authHeader }).then(r => r.json());
        if (res.success) {
          window.showToastMsg('Session archived — visit Archive to restore or permanently delete.');
          if (typeof loadReports === 'function') loadReports();
          window.loadArchiveBadge();
        }
      } catch(e) { alert('Error archiving session.'); }
    };
  
    // ── Inject Archive sidebar link ──────────────────────────────────────────
    function injectArchiveSidebarLink() {
      // Find the Reports sb-link and insert Archive after it
      const navLinks = document.querySelectorAll('.sb-link');
      let reportsLink = null;
      navLinks.forEach(link => {
        if (link.textContent.includes('Reports')) reportsLink = link;
      });
      if (!reportsLink || document.getElementById('archiveSidebarLink')) return;
  
      const archiveLink = document.createElement('a');
      archiveLink.className = 'sb-link';
      archiveLink.id = 'archiveSidebarLink';
      archiveLink.style.cursor = 'pointer';
      archiveLink.onclick = () => window.location.href = '/archive';
      archiveLink.innerHTML = `
        <span class="sb-icon">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="1" y="5" width="14" height="9" rx="1"/>
            <path d="M1 5l2-3h10l2 3"/>
            <path d="M6 9h4"/>
          </svg>
        </span>
        Archive
        <span class="sb-badge b-blue" id="archiveBadge" style="display:none;margin-left:auto">0</span>`;
      reportsLink.insertAdjacentElement('afterend', archiveLink);
    }
  
    // ── Patch reports table: add Archive button via MutationObserver ─────────
    function patchReportsTable() {
      const reportsBody = document.getElementById('reportsTableBody');
      if (!reportsBody) return;
  
      function addArchiveButtons() {
        const rows = reportsBody.querySelectorAll('tr');
        rows.forEach(row => {
          // Find the actions cell (last td)
          const cells = row.querySelectorAll('td');
          if (!cells.length) return;
          const actionCell = cells[cells.length - 1];
          // Skip if already has archive button
          if (actionCell.querySelector('.archive-session-btn')) return;
          // Get session ID from the View link href
          const viewLink = actionCell.querySelector('a[href*="/report/"]');
          if (!viewLink) return;
          const sessionId = viewLink.href.split('/report/')[1];
          if (!sessionId) return;
  
          const btn = document.createElement('button');
          btn.className = 'btn btn-ghost btn-sm archive-session-btn';
          btn.style.cssText = 'color:var(--warn);border-color:rgba(217,119,6,0.3)';
          btn.textContent = 'Archive';
          btn.onclick = () => window.archiveSession(sessionId);
          actionCell.style.display = 'flex';
          actionCell.style.gap = '4px';
          actionCell.style.flexWrap = 'wrap';
          actionCell.appendChild(btn);
        });
      }
  
      // Initial patch attempt
      addArchiveButtons();
  
      // Watch for dynamic table updates
      const observer = new MutationObserver(addArchiveButtons);
      observer.observe(reportsBody, { childList: true, subtree: true });
    }
  
    // ── Quick access Archive button in topbar ────────────────────────────────
    function injectTopbarArchiveBtn() {
      const tbRight = document.querySelector('.tb-right');
      if (!tbRight || document.getElementById('topbarArchiveBtn')) return;
      const btn = document.createElement('a');
      btn.id = 'topbarArchiveBtn';
      btn.href = '/archive';
      btn.className = 'btn btn-ghost btn-sm';
      btn.style.textDecoration = 'none';
      btn.innerHTML = `
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="1" y="5" width="14" height="9" rx="1"/><path d="M1 5l2-3h10l2 3"/><path d="M6 9h4"/></svg>
        Archive`;
      // Insert before the "+ New Exam" button
      const newExamBtn = tbRight.querySelector('.btn-primary');
      if (newExamBtn) tbRight.insertBefore(btn, newExamBtn);
      else tbRight.appendChild(btn);
    }
  
    // ── Init ─────────────────────────────────────────────────────────────────
    ready(function() {
      // Small delay to let the dashboard JS run first
      setTimeout(() => {
        injectArchiveSidebarLink();
        injectTopbarArchiveBtn();
        patchReportsTable();
        window.loadArchiveBadge();
        setInterval(window.loadArchiveBadge, 30000);
      }, 300);
  
      // Also watch for page switches to re-patch the reports table
      document.addEventListener('click', function(e) {
        const link = e.target.closest('.sb-link');
        if (link && link.textContent.includes('Reports')) {
          setTimeout(patchReportsTable, 500);
        }
      });
    });
  
  })();