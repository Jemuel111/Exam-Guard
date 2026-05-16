/**
 * ExamGuard — Push Notification Manager
 * Drop this as static/push.js
 * Include in teacher_dashboard.html and student_dashboard.html
 *
 * Usage:
 *   <script src="/static/push.js"></script>
 *   // Then call: ExamGuardPush.init(userId, role)
 */

const ExamGuardPush = (() => {
  'use strict';

  let _userId = null;
  let _role   = null;
  let _swReg  = null;

  // ── Helpers ──────────────────────────────────────────────────────────────

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64  = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw     = atob(base64);
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  function authHeader() {
    const token = localStorage.getItem('eg_token') || '';
    return {
      'Content-Type': 'application/json',
      'X-Demo-Mode': '1',
      'Authorization': `Bearer ${token}`,
    };
  }

  // ── Check support ─────────────────────────────────────────────────────────

  function isSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  // ── Get VAPID public key from server ─────────────────────────────────────

  async function getVapidKey() {
    try {
      const res  = await fetch('/api/push/vapid-public-key', { headers: authHeader() });
      const data = await res.json();
      return data.publicKey || null;
    } catch {
      return null;
    }
  }

  // ── Request permission and subscribe ─────────────────────────────────────

  async function subscribe() {
    if (!isSupported()) {
      return { success: false, reason: 'not-supported' };
    }

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      return { success: false, reason: 'denied' };
    }

    // Force SW registration if not already done
    let reg;
    try {
      reg = await navigator.serviceWorker.register('/static/sw.js', { scope: '/' });
      await navigator.serviceWorker.ready;
      _swReg = await navigator.serviceWorker.ready;
    } catch (e) {
      console.error('[Push] SW registration failed:', e);
      return { success: false, reason: 'sw-failed: ' + e.message };
    }

    const vapidKey = await getVapidKey();
    if (!vapidKey) {
      return { success: false, reason: 'no-vapid-key' };
    }

    console.log('[Push] VAPID key length:', vapidKey.length);
    console.log('[Push] VAPID key:', vapidKey);

    try {
      // Unsubscribe from any existing broken subscription first
      const existing = await _swReg.pushManager.getSubscription();
      if (existing) {
        await existing.unsubscribe();
        console.log('[Push] Cleared existing subscription');
      }

      const subscription = await _swReg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });

      console.log('[Push] Got subscription:', subscription.endpoint);
      await _saveSubscription(subscription);
      localStorage.setItem('eg_push_subscribed', '1');
      return { success: true, reason: 'subscribed' };

    } catch (err) {
      console.error('[Push] pushManager.subscribe error:', err);
      return { success: false, reason: err.message };
    }
  }
  // ── Save subscription to server ───────────────────────────────────────────

  async function _saveSubscription(subscription) {
    try {
      await fetch('/api/push/subscribe', {
        method:  'POST',
        headers: authHeader(),
        body: JSON.stringify({
          subscription: subscription.toJSON(),
          user_id: _userId,
          role:    _role,
        }),
      });
    } catch (err) {
      console.error('[Push] Failed to save subscription:', err);
    }
  }

  // ── Unsubscribe ───────────────────────────────────────────────────────────

  async function unsubscribe() {
    if (!_swReg) {
      _swReg = await navigator.serviceWorker.ready;
    }
    const sub = await _swReg.pushManager.getSubscription();
    if (!sub) return { success: true };

    await fetch('/api/push/unsubscribe', {
      method:  'POST',
      headers: authHeader(),
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });

    await sub.unsubscribe();
    localStorage.removeItem('eg_push_subscribed');
    console.log('[Push] Unsubscribed');
    return { success: true };
  }

  // ── Check current state ───────────────────────────────────────────────────

  async function getState() {
    if (!isSupported()) return 'unsupported';
    const permission = Notification.permission;
    if (permission === 'denied') return 'denied';
    if (permission === 'default') return 'default';

    _swReg = _swReg || await navigator.serviceWorker.ready;
    const sub = await _swReg.pushManager.getSubscription();
    return sub ? 'subscribed' : 'default';
  }

  // ── Render a notification toggle button into a container ─────────────────

  async function renderToggleButton(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!isSupported()) {
      container.innerHTML = `
        <div class="push-unsupported" style="font-family:var(--mono,monospace);font-size:0.62rem;color:var(--low,#666);padding:8px 12px;border:1px solid var(--bd,#ddd)">
          🔕 Push not supported in this browser
        </div>`;
      return;
    }

    const state = await getState();
    _renderButton(container, state);
  }

  function _renderButton(container, state) {
    const configs = {
      subscribed: {
        label: '🔔 Notifications On',
        cls:   'push-btn push-btn--on',
        action: 'unsubscribe',
        style: 'color:var(--ok,#22c55e);border-color:rgba(34,197,94,0.3);background:rgba(34,197,94,0.06)',
      },
      default: {
        label: '🔕 Enable Notifications',
        cls:   'push-btn push-btn--off',
        action: 'subscribe',
        style: 'color:var(--mid,#666);border-color:var(--bd,#ddd)',
      },
      denied: {
        label: '⛔ Notifications Blocked',
        cls:   'push-btn push-btn--denied',
        action: null,
        style: 'color:var(--danger,#ef4444);border-color:rgba(239,68,68,0.3);cursor:not-allowed;opacity:0.6',
      },
      unsupported: {
        label: '🔕 Not Supported',
        cls:   'push-btn push-btn--unsupported',
        action: null,
        style: 'color:var(--low,#999);opacity:0.5;cursor:not-allowed',
      },
    };

    const cfg = configs[state] || configs.default;
    container.innerHTML = `
      <button
        class="${cfg.cls}"
        data-action="${cfg.action || ''}"
        style="font-family:var(--mono,'Courier New',monospace);font-size:0.62rem;font-weight:700;
               letter-spacing:0.06em;padding:7px 14px;border:1px solid;background:none;
               cursor:pointer;transition:all 0.15s;${cfg.style}"
        ${cfg.action ? '' : 'disabled'}
      >${cfg.label}</button>
      ${state === 'denied' ? '<div style="font-size:0.55rem;color:var(--low,#999);margin-top:4px;font-family:var(--mono,monospace)">Allow in browser settings to enable</div>' : ''}
    `;

    const btn = container.querySelector('button');
    if (btn && cfg.action) {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = cfg.action === 'subscribe' ? 'Enabling…' : 'Disabling…';

        if (cfg.action === 'subscribe') {
          const result = await subscribe();
          if (result.success) {
            _renderButton(container, 'subscribed');
            _showToast('Notifications enabled', 'ok');
          } else if (result.reason === 'denied') {
            _renderButton(container, 'denied');
          } else {
            btn.disabled = false;
            btn.textContent = cfg.label;
            _showToast('Could not enable notifications: ' + result.reason, 'warn');
          }
        } else {
          await unsubscribe();
          _renderButton(container, 'default');
          _showToast('Notifications disabled', 'warn');
        }
      });
    }
  }

  // ── Simple toast helper ───────────────────────────────────────────────────

  function _showToast(msg, type = 'ok') {
    // Try to use ExamGuard's existing toast system
    if (window.showToastMsg) {
      window.showToastMsg(msg, type);
      return;
    }
    // Fallback: plain alert
    console.log(`[Push Toast] ${type}: ${msg}`);
  }

  // ── Auto-prompt on first visit (after 3 seconds) ──────────────────────────

  async function _autoPrompt() {
    if (localStorage.getItem('eg_push_dismissed')) return;
    if (localStorage.getItem('eg_push_subscribed')) return;
    if (!isSupported()) return;
    if (Notification.permission !== 'default') return;

    setTimeout(async () => {
      const banner = document.getElementById('pushPromptBanner');
      if (banner) {
        banner.style.display = 'flex';
      }
    }, 3000);
  }

  // ── Inject prompt banner HTML ─────────────────────────────────────────────

  function injectPromptBanner() {
    if (!isSupported()) return;
    if (document.getElementById('pushPromptBanner')) return;

    const banner = document.createElement('div');
    banner.id = 'pushPromptBanner';
    banner.style.cssText = `
      display:none; position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
      z-index:999; background:var(--bg1,#0d1424); border:1px solid rgba(37,99,235,0.4);
      border-top:3px solid var(--acc,#2563eb); padding:16px 20px;
      max-width:380px; width:calc(100% - 48px);
      box-shadow:0 8px 32px rgba(0,0,0,0.4);
      align-items:flex-start; gap:14px;
      animation:slideUp 0.4s cubic-bezier(0.16,1,0.3,1);
    `;
    banner.innerHTML = `
      <style>
        @keyframes slideUp {
          from { opacity:0; transform:translateX(-50%) translateY(20px); }
          to   { opacity:1; transform:translateX(-50%) translateY(0); }
        }
        #pushPromptBanner .push-icon {
          width:36px;height:36px;flex-shrink:0;
          background:rgba(37,99,235,0.1);border:1px solid rgba(37,99,235,0.25);
          display:flex;align-items:center;justify-content:center;font-size:1rem;
        }
        #pushPromptBanner .push-title {
          font-family:var(--mono,'Courier New',monospace);font-size:0.7rem;
          font-weight:700;color:var(--hi,#f0f6ff);margin-bottom:3px;
        }
        #pushPromptBanner .push-desc {
          font-size:0.72rem;color:var(--mid,#6b84a8);line-height:1.5;margin-bottom:12px;
        }
        #pushPromptBanner .push-actions { display:flex; gap:8px; }
        #pushPromptBanner .push-allow {
          font-family:var(--mono,'Courier New',monospace);font-size:0.6rem;font-weight:700;
          letter-spacing:0.06em;padding:7px 16px;background:var(--acc,#2563eb);
          color:#fff;border:none;cursor:pointer;transition:background 0.15s;
        }
        #pushPromptBanner .push-allow:hover { background:#1d4ed8; }
        #pushPromptBanner .push-dismiss {
          font-family:var(--mono,'Courier New',monospace);font-size:0.6rem;
          padding:7px 12px;background:none;border:1px solid var(--bd,rgba(255,255,255,0.08));
          color:var(--mid,#6b84a8);cursor:pointer;transition:all 0.15s;
        }
        #pushPromptBanner .push-dismiss:hover { border-color:var(--danger,#ef4444);color:var(--danger,#ef4444); }
        #pushPromptBanner .push-close {
          position:absolute;top:10px;right:10px;background:none;border:none;
          color:var(--mid,#6b84a8);cursor:pointer;font-size:1rem;line-height:1;padding:4px;
        }
      </style>
      <div class="push-icon">🔔</div>
      <div style="flex:1;position:relative">
        <button class="push-close" id="pushBannerClose">×</button>
        <div class="push-title">ENABLE NOTIFICATIONS</div>
        <div class="push-desc">
          Get instant alerts when high-risk exam sessions are detected or important updates occur.
        </div>
        <div class="push-actions">
          <button class="push-allow" id="pushBannerAllow">Allow Notifications</button>
          <button class="push-dismiss" id="pushBannerDismiss">Not Now</button>
        </div>
      </div>
    `;
    document.body.appendChild(banner);

    document.getElementById('pushBannerAllow').addEventListener('click', async () => {
      banner.style.display = 'none';
      const result = await subscribe();
      if (result.success) {
        _showToast('Notifications enabled successfully!', 'ok');
        // Refresh any toggle buttons
        const toggleContainers = document.querySelectorAll('[data-push-toggle]');
        toggleContainers.forEach(c => renderToggleButton(c.id));
      }
    });

    document.getElementById('pushBannerDismiss').addEventListener('click', () => {
      banner.style.display = 'none';
      localStorage.setItem('eg_push_dismissed', '1');
    });

    document.getElementById('pushBannerClose').addEventListener('click', () => {
      banner.style.display = 'none';
    });
  }

  // ── Public init ───────────────────────────────────────────────────────────

  async function init(userId, role) {
    _userId = userId;
    _role   = role;

    if (!isSupported()) {
      console.log('[Push] Push notifications not supported');
      return;
    }

    // Wait for SW to be ready
    try {
      _swReg = await navigator.serviceWorker.ready;
    } catch (e) {
      console.warn('[Push] Service worker not ready:', e);
      return;
    }

    injectPromptBanner();
    await _autoPrompt();

    // Re-sync subscription with server on each page load (handles key rotations)
    const existing = await _swReg.pushManager.getSubscription();
    if (existing && Notification.permission === 'granted') {
      await _saveSubscription(existing);
    }

    console.log('[Push] Push manager initialized for', role, userId);
  }

  return { init, subscribe, unsubscribe, getState, renderToggleButton, isSupported };
})();